"""Working-day shift for availability dates (see app/config/calendar_config.py)."""

from __future__ import annotations

from datetime import date, timedelta

from app.config.calendar_config import CalendarConfig
from app.core.errors import StageError

STAGE = "Step 2 -- working-day calendar"


def is_working_day(d: date, config: CalendarConfig) -> bool:
    return d.weekday() in config.working_weekdays and d not in config.holidays


def validate_calendar(config: CalendarConfig) -> None:
    """Fail visibly on a calendar that cannot be used, instead of looping or guessing."""
    bad_days = sorted(set(config.working_weekdays) - set(range(7)))
    if not config.working_weekdays or bad_days:
        raise StageError(
            stage=STAGE,
            message="The working-day calendar has no usable working weekdays.",
            expected="working_weekdays: a non-empty set of weekday numbers 0 (Mon) .. 6 (Sun)",
            received=repr(sorted(config.working_weekdays)),
        )
    if config.max_shift_days < 1:
        raise StageError(
            stage=STAGE,
            message="max_shift_days must be at least 1.",
            expected="max_shift_days >= 1",
            received=repr(config.max_shift_days),
        )


def next_working_day(d: date | None, config: CalendarConfig) -> date | None:
    """``d`` itself if it is a working day (or the calendar is off), otherwise
    the first working day after it. ``None`` (UNPLANNED) stays ``None``."""
    if d is None or not config.enabled:
        return d
    candidate = d
    for _ in range(config.max_shift_days + 1):
        if is_working_day(candidate, config):
            return candidate
        candidate += timedelta(days=1)
    raise StageError(
        stage=STAGE,
        message=f"No working day found within {config.max_shift_days} days after {d.isoformat()}.",
        expected=f"a working day within {config.max_shift_days} days (check holidays / shutdown periods)",
        received=f"{d.isoformat()} .. {candidate.isoformat()} all non-working",
    )
