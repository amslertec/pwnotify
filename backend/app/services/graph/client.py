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
        self._app = msal.ConfidentialClientApplication(
            config.client_id,
            authority=config.authority,
            client_credential=config.client_secret,
        )
        self._token: str | None = None
        self._token_exp: dt.datetime = dt.datetime.min.replace(tzinfo=dt.UTC)

    # -- Auth ---------------------------------------------------------------- #
    async def _acquire_token(self) -> str:
        now = dt.datetime.now(dt.UTC)
        if self._token and now < self._token_exp - dt.timedelta(minutes=2):
            return self._token
        result = await asyncio.to_thread(
            self._app.acquire_token_for_client, scopes=[self.config.scope]
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
    async def test_connection(self) -> GraphConnectionResult:
        try:
            token = await self._acquire_token()
        except GraphError as exc:
            return GraphConnectionResult(connected=False, error=str(exc))
        granted = self._roles_from_token(token)
        missing = [p for p in REQUIRED_PERMISSIONS if p not in granted]
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
                url = data.get("@odata.nextLink", "")
        return default_validity, by_domain

    async def iter_users(self) -> AsyncIterator[dict[str, Any]]:
        """Alle Benutzer (paginiert, minimales $select)."""
        async with httpx.AsyncClient(timeout=60) as client:
            url = f"{self.config.base}/v1.0/users?$select={USER_SELECT}&$top=999"
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for user in data.get("value", []):
                    yield user
                url = data.get("@odata.nextLink", "")

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
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for user in data.get("value", []):
                    yield user
                url = data.get("@odata.nextLink", "")

    async def get_group_members(self, group_id: str) -> list[dict[str, Any]]:
        """Benutzer-Mitglieder einer Gruppe (id, upn, displayName, accountEnabled)."""
        members: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            url = (
                f"{self.config.base}/v1.0/groups/{group_id}/transitiveMembers/"
                "microsoft.graph.user"
                "?$select=id,userPrincipalName,displayName,mail,accountEnabled&$top=999"
            )
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                members.extend(data.get("value", []))
                url = data.get("@odata.nextLink", "")
        return members

    async def get_group_member_ids(self, group_id: str) -> set[str]:
        """Transitive Mitglieder-IDs einer Gruppe (für Exclude-Gruppen)."""
        ids: set[str] = set()
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.config.base}/v1.0/groups/{group_id}/transitiveMembers?$select=id&$top=999"
            while url:
                resp = await self._request(client, "GET", url)
                data = resp.json()
                for member in data.get("value", []):
                    if "id" in member:
                        ids.add(member["id"])
                url = data.get("@odata.nextLink", "")
        return ids

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
        async with httpx.AsyncClient(timeout=30) as client:
            url = f"{self.config.base}/v1.0/users/{sender}/sendMail"
            await self._request(client, "POST", url, json=message)
