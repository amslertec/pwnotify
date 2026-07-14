"""Alembic-Migrationen programmatisch anwenden (Entrypoint + Setup-Wizard).

Aus dem laufenden Event-Loop via ``asyncio.to_thread(run_migrations)`` aufrufen —
env.py nutzt ``asyncio.run`` und darf daher nicht in einem aktiven Loop laufen.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _config() -> Config:
    cfg = Config(str(_BACKEND_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_BACKEND_ROOT / "alembic"))
    return cfg


def run_migrations() -> None:
    """Wendet alle ausstehenden Migrationen an (idempotent)."""
    command.upgrade(_config(), "head")
