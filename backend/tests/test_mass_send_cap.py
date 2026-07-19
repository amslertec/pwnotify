"""M8: the mass-send brake must not be ratio-only nor switch-off-able.

(a) schedule.max_notify_ratio no longer accepts 0 (would disable the ratio brake) and is
    bounded to (0, 1]; (b) an absolute cap (schedule.max_notify_count) blocks a run over the
    ceiling even when the ratio would allow it.
"""

from __future__ import annotations

import pytest
from app.core.errors import ValidationError
from app.services.runner import mass_send_blocked_reason
from app.services.settings_schema import SETTINGS


# --- (a) ratio + count validators ----------------------------------------------- #
def test_ratio_zero_is_rejected() -> None:
    validate = SETTINGS["schedule.max_notify_ratio"].validate
    assert validate is not None
    with pytest.raises(ValidationError) as ei:
        validate(0)
    assert ei.value.status_code == 400


def test_ratio_above_one_is_rejected() -> None:
    validate = SETTINGS["schedule.max_notify_ratio"].validate
    with pytest.raises(ValidationError):
        validate(1.5)


def test_ratio_valid_value_passes() -> None:
    validate = SETTINGS["schedule.max_notify_ratio"].validate
    assert validate(0.5) == 0.5


def test_max_count_zero_is_rejected() -> None:
    validate = SETTINGS["schedule.max_notify_count"].validate
    assert validate is not None
    with pytest.raises(ValidationError):
        validate(0)


def test_max_count_valid_value_passes() -> None:
    validate = SETTINGS["schedule.max_notify_count"].validate
    assert validate(500) == 500


# --- (b) absolute cap ----------------------------------------------------------- #
def test_absolute_cap_blocks_even_when_ratio_is_fine() -> None:
    # 600 of 100000 = 0.6% -> well below the 50% ratio, but over the absolute cap of 500.
    reason = mass_send_blocked_reason(due=600, checked=100_000, max_ratio=0.5, max_count=500)
    assert reason is not None
    assert "500" in reason


def test_absolute_cap_allows_run_below_ceiling() -> None:
    assert mass_send_blocked_reason(due=100, checked=100_000, max_ratio=0.5, max_count=500) is None
