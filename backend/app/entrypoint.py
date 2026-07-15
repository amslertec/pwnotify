"""Container-Entrypoint: startet Uvicorn (Single-Worker) bzw. `migrate`.

Single-Worker ist bewusst gewählt: der APScheduler läuft im App-Prozess; mehrere
Worker würden Jobs mehrfach ausführen. Migrationen/Seed laufen im Lifespan.
"""

from __future__ import annotations

import sys

import uvicorn

from .core.config import get_settings
from .core.logging import configure_logging, get_logger


def main() -> None:
    configure_logging()
    log = get_logger("entrypoint")

    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        from .db.migrate import run_migrations

        run_migrations()
        log.info("migrations_applied")
        return

    settings = get_settings()
    log.info("starting_uvicorn", port=settings.port, trusted_proxies=settings.trusted_proxies)
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=settings.port,
        workers=1,
        log_config=None,  # structlog übernimmt das Logging
        access_log=False,
        proxy_headers=True,
        # Nur diesen Peers wird X-Forwarded-For geglaubt — sonst könnte jeder Client
        # seine Herkunfts-IP fälschen und damit Rate-Limit und Lockout aushebeln.
        forwarded_allow_ips=settings.trusted_proxies,
    )


if __name__ == "__main__":
    main()
