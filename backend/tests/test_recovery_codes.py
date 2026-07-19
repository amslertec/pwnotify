"""Recovery-Codes: höhere Entropie (80 bit) + Argon2id-Speicher-Hash, Legacy-koexistent.

Vorher: `generate_recovery_codes` erzeugte nur 48 bit Entropie (3 Gruppen `token_hex(2)`)
und speicherte unsalted SHA-256 (`sha256(code).hexdigest()`), obwohl alles andere
(Passwörter, 2FA-Secret-Crypto) Argon2id/Fernet nutzt. Recovery-Codes lassen sich nicht neu
ableiten, daher müssen bestehende SHA-256-Hashes weiter verifizierbar bleiben, bis der
Nutzer 2FA neu einrichtet — `match_recovery_code` muss also BEIDE Formate erkennen.
"""

from __future__ import annotations

import hashlib

from app.core.twofa import generate_recovery_codes, match_recovery_code


def test_generated_codes_have_five_groups_of_four_hex_chars() -> None:
    """80-bit-Format: 5 Gruppen à token_hex(2) (4 Hex-Zeichen), statt vorher 3 Gruppen."""
    codes, _hashes = generate_recovery_codes()
    assert len(codes) == 10
    for code in codes:
        groups = code.split("-")
        assert len(groups) == 5
        for g in groups:
            assert len(g) == 4
            int(g, 16)  # muss gültiges Hex sein


def test_stored_hashes_are_argon2id() -> None:
    """Speicher-Hashes müssen Argon2id sein, keine unsalted-SHA-256-Hex-Strings mehr."""
    _codes, hashes = generate_recovery_codes()
    assert len(hashes) == 10
    for h in hashes:
        assert h.startswith("$argon2id$")


def test_generated_code_matches_its_own_hash() -> None:
    codes, hashes = generate_recovery_codes()
    for code, h in zip(codes, hashes, strict=True):
        matched = match_recovery_code(code, [h])
        assert matched == h


def test_wrong_code_does_not_match() -> None:
    codes, hashes = generate_recovery_codes()
    assert match_recovery_code("ffff-ffff-ffff-ffff-ffff", [hashes[0]]) is None
    # Ein fremder gültiger Code darf nicht gegen einen anderen Hash matchen.
    assert match_recovery_code(codes[1], [hashes[0]]) is None


def test_legacy_sha256_hash_still_verifies() -> None:
    """Kernstück: bestehende SHA-256-Recovery-Codes (vor diesem Fix erzeugt) dürfen nicht
    ungültig werden — eine reine Argon2-Implementierung könnte das NICHT leisten."""
    legacy_plain = "aa-bb-cc"
    legacy_hash = hashlib.sha256(legacy_plain.encode()).hexdigest()
    matched = match_recovery_code(legacy_plain, [legacy_hash])
    assert matched == legacy_hash


def test_legacy_and_new_format_coexist_in_same_list() -> None:
    """Ein Nutzer mit gemischter Liste (alte SHA-256- + neue Argon2-Codes) muss beide
    einlösen können."""
    legacy_plain = "dd-ee-ff"
    legacy_hash = hashlib.sha256(legacy_plain.encode()).hexdigest()
    new_codes, new_hashes = generate_recovery_codes(n=1)
    mixed = [legacy_hash, *new_hashes]

    assert match_recovery_code(legacy_plain, mixed) == legacy_hash
    assert match_recovery_code(new_codes[0], mixed) == new_hashes[0]


def test_wrong_legacy_style_code_does_not_match() -> None:
    legacy_hash = hashlib.sha256(b"aa-bb-cc").hexdigest()
    assert match_recovery_code("aa-bb-cd", [legacy_hash]) is None


def test_match_is_case_insensitive() -> None:
    codes, hashes = generate_recovery_codes(n=1)
    assert match_recovery_code(codes[0].upper(), hashes) == hashes[0]

    legacy_plain = "aa-bb-cc"
    legacy_hash = hashlib.sha256(legacy_plain.encode()).hexdigest()
    assert match_recovery_code(legacy_plain.upper(), [legacy_hash]) == legacy_hash
