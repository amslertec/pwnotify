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


UPN = "user@contoso.com"


def test_upn_fallback_used_when_no_mailbox() -> None:
    # No mailbox, no alternates, fallback on: UPN becomes the primary recipient.
    assert resolve_recipients("primary", None, [], upn=UPN, upn_fallback=True) == ([UPN], "primary")


def test_upn_fallback_ignored_when_mailbox_present() -> None:
    # A real mailbox always wins over the UPN.
    assert resolve_recipients("primary", PRIMARY, [], upn=UPN, upn_fallback=True) == (
        [PRIMARY],
        "primary",
    )


def test_upn_fallback_off_yields_empty() -> None:
    # Fallback off (default): a mailbox-less account stays without a recipient.
    assert resolve_recipients("primary", None, [], upn=UPN, upn_fallback=False) == ([], "primary")


def test_upn_fallback_feeds_both_and_alternate_fallback() -> None:
    # The UPN-derived primary also flows into `both` and `alternate_fallback_primary`.
    assert resolve_recipients("both", None, [], upn=UPN, upn_fallback=True) == ([UPN], "both")
    assert resolve_recipients(
        "alternate_fallback_primary", None, [], upn=UPN, upn_fallback=True
    ) == ([UPN], "primary")
