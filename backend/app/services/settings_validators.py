"""Reusable value validators for the settings registry.

A validator takes the raw value a client wants to persist and either returns a (possibly
normalised) value or raises :class:`ValidationError` (HTTP 400). Specs in
``settings_schema`` opt in via ``SettingSpec(default, validate=...)``; keys without a
validator keep their previous free-form behaviour (backwards compatible).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..core.errors import ValidationError


def number_range(
    *,
    min_value: float | None = None,
    max_value: float | None = None,
    exclusive_min: bool = False,
    integer_only: bool = False,
    allow_none: bool = False,
    message: str | None = None,
) -> Callable[[Any], Any]:
    """Build a validator enforcing a numeric range.

    ``min_value``/``max_value`` are inclusive unless ``exclusive_min`` makes the lower
    bound strict. ``integer_only`` rejects fractional values. ``allow_none`` permits an
    unset value. The original value is returned unchanged on success so the stored JSON
    keeps its input type.
    """

    def _validate(value: Any) -> Any:
        if value is None:
            if allow_none:
                return None
            raise ValidationError(message or "A value is required.")
        if isinstance(value, bool):
            # bool is an int subclass but never a meaningful numeric setting here.
            raise ValidationError(message or f"Expected a number, got {value!r}.")
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(message or f"Expected a number, got {value!r}.") from exc
        if integer_only and not float(num).is_integer():
            raise ValidationError(message or f"Expected a whole number, got {value!r}.")
        if min_value is not None:
            if exclusive_min and num <= min_value:
                raise ValidationError(message or f"Value must be greater than {min_value}.")
            if not exclusive_min and num < min_value:
                raise ValidationError(message or f"Value must be >= {min_value}.")
        if max_value is not None and num > max_value:
            raise ValidationError(message or f"Value must be <= {max_value}.")
        return value

    return _validate
