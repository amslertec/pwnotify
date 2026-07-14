"""Tests der reinen Ablaufberechnung und Reminder-Staffelung."""

from __future__ import annotations

import datetime as dt

from app.services.expiry import compute_expiry, due_reminder_stage

NOW = dt.datetime(2026, 7, 13, 8, 0, tzinfo=dt.UTC)


def test_expires_in_10_days() -> None:
    r = compute_expiry(
        last_password_change=NOW - dt.timedelta(days=80),
        validity_days=90,
        password_policies=None,
        now=NOW,
    )
    assert r.never_expires is False
    assert r.days_left == 10
    assert r.expiry_date == (NOW - dt.timedelta(days=80)) + dt.timedelta(days=90)
    assert r.cycle == r.expiry_date.date().isoformat()


def test_already_expired_is_negative() -> None:
    r = compute_expiry(
        last_password_change=NOW - dt.timedelta(days=100),
        validity_days=90,
        password_policies=None,
        now=NOW,
    )
    assert r.days_left == -10


def test_disable_expiration_policy_never_expires() -> None:
    r = compute_expiry(
        last_password_change=NOW - dt.timedelta(days=400),
        validity_days=90,
        password_policies="DisablePasswordExpiration",
        now=NOW,
    )
    assert r.never_expires is True
    assert r.expiry_date is None and r.days_left is None and r.cycle is None


def test_zero_or_missing_validity_never_expires() -> None:
    for vd in (None, 0):
        r = compute_expiry(
            last_password_change=NOW, validity_days=vd, password_policies=None, now=NOW
        )
        assert r.never_expires is True


def test_missing_last_change_is_unknown_not_never() -> None:
    r = compute_expiry(last_password_change=None, validity_days=90, password_policies=None, now=NOW)
    assert r.never_expires is False
    assert r.days_left is None and r.expiry_date is None


def test_naive_datetime_treated_as_utc() -> None:
    r = compute_expiry(
        last_password_change=dt.datetime(2026, 4, 14, 8, 0),  # naiv
        validity_days=90,
        password_policies=None,
        now=NOW,
    )
    assert r.days_left == 0  # 2026-04-14 + 90 = 2026-07-13


class TestDueReminderStage:
    days = [14, 7, 3, 1, 0]

    def test_picks_largest_threshold_at_or_above_days_left(self) -> None:
        assert due_reminder_stage(days_left=10, reminder_days=self.days, already_sent=set()) == 14

    def test_exact_threshold(self) -> None:
        assert due_reminder_stage(days_left=7, reminder_days=self.days, already_sent=set()) == 7

    def test_skips_already_sent_stage(self) -> None:
        # 14 schon gesendet, 10 Resttage -> nichts fällig (7 erst ab <=7)
        assert due_reminder_stage(days_left=10, reminder_days=self.days, already_sent={14}) is None

    def test_catch_up_after_downtime_sends_one(self) -> None:
        # Lauf bei 5 Resttagen, 14 bereits gesendet -> 7 wird nachgeholt (einmal)
        assert due_reminder_stage(days_left=5, reminder_days=self.days, already_sent={14}) == 7

    def test_expired_triggers_zero_stage_once(self) -> None:
        assert due_reminder_stage(days_left=-3, reminder_days=self.days, already_sent=set()) == 0
        assert due_reminder_stage(days_left=-3, reminder_days=self.days, already_sent={0}) is None

    def test_none_days_left_never_due(self) -> None:
        assert (
            due_reminder_stage(days_left=None, reminder_days=self.days, already_sent=set()) is None
        )
