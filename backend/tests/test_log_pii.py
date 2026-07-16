"""Personenbezogene Daten werden in Logs gekürzt.

Logs werden oft zentral gesammelt und lange aufbewahrt — vollständige Mailadressen und
UPNs gehören dort nicht hin. Ganz entfernen wäre aber falsch: Bei einem fehlgeschlagenen
Versand muss man erkennen können, wen es betraf. Deshalb wird gekürzt, nicht gelöscht;
der Klartext steht bei Bedarf im Audit-Protokoll.
"""

from __future__ import annotations

from app.core.logging import _redact, mask_pii


def test_mail_keeps_domain() -> None:
    """Die Domain bleibt lesbar — daran erkennt man den Tenant beim Debuggen."""
    assert mask_pii("pascal.amsler@example.com") == "pa***@example.com"


def test_short_local_part_is_shortened_further() -> None:
    assert mask_pii("ab@example.com") == "a***@example.com"


def test_plain_name_is_masked() -> None:
    assert mask_pii("administrator") == "ad***"


def test_very_short_value_reveals_nothing() -> None:
    assert mask_pii("abc") == "***"


def test_empty_stays_empty() -> None:
    assert mask_pii("") == ""


def test_redact_masks_pii_keys() -> None:
    out = _redact(None, "", {"event": "notify_failed", "upn": "erika.muster@example.com"})
    assert out["upn"] == "er***@example.com"


def test_redact_still_removes_secrets_entirely() -> None:
    """Secrets werden weiterhin ganz entfernt — nicht nur gekürzt."""
    out = _redact(None, "", {"client_secret": "streng-geheim", "token": "abc123"})
    assert out["client_secret"] == "***redacted***"
    assert out["token"] == "***redacted***"


def test_non_pii_fields_are_untouched() -> None:
    out = _redact(None, "", {"event": "run_done", "sent": 42, "status": "success"})
    assert out == {"event": "run_done", "sent": 42, "status": "success"}


def test_non_string_pii_is_left_alone() -> None:
    """Kein Absturz, wenn ein PII-Schlüssel mal keine Zeichenkette trägt."""
    out = _redact(None, "", {"recipient": ["a@b.com", "c@d.com"]})
    assert out["recipient"] == ["a@b.com", "c@d.com"]
