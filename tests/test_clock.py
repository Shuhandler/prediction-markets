"""Clock-dependent logic under a controlled clock: daily P&L rollover,
the pre-midnight summary slot, and ISO timestamp parsing."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from hypothesis import given, strategies as st

import arb_bot as ab


def fake_day(monkeypatch):
    """Make DailyPnLTracker's calendar day controllable."""
    day = ["2026-07-09"]
    monkeypatch.setattr(
        ab.DailyPnLTracker, "_current_day", staticmethod(lambda: day[0]),
    )
    return day


def test_circuit_breaker_trips_and_resets_at_midnight(monkeypatch):
    day = fake_day(monkeypatch)
    tracker = ab.DailyPnLTracker(daily_limit=D("100"))
    tracker.record(D("-101"), 1)
    assert tracker.is_breached
    assert tracker.daily_pnl == D("-101")

    day[0] = "2026-07-10"  # midnight UTC rolls over
    assert not tracker.is_breached
    assert tracker.daily_pnl == ab.ZERO


def test_pnl_accumulates_within_day_only(monkeypatch):
    day = fake_day(monkeypatch)
    tracker = ab.DailyPnLTracker(daily_limit=D("100"))
    tracker.record(D("2.50"), 2)
    tracker.record(D("-1.00"), 1)
    assert tracker.daily_pnl == D("4.00")

    day[0] = "2026-07-10"
    tracker.record(D("1.00"), 1)
    assert tracker.daily_pnl == D("1.00")  # yesterday's P&L not carried over


def test_breach_uses_strict_threshold(monkeypatch):
    fake_day(monkeypatch)
    tracker = ab.DailyPnLTracker(daily_limit=D("100"))
    tracker.record(D("-100"), 1)  # exactly at the limit: not breached
    assert not tracker.is_breached
    tracker.record(D("-0.01"), 1)
    assert tracker.is_breached


# ── Daily summary slot: must fire BEFORE the midnight reset ──────────

def test_summary_fires_before_midnight_same_day():
    now = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)
    wait = ab._seconds_until_daily_summary(now)
    fire_at = now + timedelta(seconds=wait)
    assert fire_at == datetime(2026, 7, 9, 23, 59, 55, tzinfo=timezone.utc)


def test_summary_in_final_seconds_targets_next_day():
    now = datetime(2026, 7, 9, 23, 59, 57, tzinfo=timezone.utc)
    wait = ab._seconds_until_daily_summary(now)
    fire_at = now + timedelta(seconds=wait)
    assert fire_at == datetime(2026, 7, 10, 23, 59, 55, tzinfo=timezone.utc)


@given(st.datetimes(
    min_value=datetime(2026, 1, 1),
    max_value=datetime(2027, 1, 1),
).map(lambda d: d.replace(microsecond=0)))
def test_summary_wait_always_positive_and_pre_midnight(naive_now):
    # whole seconds only: keeps now + timedelta(seconds=wait) exact
    now = naive_now.replace(tzinfo=timezone.utc)
    wait = ab._seconds_until_daily_summary(now)
    assert 0 < wait <= 86400
    fire_at = now + timedelta(seconds=wait)
    # Always lands on the 23:59:55 slot — before the day rolls over
    assert (fire_at.hour, fire_at.minute, fire_at.second) == (23, 59, 55)


# ── ISO parsing helper (used for stuck-position aging) ───────────────

def test_parse_iso_variants():
    z = ab._parse_iso("2026-07-09T12:00:00Z")
    assert z is not None and z.tzinfo is not None
    offset = ab._parse_iso("2026-07-09T12:00:00+00:00")
    assert offset == z
    naive = ab._parse_iso("2026-07-09T12:00:00")
    assert naive == z  # naive timestamps assumed UTC
    assert ab._parse_iso("") is None
    assert ab._parse_iso("garbage") is None
