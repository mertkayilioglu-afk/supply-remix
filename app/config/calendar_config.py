"""Working-day calendar for availability dates.

A plant does not dispatch on a weekend or a public holiday, so goods that
become available on such a day are really ready on the next working day.
When enabled, the splitter shifts every non-working AVAILABILITY date forward
to the next working day before anything else is computed, so OK/NOK status,
availability of mix and container dates all use the shifted date.

Deliberately NOT shifted:
  * requested delivery dates -- they are the customer's own dates;
  * UNPLANNED chunks -- they have no date to shift (None stays None).

Off by default, so existing results do not change unless someone opts in.

Example (Mon-Fri plus two public holidays)::

    CalendarConfig(
        enabled=True,
        holidays=frozenset({date(2026, 12, 25), date(2027, 1, 1)}),
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class CalendarConfig:
    enabled: bool = False

    # ISO-style weekday numbers as returned by date.weekday(): Monday=0 ... Sunday=6.
    working_weekdays: frozenset[int] = frozenset({0, 1, 2, 3, 4})

    # Non-working calendar dates (public holidays, plant shutdown days).
    holidays: frozenset[date] = frozenset()

    # Safety bound for the forward search. A calendar with no working day in
    # this many days is treated as a configuration error, not looped over.
    max_shift_days: int = 31
