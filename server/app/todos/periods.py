"""Period math for recurring todos. Pure: no DB, no clock. The caller passes `local_today`
(the date in the device zone, from X-Timezone) and `week_start` (preferences, ISO weekday
1 = Monday .. 7 = Sunday, the same convention as app.db.solver_io.week_start_for).

Known gap, accepted for v1 (owner decision, 2026-11-24): a weekly completion is keyed to the
period start it was made for. Changing `week_start` does not re-key existing rows; they stop
matching the current or previous period and stop counting. Nothing here re-computes history.
"""

from __future__ import annotations

from datetime import date, timedelta

RECURRING_KINDS = ("daily", "weekly")


def _check(kind: str, week_start: int) -> None:
    if kind not in RECURRING_KINDS:
        raise ValueError(f"not a recurring kind: {kind!r}")
    if not 1 <= week_start <= 7:
        raise ValueError(f"week_start must be 1..7, got {week_start}")


def current_period(kind: str, local_today: date, week_start: int) -> date:
    """The start of the period containing `local_today`."""
    _check(kind, week_start)
    if kind == "daily":
        return local_today
    # Days since the most recent week_start weekday (0 on that day).
    return local_today - timedelta(days=(local_today.isoweekday() - week_start) % 7)


def previous_period(kind: str, local_today: date, week_start: int) -> date:
    """The start of the period before the current one."""
    current = current_period(kind, local_today, week_start)
    return current - timedelta(days=1 if kind == "daily" else 7)


def allowed_periods(kind: str, local_today: date, week_start: int) -> tuple[date, date]:
    """The only period starts a completion may name: (current, previous)."""
    return (
        current_period(kind, local_today, week_start),
        previous_period(kind, local_today, week_start),
    )


def is_allowed_period(kind: str, period_start: date, local_today: date, week_start: int) -> bool:
    """True iff `period_start` is exactly the current or the previous period start. A non-start
    date (e.g. a Wednesday for a Monday-weekly) is therefore rejected by construction."""
    return period_start in allowed_periods(kind, local_today, week_start)
