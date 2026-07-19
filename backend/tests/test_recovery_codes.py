"""Recovery codes: higher entropy (80 bit) + Argon2id storage hash, legacy-coexistent.

Before: `generate_recovery_codes` only produced 48 bits of entropy (3 groups of
`token_hex(2)`) and stored an unsalted SHA-256 hash (`sha256(code).hexdigest()`),
even though everything else (passwords, 2FA secret crypto) uses Argon2id/Fernet.
Recovery codes can't be re-derived, so existing SHA-256 hashes must stay verifiable
until the user re-enrolls in 2FA — `match_recovery_code` therefore has to recognize
BOTH formats.
"""

from __future__ import annotations

import hashlib

from app.core.twofa import generate_recovery_codes, match_recovery_code


def test_generated_codes_have_five_groups_of_four_hex_chars() -> None:
    """80-bit format: 5 groups of token_hex(2) (4 hex chars each), instead of 3 groups before."""
    codes, _hashes = generate_recovery_codes()
    assert len(codes) == 10
    for code in codes:
        groups = code.split("-")
        assert len(groups) == 5
        for g in groups:
            assert len(g) == 4
            int(g, 16)  # must be valid hex


def test_stored_hashes_are_argon2id() -> None:
    """Stored hashes must be Argon2id, no longer unsalted SHA-256 hex strings."""
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
    # A different valid code must not match against another hash.
    assert match_recovery_code(codes[1], [hashes[0]]) is None


def test_legacy_sha256_hash_still_verifies() -> None:
    """Core case: existing SHA-256 recovery codes (generated before this fix) must not
    become invalid — a pure Argon2 implementation could NOT achieve this."""
    legacy_plain = "aa-bb-cc"
    legacy_hash = hashlib.sha256(legacy_plain.encode()).hexdigest()
    matched = match_recovery_code(legacy_plain, [legacy_hash])
    assert matched == legacy_hash


def test_legacy_and_new_format_coexist_in_same_list() -> None:
    """A user with a mixed list (old SHA-256 + new Argon2 codes) must be able to
    redeem both."""
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
