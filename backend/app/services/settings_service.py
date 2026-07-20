"""Loading/saving the DB settings incl. secret handling and masking."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.crypto import decrypt, encrypt
from ..core.logging import get_logger
from ..db.tenant_context import current_tenant_or_none
from ..models._base import utcnow
from ..models.setting import Setting
from ..repositories import tenant_repo
from .settings_schema import MASK, SECRET_KEYS, SETTINGS, default_settings
from .settings_validators import check_smtp_tls_allowed

log = get_logger("settings")


def effective_base_url(settings: dict[str, Any]) -> str:
    """Public base URL: DB setting ``app.public_url`` before ENV ``PWNOTIFY_BASE_URL``."""
    return str(settings.get("app.public_url") or get_settings().base_url).rstrip("/")


class SettingsService:
    """All access to the running configuration goes through here."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self) -> dict[str, Any]:
        """Effective configuration: defaults overlaid with DB values (secrets decrypted).

        Explicitly scoped to a single tenant -- the active one if the caller runs inside
        `tenant_scoped_session`/`use_tenant`, otherwise the default tenant (owner session
        with no context, e.g. `version.py`, `setup.py`, `auth.auth_config`). Without this
        filter an owner-session read of `select(Setting)` sees every tenant's rows (RLS is
        bypassed by ownership) and folds them into one dict, last-wins on the shared `key`
        -- and decrypts every tenant's secrets along the way.
        """
        result = default_settings()
        tid = current_tenant_or_none()
        if tid is None:
            # Owner session without an active tenant: only the owner role reads `tenant`
            # here (a runtime/tenant-scoped session always has `current_tenant_or_none()`
            # set by `tenant_scoped_session`, so this branch never runs there).
            tid = (await tenant_repo.default_tenant(self.session)).id
        rows = (
            (await self.session.execute(select(Setting).where(Setting.tenant_id == tid)))
            .scalars()
            .all()
        )
        for row in rows:
            if row.key not in SETTINGS:
                continue
            value = row.value
            if row.is_secret and isinstance(value, str) and value:
                try:
                    value = decrypt(value)
                except ValueError:
                    # Undecryptable almost always means: wrong or lost Fernet key
                    # (/data/secret.key gone, PWNOTIFY_SECRET_KEY changed).
                    # The empty value makes the secret look "never configured" —
                    # without this log entry, the outage gets debugged in the wrong place.
                    log.error(
                        "secret_decrypt_failed",
                        key=row.key,
                        hint=(
                            "Fernet-Key passt nicht zum verschlüsselten Wert. "
                            "PWNOTIFY_SECRET_KEY bzw. /data/secret.key prüfen — sonst muss "
                            "das Secret in den Einstellungen neu gesetzt werden."
                        ),
                    )
                    value = ""
            result[row.key] = value
        return result

    async def get(self, key: str) -> Any:
        return (await self.get_all()).get(key)

    async def get_public(self) -> dict[str, Any]:
        """Like get_all(), but secrets are masked (never sent to the frontend in plaintext)."""
        data = await self.get_all()
        for key in SECRET_KEYS:
            data[key] = MASK if data.get(key) else ""
        return data

    async def set_many(self, values: dict[str, Any]) -> None:
        """Persist multiple keys. Secrets: MASK/None/"" means "leave unchanged".

        All values are validated up front: a single invalid value aborts the whole batch
        with a ValidationError (HTTP 400) before anything is written.
        """
        prepared: list[tuple[str, Any, bool]] = []
        for key, value in values.items():
            if key not in SETTINGS:
                continue
            spec = SETTINGS[key]
            if spec.secret and value in (MASK, None, ""):
                # Mask marker or None means: leave the existing value unchanged.
                continue
            if spec.validate is not None:
                value = spec.validate(value)
            if spec.secret:
                value = encrypt(str(value))
            prepared.append((key, value, spec.secret))
        # A6 cross-key check: plaintext SMTP (tls=none) only to an internal relay. A single-key
        # validator cannot see both keys, and a PUT may change only one of them -- so merge the
        # batch with the persisted state to know the EFFECTIVE host/tls, then enforce here,
        # before anything is written. Only pay the get_all() cost when a mail key is in play.
        if "mail.smtp_tls" in values or "mail.smtp_host" in values:
            current = await self.get_all()
            eff_host = values.get("mail.smtp_host", current.get("mail.smtp_host"))
            eff_tls = values.get("mail.smtp_tls", current.get("mail.smtp_tls"))
            check_smtp_tls_allowed(eff_host, eff_tls)
        for key, value, is_secret in prepared:
            await self._upsert(key, value, is_secret)
        await self.session.commit()

    async def set(self, key: str, value: Any) -> None:
        await self.set_many({key: value})

    async def _upsert(self, key: str, value: Any, is_secret: bool) -> None:
        now = utcnow()
        # tenant_id explicitly from the active tenant context: this is a core `pg_insert`,
        # which (unlike `session.add(Setting(...))`) does NOT go through the ORM
        # `default_factory` -- without this value, the NOT-NULL column would fail since the
        # Phase-1 server_default was dropped. Full tenant scoping of callers (routes,
        # scheduler) follows in Task 3/4; this fix keeps the writer itself functional.
        stmt = (
            pg_insert(Setting)
            .values(
                tenant_id=current_tenant_or_none(),
                key=key,
                value=value,
                is_secret=is_secret,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[Setting.tenant_id, Setting.key],
                set_={"value": value, "is_secret": is_secret, "updated_at": now},
            )
        )
        await self.session.execute(stmt)

    async def is_configured(self, *keys: str) -> bool:
        data = await self.get_all()
        return all(bool(data.get(k)) for k in keys)

    async def has_any(self) -> bool:
        """True as soon as at least one setting row exists (seed has already run)."""
        row = (await self.session.execute(select(Setting.key).limit(1))).first()
        return row is not None
