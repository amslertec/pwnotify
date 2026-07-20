"""Microsoft-Graph-Client (App-Registration, Client-Credentials-Flow).

- Token via MSAL (blockierend -> in Thread ausgelagert), gecacht bis kurz vor Ablauf.
- Pagination über ``@odata.nextLink``.
- Throttling: 429 mit ``Retry-After`` wird respektiert (exponential backoff via tenacity).
- ``$select`` für minimale Payloads.
- Permission-Erkennung: der App-Token trägt die gewährten App-Rollen im ``roles``-Claim.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
import jwt
import msal
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ...core.errors import GraphError
from ...core.logging import get_logger

log = get_logger("graph")

REQUIRED_PERMISSIONS = ["User.Read.All", "Domain.Read.All", "Mail.Send"]

# Nur nötig, wenn eine Gruppe konfiguriert ist (gruppenbasierter Sync, SSO-Rollen).
# Fehlt sie, scheitern die betroffenen Abfragen mit 403 — deshalb darf der
# Verbindungstest sie nicht pauschal ignorieren, aber auch nicht pauschal verlangen.
GROUP_PERMISSION = "GroupMember.Read.All"

# $top=999 -> ~1M rows per paginated call, comfortably above any real tenant/group size --
# a firm ceiling against a broken or hostile @odata.nextLink chain looping unbounded (L6).
_MAX_PAGES = 1000

USER_SELECT = (
    "id,displayName,userPrincipalName,mail,otherMails,accountEnabled,"
    "lastPasswordChangeDateTime,passwordPolicies,department,jobTitle,assignedLicenses,"
    "preferredLanguage"
)

_CLOUDS = {
    "global": ("https://login.microsoftonline.com", "https://graph.microsoft.com"),
    "usgov": ("https://login.microsoftonline.us", "https://graph.microsoft.us"),
    "china": ("https://login.chinacloudapi.cn", "https://microsoftgraph.chinacloudapi.cn"),
}


def _ext_for(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".png")


@dataclass
class GraphConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    cloud: str = "global"

    @property
    def authority(self) -> str:
        login, _ = _CLOUDS.get(self.cloud, _CLOUDS["global"])
        return f"{login}/{self.tenant_id}"

    @property
    def base(self) -> str:
        _, graph = _CLOUDS.get(self.cloud, _CLOUDS["global"])
        return graph

    @property
    def scope(self) -> str:
        return f"{self.base}/.default"


@dataclass
class GraphConnectionResult:
    connected: bool
    tenant_id: str | None = None
    granted_permissions: list[str] = field(default_factory=list)
    missing_permissions: list[str] = field(default_factory=list)
    error: str | None = None


class _RetryableStatusError(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after


class GraphClient:
    def __init__(self, config: GraphConfig):
        self.config = config
        self._app: msal.ConfidentialClientApplication | None = None
        self._token: str | None = None
        self._token_exp: dt.datetime = dt.datetime.min.replace(tzinfo=dt.UTC)
        self._http: httpx.AsyncClient | None = None

    def _msal_app(self) -> msal.ConfidentialClientApplication:
        """Build the MSAL client lazily, on first token acquisition.

        Constructing it eagerly in ``__init__`` raises on an incomplete authority: an
        empty ``graph.tenant_id`` degrades the authority to
        ``https://login.microsoftonline.com/`` (no tenant path segment), which MSAL
        rejects at construction time. Since the mail backend defaults to ``graph``, a
        partially configured tenant would then crash the whole scheduled run just by
        BUILDING the sender — even with nothing to send (prod: run_id 19, tenant 2,
        checked=0). Deferring construction keeps an unused client harmless; a real
        misconfiguration only surfaces when a token is actually requested.
        """
        if self._app is None:
            # A9: validate config BEFORE constructing MSAL. An empty/whitespace tenant_id
            # (or client_id/secret) would otherwise fail only AFTER MSAL's instance-discovery
            # network roundtrip -- and in a send loop that roundtrip repeats per recipient
            # (send_mail -> _acquire_token per address), a self-DoS against a misconfigured
            # tenant. This check is cheap and deterministic, so even a per-recipient call
            # never touches the network.
            if not (
                self.config.tenant_id.strip()
                and self.config.client_id.strip()
                and self.config.client_secret.strip()
            ):
                raise GraphError(
                    "Graph ist nicht vollständig konfiguriert.", code="graph_not_configured"
                )
            try:
                self._app = msal.ConfidentialClientApplication(
                    self.config.client_id,
                    authority=self.config.authority,
                    client_credential=self.config.client_secret,
                )
            except ValueError as exc:
                # A10: surface a GraphError (not MSAL's raw ValueError) so the existing
                # `except GraphError` handlers (test_connection, get_user_photo) give a clean
                # "not configured" message instead of a generic 500 / raw MSAL string leaking
                # into per-user notify failures.
                raise GraphError(
                    f"Graph-Konfiguration ungültig: {exc}", code="graph_config_invalid"
                ) from exc
        return self._app

    # -- HTTP-Verbindung ----------------------------------------------------- #
    def _shared_http(self) -> httpx.AsyncClient:
        """Wiederverwendbarer Client — spart je Aufruf TCP- und TLS-Handshake.

        Beim Massenversand fällt das ins Gewicht: gemessen rund 26 ms pro Mail, die
        sonst nur für den Verbindungsaufbau draufgehen. Wird lazy erzeugt und über
        :meth:`aclose` geschlossen.
        """
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=30)
        return self._http

    async def aclose(self) -> None:
        """Offene Verbindungen schliessen. Mehrfach aufrufbar."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

    async def __aenter__(self) -> GraphClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # -- Auth ---------------------------------------------------------------- #
    async def _acquire_token(self) -> str:
        now = dt.datetime.now(dt.UTC)
        if self._token and now < self._token_exp - dt.timedelta(minutes=2):
            return self._token
        result = await asyncio.to_thread(
            self._msal_app().acquire_token_for_client, scopes=[self.config.scope]
        )
        if "access_token" not in result:
            desc = result.get("error_description", result.get("error", "unbekannter Fehler"))
            raise GraphError(desc, code="graph_token_failed")
        self._token = result["access_token"]
        self._token_exp = now + dt.timedelta(seconds=int(result.get("expires_in", 3600)))
        return self._token

    @staticmethod
    def _roles_from_token(token: str) -> list[str]:
        try:
            claims = jwt.decode(token, options={"verify_signature": False})
            roles = claims.get("roles", [])
            return list(roles) if isinstance(roles, list) else []
        except jwt.PyJWTError:
            return []

    # -- Pagination ---------------------------------------------------------- #
    def _same_graph_host(self, url: str) -> bool:
        """True only when ``url`` is https AND its host matches the configured Graph host.

        The scheme check matters: comparing only ``netloc`` would let an ``http://`` link to
        the same host pass, and the follow-up request would then carry the Bearer token over
        a cleartext connection (A2, CWE-918).
        """
        p = urlparse(url)
        return p.scheme == "https" and p.netloc == urlparse(self.config.base).netloc

    def _next_link(self, data: dict[str, Any]) -> str:
        """Return the ``@odata.nextLink`` to follow, or ``""`` to stop the loop.

        Every page request carries the app's Bearer token. ``@odata.nextLink`` is an
        absolute URL copied verbatim from the response body -- a spoofed/broken response (or
        a misbehaving proxy) could point it at a foreign host, or downgrade the same host to
        cleartext ``http://``, and the loop would then send the Bearer token there. Graph's
        response is TLS-authenticated so this is defense-in-depth (I3/A2), but a token leaking
        off-tenant is severe. A mismatching nextLink is a security event, not a normal loop
        end: we raise (not silently drop to ``""``, which would quietly truncate the result
        set and mask the tampering) so the whole paginated call aborts before the token is
        ever sent to the untrusted target.
        """
        nxt = str(data.get("@odata.nextLink") or "")
        if nxt and not self._same_graph_host(nxt):
            log.warning("graph_nextlink_host_mismatch", next_link=nxt)
            raise GraphError(
                "Nicht vertrauenswürdiger @odata.nextLink-Host/Schema; "
                "Abbruch zum Schutz des Zugriffstokens.",
                code="graph_nextlink_untrusted",
            )
        return nxt

    # -- HTTP ---------------------------------------------------------------- #
    @retry(
        retry=retry_if_exception_type(
            (_RetryableStatusError, httpx.TransportError, httpx.RemoteProtocolError)
        ),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _request(
        self, client: httpx.AsyncClient, method: str, url: str, **kwargs: Any
    ) -> httpx.Response:
        token = await self._acquire_token()
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        resp = await client.request(method, url, headers=headers, **kwargs)
        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", "5"))
            log.warning("graph_throttled", retry_after=retry_after, url=url)
            await asyncio.sleep(retry_after)
            raise _RetryableStatusError(retry_after)
        if resp.status_code >= 500:
            raise _RetryableStatusError(2.0)
        if resp.status_code >= 400:
            raise GraphError(
                f"{resp.status_code}: {resp.text[:300]}", status_code=502, code="graph_error"
            )
        return resp

    # -- Public API ---------------------------------------------------------- #
    async def test_connection(
        self, *, extra_permissions: list[str] | None = None
    ) -> GraphConnectionResult:
        """Prüft Token und Berechtigungen.

        ``extra_permissions`` ergänzt die Basisrechte um solche, die nur für aktivierte
        Funktionen nötig sind (z. B. ``GroupMember.Read.All``, sobald eine Gruppe
        konfiguriert ist). Ohne das meldet der Test „alles vorhanden“, während der
        gruppenbasierte Sync später mit 403 scheitert.
        """
        try:
            token = await self._acquire_token()
        except GraphError as exc:
            return GraphConnectionResult(connected=False, error=str(exc))
        granted = self._roles_from_token(token)
        required = [*REQUIRED_PERMISSIONS, *(extra_permissions or [])]
        missing = [p for p in required if p not in granted]
        return GraphConnectionResult(
            connected=True,
            tenant_id=self.config.tenant_id,
            granted_permissions=granted,
            missing_permissions=missing,
        )

    async def get_password_validity_map(self) -> tuple[int | None, dict[str, int]]:
        """Liefert (default_validity, {domain_suffix: validity}) aus /domains."""
        default_validity: int | None = None
        by_domain: dict[str, int] = {}
        async with httpx.AsyncClient(timeout=30) as client:
            url = (
                f"{self.config.base}/v1.0/domains?$select=id,isDefault,passwordValidityPeriodInDays"
            )
            pages = 0
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for dom in data.get("value", []):
                    validity = dom.get("passwordValidityPeriodInDays")
                    if validity is None:
                        continue
                    # 2147483647 = "läuft nie ab"
                    v = None if validity >= 2147483647 else int(validity)
                    if v is not None:
                        by_domain[dom["id"].lower()] = v
                    if dom.get("isDefault"):
                        default_validity = v
                pages += 1
                if pages >= _MAX_PAGES:
                    log.warning("graph_pagination_cap_reached", pages=pages)
                    break
                url = self._next_link(data)
        return default_validity, by_domain

    async def iter_users(self) -> AsyncIterator[dict[str, Any]]:
        """Alle Benutzer (paginiert, minimales $select)."""
        async with httpx.AsyncClient(timeout=60) as client:
            url = f"{self.config.base}/v1.0/users?$select={USER_SELECT}&$top=999"
            pages = 0
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for user in data.get("value", []):
                    yield user
                pages += 1
                if pages >= _MAX_PAGES:
                    log.warning("graph_pagination_cap_reached", pages=pages)
                    break
                url = self._next_link(data)

    async def get_user_photo(self, user_id: str) -> bytes | None:
        """Profilfoto eines Benutzers (Bytes) oder None, wenn keins vorhanden ist.

        Nutzt die bereits benötigte App-Berechtigung ``User.Read.All`` — keine
        zusätzliche Permission nötig. Fehler/kein Foto -> None (Fallback Initialen).
        """
        try:
            token = await self._acquire_token()
        except GraphError:
            return None
        url = f"{self.config.base}/v1.0/users/{user_id}/photo/$value"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError:
            return None
        if resp.status_code == 200 and resp.content:
            return resp.content
        return None

    async def iter_group_users(self, group_id: str) -> AsyncIterator[dict[str, Any]]:
        """Transitive Benutzer-Mitglieder einer Gruppe (voller $select, paginiert).

        ``transitiveMembers`` löst auch verschachtelte Gruppen auf; der OData-Cast
        ``microsoft.graph.user`` beschränkt auf echte Benutzerkonten (keine Geräte/SPs).
        """
        async with httpx.AsyncClient(timeout=60) as client:
            url = (
                f"{self.config.base}/v1.0/groups/{group_id}/transitiveMembers/"
                f"microsoft.graph.user?$select={USER_SELECT}&$top=999"
            )
            pages = 0
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for user in data.get("value", []):
                    yield user
                pages += 1
                if pages >= _MAX_PAGES:
                    log.warning("graph_pagination_cap_reached", pages=pages)
                    break
                url = self._next_link(data)

    async def get_group_members(self, group_id: str) -> list[dict[str, Any]]:
        """Benutzer-Mitglieder einer Gruppe (id, upn, displayName, accountEnabled)."""
        members: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            url = (
                f"{self.config.base}/v1.0/groups/{group_id}/transitiveMembers/"
                "microsoft.graph.user"
                "?$select=id,userPrincipalName,displayName,mail,accountEnabled&$top=999"
            )
            pages = 0
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                members.extend(data.get("value", []))
                pages += 1
                if pages >= _MAX_PAGES:
                    log.warning("graph_pagination_cap_reached", pages=pages)
                    break
                url = self._next_link(data)
        return members

    async def get_group_member_ids(self, group_id: str) -> set[str]:
        """Transitive Mitglieder-IDs einer Gruppe (für Exclude-Gruppen)."""
        ids: set[str] = set()
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.config.base}/v1.0/groups/{group_id}/transitiveMembers?$select=id&$top=999"
            pages = 0
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for member in data.get("value", []):
                    if "id" in member:
                        ids.add(member["id"])
                pages += 1
                if pages >= _MAX_PAGES:
                    log.warning("graph_pagination_cap_reached", pages=pages)
                    break
                url = self._next_link(data)
        return ids

    async def check_member_groups(self, user_id: str, group_ids: list[str]) -> set[str]:
        """Welche der genannten Gruppen enthalten den Benutzer (transitiv)?

        Nötig für grosse Tenants: Ist ein Benutzer in mehr als 200 Gruppen, liefert Entra
        im Token keine Gruppenliste mehr, sondern nur einen Verweis ("Overage"). Ohne
        Rückfrage wären dort ausgerechnet die Konten mit vielen Mitgliedschaften — meist
        die Administratoren — von der Anmeldung ausgesperrt.

        Braucht keine zusätzliche Berechtigung: ``GroupMember.Read.All`` ist bereits
        gesetzt, sobald Gruppen konfiguriert sind.
        """
        if not group_ids:
            return set()
        url = f"{self.config.base}/v1.0/users/{user_id}/checkMemberGroups"
        resp = await self._request(self._shared_http(), "POST", url, json={"groupIds": group_ids})
        return {g for g in resp.json().get("value", []) if isinstance(g, str)}

    async def send_mail(
        self,
        *,
        sender: str,
        to: list[str],
        subject: str,
        html_body: str,
        text_body: str | None = None,
        inline_images: list[tuple[str, bytes, str]] | None = None,
    ) -> None:
        """Versand via Graph ``sendMail`` (Mail.Send, im Namen von ``sender``)."""
        message: dict[str, Any] = {
            "message": {
                "subject": subject,
                "body": {"contentType": "HTML", "content": html_body},
                "toRecipients": [{"emailAddress": {"address": addr}} for addr in to],
            },
            "saveToSentItems": False,
        }
        if inline_images:
            message["message"]["attachments"] = [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": f"{cid}{_ext_for(mime)}",
                    "contentType": mime,
                    "contentId": cid,
                    "isInline": True,
                    "contentBytes": base64.b64encode(content).decode(),
                }
                for cid, content, mime in inline_images
            ]
        # Bewusst der geteilte Client: sendMail ist der einzige Aufruf, der pro Lauf
        # hundert- bis tausendfach vorkommt — hier zahlt sich Verbindungs-Pooling aus.
        url = f"{self.config.base}/v1.0/users/{sender}/sendMail"
        await self._request(self._shared_http(), "POST", url, json=message)
