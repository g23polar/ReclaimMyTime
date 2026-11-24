"""Pure period math (app.todos.periods): no DB, no clock. Covers every week_start, the year
boundary, the DST transition day, the current/previous window, and bad inputs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.todos.periods import (
    allowed_periods,
    current_period,
    is_allowed_period,
    previous_period,
)

# Tue 2026-11-24. ISO weekdays: Mon 23, Tue 24, Wed 25, Sun 29.
TODAY = date(2026, 11, 24)


@pytest.mark.parametrize(
    ("week_start", "expected"),
    [
        (1, date(2026, 11, 23)),  # Monday
        (2, date(2026, 11, 24)),  # Tuesday: today is the week's first day
        (3, date(2026, 11, 18)),  # Wednesday: a Wednesday-start week began on the 18th
        (4, date(2026, 11, 19)),  # Thursday
        (5, date(2026, 11, 20)),  # Friday
        (6, date(2026, 11, 21)),  # Saturday
        (7, date(2026, 11, 22)),  # Sunday
    ],
)
def test_weekly_current_period_for_every_week_start(week_start: int, expected: date) -> None:
    assert current_period("weekly", TODAY, week_start) == expected
    assert previous_period("weekly", TODAY, week_start) == expected - timedelta(days=7)


@pytest.mark.parametrize("week_start", range(1, 8))
def test_the_week_start_day_is_its_own_period_start(week_start: int) -> None:
    start = current_period("weekly", TODAY, week_start)
    assert start.isoweekday() == week_start
    assert current_period("weekly", start, week_start) == start
    assert current_period("weekly", start + timedelta(days=6), week_start) == start


@pytest.mark.parametrize("week_start", range(1, 8))
def test_week_boundary_is_exact(week_start: int) -> None:
    """Six days into a week still belongs to it; the seventh starts the next one."""
    start = current_period("weekly", TODAY, week_start)
    assert current_period("weekly", start + timedelta(days=6), week_start) == start
    assert current_period("weekly", start + timedelta(days=7), week_start) == start + timedelta(
        days=7
    )


def test_week_crossing_a_year_boundary() -> None:
    new_year = date(2027, 1, 2)  # Saturday
    assert current_period("weekly", new_year, 1) == date(2026, 12, 28)
    assert previous_period("weekly", new_year, 1) == date(2026, 12, 21)
    assert current_period("weekly", date(2027, 1, 1), 1) == date(2026, 12, 28)


def test_daily_periods_are_the_local_day_and_the_day_before() -> None:
    assert allowed_periods("daily", TODAY, 1) == (date(2026, 11, 24), date(2026, 11, 23))


def test_daily_ignores_week_start() -> None:
    assert allowed_periods("daily", TODAY, 1) == allowed_periods("daily", TODAY, 7)


def test_dst_transition_day_is_the_calendar_day_in_the_device_zone() -> None:
    """US DST starts 2026-03-08. An instant just after midnight local is that day, not the
    previous one, and the periods are computed from that date."""
    la = ZoneInfo("America/Los_Angeles")
    instant = datetime(2026, 3, 8, 8, 30, tzinfo=UTC)  # 00:30 PST; the clock jumps at 02:00
    local_today = instant.astimezone(la).date()
    assert local_today == date(2026, 3, 8)
    assert allowed_periods("daily", local_today, 1) == (date(2026, 3, 8), date(2026, 3, 7))
    assert current_period("weekly", local_today, 1) == date(2026, 3, 2)


def test_previous_and_current_are_allowed_older_and_future_are_not() -> None:
    for kind in ("daily", "weekly"):
        current, previous = allowed_periods(kind, TODAY, 1)
        assert is_allowed_period(kind, current, TODAY, 1)
        assert is_allowed_period(kind, previous, TODAY, 1)
        assert not is_allowed_period(
            kind, previous - timedelta(days=7 if kind == "weekly" else 1), TODAY, 1
        )
        assert not is_allowed_period(
            kind, current + timedelta(days=7 if kind == "weekly" else 1), TODAY, 1
        )


def test_non_start_date_is_rejected_for_weekly() -> None:
    assert not is_allowed_period("weekly", date(2026, 11, 25), TODAY, 1)  # a Wednesday
    assert not is_allowed_period("weekly", date(2026, 11, 24), TODAY, 1)  # today, not a start


def test_week_start_change_moves_the_allowed_set() -> None:
    """A week_start change is not re-keyed: a Monday start is no longer allowed for a Sunday-start
    user on the same day (the accepted v1 gap, documented in app.todos.periods)."""
    assert is_allowed_period("weekly", date(2026, 11, 23), TODAY, 1)
    assert not is_allowed_period("weekly", date(2026, 11, 23), TODAY, 7)


@pytest.mark.parametrize("bad", [0, 8, -1])
def test_week_start_outside_1_to_7_is_rejected(bad: int) -> None:
    with pytest.raises(ValueError, match="week_start"):
        current_period("weekly", TODAY, bad)


@pytest.mark.parametrize("fn", [current_period, previous_period])
def test_one_off_has_no_periods(fn: object) -> None:
    with pytest.raises(ValueError, match="not a recurring kind"):
        fn("one_off", TODAY, 1)  # type: ignore[operator]


def test_unknown_kind_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a recurring kind"):
        allowed_periods("monthly", TODAY, 1)
