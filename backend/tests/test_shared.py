"""Tests der Shared-Mailbox-Erkennung."""

from __future__ import annotations

from app.services.graph.sync import detect_shared_mailbox

PATTERNS = ["noreply@*", "info@*"]


def _raw(licenses: list[dict] | None) -> dict:
    return {"assignedLicenses": licenses or []}


def test_mailbox_without_license_is_shared() -> None:
    assert detect_shared_mailbox(
        _raw([]), "home@x.ch", "home@x.ch", patterns=[], detect_unlicensed=True
    )


def test_licensed_user_is_not_shared() -> None:
    assert not detect_shared_mailbox(
        _raw([{"skuId": "abc"}]), "pascal@x.ch", "pascal@x.ch", patterns=[], detect_unlicensed=True
    )


def test_no_mailbox_is_not_shared() -> None:
    # kein Postfach (mail=None) -> nicht als Shared markieren
    assert not detect_shared_mailbox(
        _raw([]), "svc@x.ch", None, patterns=[], detect_unlicensed=True
    )


def test_detection_can_be_disabled() -> None:
    assert not detect_shared_mailbox(
        _raw([]), "home@x.ch", "home@x.ch", patterns=[], detect_unlicensed=False
    )


def test_pattern_override_still_works() -> None:
    # trotz Lizenz per Muster als Shared markiert
    assert detect_shared_mailbox(
        _raw([{"skuId": "abc"}]),
        "noreply@x.ch",
        "noreply@x.ch",
        patterns=PATTERNS,
        detect_unlicensed=True,
    )
