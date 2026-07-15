"""Fehlerantworten dürfen keine eingegebenen Passwörter zurückspiegeln.

Pydantic legt Validierungsfehlern den eingegebenen Wert als ``input`` bei. Bei einem zu
kurzen Passwort stand es damit im Klartext in der 422-Antwort — beim Testen real gesehen:
    {"type":"string_too_short","loc":["body","new_password"],"input":"Neu-222", ...}
Von dort landet es in Proxy-, Browser- oder Monitoring-Logs.
"""

from __future__ import annotations

from app.core.errors import scrub_validation_errors


class FakeExc:
    """Minimaler Ersatz für RequestValidationError — nur `errors()` wird gebraucht."""

    def __init__(self, errors: list[dict[str, object]]) -> None:
        self._errors = errors

    def errors(self) -> list[dict[str, object]]:
        return self._errors


def test_password_input_is_removed() -> None:
    exc = FakeExc(
        [
            {
                "type": "string_too_short",
                "loc": ["body", "new_password"],
                "msg": "String should have at least 10 characters",
                "input": "geheim1",
            }
        ]
    )
    out = scrub_validation_errors(exc)  # type: ignore[arg-type]
    assert "input" not in out[0]
    assert out[0]["msg"] == "String should have at least 10 characters"


def test_all_sensitive_fields_are_scrubbed() -> None:
    felder = ["password", "current_password", "new_password", "client_secret", "code", "token"]
    exc = FakeExc([{"loc": ["body", f], "input": "geheim", "type": "x"} for f in felder])
    for item in scrub_validation_errors(exc):  # type: ignore[arg-type]
        assert "input" not in item


def test_harmless_fields_keep_their_input() -> None:
    """Für normale Felder bleibt der Wert erhalten — das hilft beim Debuggen."""
    exc = FakeExc([{"type": "int_parsing", "loc": ["body", "days_left"], "input": "abc"}])
    out = scrub_validation_errors(exc)  # type: ignore[arg-type]
    assert out[0]["input"] == "abc"


def test_doc_url_is_dropped() -> None:
    exc = FakeExc(
        [{"type": "x", "loc": ["body", "days_left"], "url": "https://errors.pydantic.dev"}]
    )
    assert "url" not in scrub_validation_errors(exc)[0]  # type: ignore[arg-type]
