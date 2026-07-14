"""Tests der Empfänger-Strategie."""

from __future__ import annotations

from app.services.recipients import resolve_recipients

PRIMARY = "user@example.com"
OTHERS = ["private@gmail.com", "backup@example.org"]


def test_primary() -> None:
    assert resolve_recipients("primary", PRIMARY, OTHERS) == ([PRIMARY], "primary")


def test_alternate() -> None:
    assert resolve_recipients("alternate", PRIMARY, OTHERS) == (OTHERS, "alternate")


def test_both_merges_and_dedupes() -> None:
    addrs, channel = resolve_recipients("both", PRIMARY, [PRIMARY, *OTHERS])
    assert channel == "both"
    assert addrs == [PRIMARY, *OTHERS]  # PRIMARY nicht doppelt


def test_alternate_fallback_uses_alternate_when_present() -> None:
    assert resolve_recipients("alternate_fallback_primary", PRIMARY, OTHERS) == (
        OTHERS,
        "alternate",
    )


def test_alternate_fallback_falls_back_to_primary() -> None:
    assert resolve_recipients("alternate_fallback_primary", PRIMARY, []) == ([PRIMARY], "primary")


def test_no_primary_mail_yields_empty() -> None:
    assert resolve_recipients("primary", None, OTHERS) == ([], "primary")
