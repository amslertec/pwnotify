"""secret.key must be created without a world/group-readable window (L4).

The old implementation wrote the key with the default umask-derived mode and only
tightened it afterwards with a separate ``chmod`` call -- a brief window where the
file (and its parent directory) could be readable by other local users/processes.
These tests exercise the auto-generation path directly, pointing Settings at a
throwaway directory so the real ``{data_dir}/secret.key`` is never touched.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from app.core.config import get_settings
from app.core.crypto import resolve_secret_keys


@pytest.fixture
def empty_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Settings pointed at a fresh, not-yet-existing data dir with no configured
    secret key, so ``resolve_secret_keys()`` takes the file auto-generation path."""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("PWNOTIFY_SECRET_KEY", "")
    monkeypatch.setenv("PWNOTIFY_DATA_DIR", str(data_dir))
    get_settings.cache_clear()
    yield data_dir
    get_settings.cache_clear()


def test_key_file_created_with_mode_0600(empty_data_dir: Path) -> None:
    resolve_secret_keys()
    key_path = empty_data_dir / "secret.key"
    assert key_path.exists()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_key_dir_created_without_group_or_other_bits(empty_data_dir: Path) -> None:
    resolve_secret_keys()
    mode = stat.S_IMODE(empty_data_dir.stat().st_mode)
    assert mode & 0o077 == 0


def test_key_stays_0600_under_permissive_umask(empty_data_dir: Path) -> None:
    """Even with a permissive process umask, the key must never end up
    world/group readable -- the explicit final ``chmod`` is belt-and-suspenders,
    the creation mode itself must already be restrictive."""
    old_umask = os.umask(0)
    try:
        resolve_secret_keys()
    finally:
        os.umask(old_umask)
    key_path = empty_data_dir / "secret.key"
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_existing_key_is_not_clobbered(empty_data_dir: Path) -> None:
    """If the file already exists (e.g. a parallel first-start won the race),
    the create-exclusive path must not overwrite it -- the pre-existing key is
    read back and returned unchanged."""
    empty_data_dir.mkdir(parents=True, exist_ok=True)
    key_path = empty_data_dir / "secret.key"
    existing = b"already-there-not-a-real-fernet-key"
    key_path.write_bytes(existing)
    os.chmod(key_path, 0o600)

    keys = resolve_secret_keys()

    assert keys == [existing]
    assert key_path.read_bytes() == existing
