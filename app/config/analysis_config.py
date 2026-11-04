"""Configuration for the availability splitter's status/recoverability
thresholds.

These mirror two concepts from the original spreadsheet tool:

- The OK/NOK window is a TOLERANCE window, not a strict "meets the
  exact requested date" check: a line/mix is OK if its delay is under
  that many days, not only if it has zero delay.
- The "recoverable" flag (our `is_recoverable`) can require the early
  chunk to be a minimum number of days ahead of the comparison date --
  a materiality threshold, not "any positive gap counts."

Kept in a separate config from `ScoringConfig` because these are
splitter-level business rules (they change what "OK"/"NOK"/
"recoverable" mean), not scoring weights.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.calendar_config import CalendarConfig


@dataclass(frozen=True)
class AnalysisConfig:
    # Tolerance window for item_status / mix_status. A line/mix is OK
    # if (availability_date - requested_delivery_date) is strictly less
    # than this many days — i.e. a delay under the window is still
    # tolerated as OK, matching the legacy tool's actual behaviour
    # (it used this window as an escalation threshold, not a pass/fail
    # on the exact requested date). UNPLANNED (no date) is always NOK
    # regardless of this window, since there's nothing to compare.
    status_tolerance_days: int = 21

    # Materiality threshold for is_recoverable: an on-time chunk only
    # counts as recoverable if the order's availability_of_mix is at
    # least this many days later than the chunk's own date. The original
    # tool used a fixed threshold compared at line level; we keep the
    # comparison at order level (against availability_of_mix, per the
    # Step 2 spec) and make the threshold configurable instead of
    # hard-coding it.
    #
    # Default 0 means "any positive gap counts" — note that in
    # practice, once mix_status is NOK (a delay of at least
    # `status_tolerance_days`), the gap between any on-time chunk and
    # availability_of_mix is automatically at least `status_tolerance_days`
    # too, so this threshold only has independent effect when set
    # higher than `status_tolerance_days`, or when the two windows are
    # deliberately decoupled (e.g. a tight status_tolerance_days for
    # early warning, but a much larger recoverable_min_gap_days so only
    # substantial recovery opportunities get surfaced for a remix).
    # When availability_of_mix is None (blocked by an UNPLANNED chunk
    # elsewhere), the gap can't be computed — treated as automatically
    # satisfying materiality, since an unplanned block is a real,
    # if unquantifiable, blockage.
    recoverable_min_gap_days: int = 0

    # Working-day calendar applied to availability dates before anything else
    # (see app/config/calendar_config.py). Off by default.
    calendar: CalendarConfig = field(default_factory=CalendarConfig)
