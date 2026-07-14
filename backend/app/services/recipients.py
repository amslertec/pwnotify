"""Empfänger-Auflösung nach Strategie (rein, unit-testbar).

Strategien: primary | alternate | both | alternate_fallback_primary.
``both`` erzeugt EINE Mail an mehrere Adressen (nicht mehrere Mails), damit der
Dedup-Constraint (User, Stufe, Zyklus) unverletzt bleibt.
"""

from __future__ import annotations


def resolve_recipients(
    strategy: str, primary_mail: str | None, other_mails: list[str]
) -> tuple[list[str], str]:
    """Gibt (Adressliste, Kanal-Label) zurück. Kanal: primary | alternate | both."""
    primary = [primary_mail] if primary_mail else []
    others = [m for m in dict.fromkeys(other_mails) if m]  # dedupe, Reihenfolge erhalten

    if strategy == "alternate":
        return others, "alternate"
    if strategy == "both":
        merged = list(dict.fromkeys([*primary, *others]))
        return merged, "both"
    if strategy == "alternate_fallback_primary":
        return (others, "alternate") if others else (primary, "primary")
    # default: primary
    return primary, "primary"
