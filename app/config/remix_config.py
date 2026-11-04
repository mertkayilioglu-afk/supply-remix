"""Configuration for the remix engine.

The remix mirrors the original tool's potential-availability view: for one customer,
EVERY unit of their not-yet-shippable (NOK) orders in the target's category
group(s) -- OK, late and UNPLANNED alike -- is distributed into containers
ordered by availability date. Each container carries its own fill % and
OK/NOK status. Late/unplanned units are never left out of the containers:
leaving them out would look to the customer like a cancellation.

Hard rule: every container that is created must be filled to at least
`min_acceptable_utilization` (and never above capacity). If the pool's total
volume cannot satisfy ``min_acceptable_utilization * container_count``, the
containers cannot be created and there is NO proposal (with an explicit
reason) -- volume that is missing here cannot be found elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class RemixConfig:
    # Category -> group name. Same customer + same/linked group only: goods in a
    # category ship from the same plant of origin and a container goes to one
    # address, so categories are never mixed unless explicitly linked here.
    # Unlisted categories are their own group.
    category_groups: dict[str, str] = field(default_factory=dict)

    container_capacity_cbm: float = 30.0

    # Hard floor for EVERY container (94.5%).
    min_acceptable_utilization: float = 0.945

    # Packer. "greedy" (default) is the date-ordered fill + local repair.
    # "milp" solves the same hard rules exactly with PuLP/CBC (optional
    # dependency, see app/core/milp_packer.py); it is an experiment to compare
    # against, not a replacement.
    packer: Literal["greedy", "milp"] = "greedy"

    # Wall-clock limit for ONE MILP solve (one category group of one order).
    # If CBC finds no solution in time, the group fails visibly as
    # cannot_balance with a message saying the time limit was hit.
    milp_time_limit_seconds: float = 10.0

    # Guard on the REPAIR step only (not the first earliest-first fill): a
    # balancing move or swap may not push a container's ready date more than
    # this many days beyond its ready date as the fill left it. The reference is
    # fixed at the start of the repair, so a chain of small moves cannot creep a
    # container later and later. A container already waiting on an UNPLANNED unit
    # has no ready date and accepts any unit; an UNPLANNED unit may not be moved
    # into a dated container (that would make its ready date unknown).
    #
    # Default 21 days: the same window under which a delay already counts as OK
    # (AnalysisConfig.status_tolerance_days). Balancing may cost a container
    # up to the delay the business already tolerates, never more. Kept as a
    # separate setting so the two can be tuned independently. None = no guard.
    max_balancing_delay_days: int | None = 21

    # Whether a unit is OK is NOT configured here: it comes from
    # AnalysisConfig.status_tolerance_days, the same window the splitter uses,
    # so a container's OK/NOK status can never disagree with the order analysis.
