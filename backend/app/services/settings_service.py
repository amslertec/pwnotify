"""Laden/Speichern der DB-Einstellungen inkl. Secret-Handling und Masking."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import get_settings
from ..core.crypto import decrypt, encrypt
from ..models._base import utcnow
from ..models.setting import Setting
from .settings_schema import MASK, SECRET_KEYS, SETTINGS, default_settings


def effective_base_url(settings: dict[str, Any]) -> str:
    """Öffentliche Basis-URL: DB-Setting ``app.public_url`` vor ENV ``PWNOTIFY_BASE_URL``."""
    return str(settings.get("app.public_url") or get_settings().base_url).rstrip("/")


class SettingsService:
    """Alle Zugriffe auf die laufende Konfiguration laufen hierüber."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_all(self) -> dict[str, Any]:
        """Effektive Konfiguration: Defaults überlagert von DB-Werten (Secrets entschlüsselt)."""
        result = default_settings()
        rows = (await self.session.execute(select(Setting))).scalars().all()
        for row in rows:
            if row.key not in SETTINGS:
                continue
            value = row.value
            if row.is_secret and isinstance(value, str) and value:
                try:
                    value = decrypt(value)
                except ValueError:
                    value = ""
            result[row.key] = value
        return result

    async def get(self, key: str) -> Any:
        return (await self.get_all()).get(key)

    async def get_public(self) -> dict[str, Any]:
        """Wie get_all(), aber Secrets werden maskiert (nie im Klartext ans Frontend)."""
        data = await self.get_all()
        for key in SECRET_KEYS:
            data[key] = MASK if data.get(key) else ""
        return data

    async def set_many(self, values: dict[str, Any]) -> None:
        """Setzt mehrere Keys. Für Secrets: MASK/None -> unverändert lassen."""
        for key, value in values.items():
            if key not in SETTINGS:
                continue
            spec = SETTINGS[key]
            if spec.secret:
                # Masken-Marker oder None bedeutet: bestehenden Wert nicht überschreiben.
                if value in (MASK, None, ""):
                    continue
                value = encrypt(str(value))
            await self._upsert(key, value, spec.secret)
        await self.session.commit()

    async def set(self, key: str, value: Any) -> None:
        await self.set_many({key: value})

    async def _upsert(self, key: str, value: Any, is_secret: bool) -> None:
        now = utcnow()
        stmt = (
            pg_insert(Setting)
            .values(key=key, value=value, is_secret=is_secret, updated_at=now)
            .on_conflict_do_update(
                index_elements=[Setting.key],
                set_={"value": value, "is_secret": is_secret, "updated_at": now},
            )
        )
        await self.session.execute(stmt)

    async def is_configured(self, *keys: str) -> bool:
        data = await self.get_all()
        return all(bool(data.get(k)) for k in keys)

    async def has_any(self) -> bool:
        """True, sobald mindestens ein Setting-Row existiert (Seed bereits gelaufen)."""
        row = (await self.session.execute(select(Setting.key).limit(1))).first()
        return row is not None
