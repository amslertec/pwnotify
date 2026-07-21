"""Empfänger-Auflösung nach Strategie (rein, unit-testbar).

Strategien: primary | alternate | both | alternate_fallback_primary.
``both`` erzeugt EINE Mail an mehrere Adressen (nicht mehrere Mails), damit der
Dedup-Constraint (User, Stufe, Zyklus) unverletzt bleibt.
"""

from __future__ import annotations


def resolve_recipients(
    strategy: str,
    primary_mail: str | None,
    other_mails: list[str],
    *,
    upn: str | None = None,
    upn_fallback: bool = False,
) -> tuple[list[str], str]:
    """Gibt (Adressliste, Kanal-Label) zurück. Kanal: primary | alternate | both.

    ``upn_fallback`` (Opt-in): hat ein Konto kein Postfach (``primary_mail`` leer), wird der
    ``upn`` als Primäradresse genutzt -- für Tenants, in denen der UPN die Mailadresse ist.
    Ohne den Fallback bleiben solche Konten ohne Empfänger (und werden nicht benachrichtigt).
    """
    effective_primary = primary_mail or (upn if upn_fallback else None)
    primary = [effective_primary] if effective_primary else []
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
