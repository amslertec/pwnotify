"""M9: PIL image decode sites must reject decompression bombs.

Three call sites decode client-supplied bytes with `Image.open` and never set
`Image.MAX_IMAGE_PIXELS`: `branding._autotrim`, `auth._process_avatar`,
`entra_avatar._process_avatar`. A small file that declares an enormous pixel area can force
Pillow to allocate a huge in-memory bitmap on decode (RAM exhaustion) -- Pillow's built-in
decompression-bomb guard is a no-op unless `Image.MAX_IMAGE_PIXELS` is actually set low
enough to catch it.

Each function reads its own module-level `_MAX_IMAGE_PIXELS` constant and assigns it to
`Image.MAX_IMAGE_PIXELS` right before decoding -- monkeypatching that constant down lets
this test prove the guard is wired up with a cheap, tiny image instead of allocating a real
multi-megapixel fixture.
"""

from __future__ import annotations

import io

import pytest
from app.api.routes import auth as auth_routes
from app.api.routes import branding
from app.services import entra_avatar
from PIL import Image

_TARGETS = [
    pytest.param(branding, "_MAX_IMAGE_PIXELS", branding._autotrim, id="branding._autotrim"),
    pytest.param(
        auth_routes, "_MAX_IMAGE_PIXELS", auth_routes._process_avatar, id="auth._process_avatar"
    ),
    pytest.param(
        entra_avatar,
        "_MAX_IMAGE_PIXELS",
        entra_avatar._process_avatar,
        id="entra_avatar._process_avatar",
    ),
]


def _tiny_png(width: int, height: int) -> bytes:
    img = Image.new("RGB", (width, height), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.parametrize("module,attr,func", _TARGETS)
def test_decode_rejects_image_over_pixel_cap(
    module: object, attr: str, func: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force a tiny cap so a cheap 50x50 image (2500 px) already exceeds Pillow's hard-error
    # threshold (2x the cap) -- no need to allocate a real multi-megapixel fixture.
    monkeypatch.setattr(module, attr, 100, raising=False)
    data = _tiny_png(50, 50)
    assert func(data) is None, (  # type: ignore[operator]
        "Decoded a file whose declared pixel area exceeds the configured cap -- "
        "Image.MAX_IMAGE_PIXELS is not being enforced at this decode site"
    )


def test_decode_accepts_image_within_pixel_cap() -> None:
    """Non-vacuous control: an ordinary small logo/avatar still decodes fine -- the guard
    rejects bombs only, not legitimate uploads."""
    data = _tiny_png(50, 50)
    assert branding._autotrim(data) is not None
    assert auth_routes._process_avatar(data) is not None
    assert entra_avatar._process_avatar(data) is not None
