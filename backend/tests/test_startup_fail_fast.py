"""M9: the app must fail fast at startup when the mandatory runtime DB password is unset.

`settings.runtime_database_url` refuses to fall back to the superuser DSN (see
`config.py`), but nothing in `lifespan` touched it -- so a container missing
`PWNOTIFY_RUNTIME_DB_PASSWORD` started up "healthy" (/health 200, Docker HEALTHY) while
every tenant-scoped request 500'd. The fix touches the property early in `lifespan`, turning
a silent partial outage into a loud, immediate startup failure.
"""

from __future__ import annotations

import pytest
from app import main
from app.core.config import Settings


async def test_startup_fails_fast_when_runtime_db_password_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PWNOTIFY_RUNTIME_DB_PASSWORD", raising=False)
    broken = Settings(_env_file=None)  # type: ignore[call-arg]
    assert broken.runtime_db_password is None
    monkeypatch.setattr(main, "get_settings", lambda: broken)

    # If the fail-fast touch is missing, startup would proceed here instead of raising -- make
    # that loudly wrong rather than silently running migrations against the real DB.
    def _boom() -> None:
        raise AssertionError("startup proceeded past the runtime-DB check without failing fast")

    monkeypatch.setattr(main, "run_migrations", _boom)

    with pytest.raises(RuntimeError, match="PWNOTIFY_RUNTIME_DB_PASSWORD"):
        async with main.lifespan(main.app):
            pass
