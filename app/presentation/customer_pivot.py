"""Customer-level aggregation shared by the HTML pivot and its Excel
export, so both views are always built from the exact same numbers.

This is presentation-layer aggregation only — it does not change
/analyze's order-level contract (still the stable engine output);
it re-groups that same data by customer, optionally filtered to one
plant, for a planner deciding which customer's situation to look at
first before drilling into a specific order.
"""

from __future__ import annotations

import math

from dataclasses import dataclass, field

from app.api.schemas import AnalyzeResponse


@dataclass(frozen=True)
class CustomerPivotRow:
    customer_id: str
    customer_name: str
    order_count: int
    order_numbers: list[str]
    total_quantity: int
    recoverable_quantity: int
    total_amount: float
    recoverable_amount: float
    # The highest coefficient among this customer's at-risk orders —
    # NOT summed or averaged. A customer's row is worth attention
    # because of their single most critical order, not diluted by
    # having many mildly-at-risk ones.
    max_coefficient: float


def all_plants(response: AnalyzeResponse) -> list[str]:
    """Every distinct plant appearing across all at-risk orders, for
    populating the pivot's plant filter dropdown."""
    plants: set[str] = set()
    for order in response.at_risk_orders:
        for entry in order.plant_breakdown:
            plants.add(entry.plant)
    return sorted(plants)


def aggregate_by_customer(
    response: AnalyzeResponse, plant: str | None = None
) -> list[CustomerPivotRow]:
    """One row per customer, summing quantity/amount across all their
    at-risk orders. If `plant` is given, only that plant's contribution
    (per order.plant_breakdown) counts toward the sums.

    PLANT-INCLUSION RULE (deliberate, verified against real data before
    picking it — see below): a customer appears under a plant filter if
    ANY of their at-risk orders has ANY line sourced from that plant —
    not only if every order (or every line) is from that plant. The
    contribution summed is still just that plant's share (via
    order.plant_breakdown), never the whole order.

    This was checked, not assumed: in the synthetic dataset, 71% of
    orders span more than one plant (a single order commonly mixes
    products from 2-3 different factories). An "ALL lines/orders must
    match" rule would exclude the large majority of real activity from
    every plant filter, making the filter nearly useless. "ANY" is also
    the standard behavior for a pivot-table filter: selecting a
    dimension member shows every fact touching it, not only facts
    exclusively tied to it.

    The HTML pivot and the Excel export both call this exact function
    — there is no separate aggregation path for either, so they cannot
    apply a different rule from each other.

    Sorted by max_coefficient descending, mirroring /analyze's own
    ranking so the highest-priority customer is always first.
    """

    @dataclass
    class _Bucket:
        customer_name: str
        order_numbers: list[str] = field(default_factory=list)
        total_quantity: int = 0
        recoverable_quantity: int = 0
        total_amount: float = 0.0
        recoverable_amount: float = 0.0
        max_coefficient: float = 0.0

    buckets: dict[str, _Bucket] = {}

    for order in response.at_risk_orders:
        if plant is None:
            qty = order.total_quantity
            recoverable_qty = order.recoverable_quantity
            amount = order.total_amount
            recoverable_amount = order.recoverable_amount
        else:
            matching = [e for e in order.plant_breakdown if e.plant == plant]
            if not matching:
                continue
            qty = sum(e.quantity for e in matching)
            recoverable_qty = sum(e.recoverable_quantity for e in matching)
            amount = math.fsum(e.total_amount for e in matching)
            recoverable_amount = math.fsum(e.recoverable_amount for e in matching)

        bucket = buckets.setdefault(order.customer_id, _Bucket(customer_name=order.customer_name))
        bucket.order_numbers.append(order.order_number)
        bucket.total_quantity += qty
        bucket.recoverable_quantity += recoverable_qty
        bucket.total_amount += amount
        bucket.recoverable_amount += recoverable_amount
        bucket.max_coefficient = max(bucket.max_coefficient, order.coefficient)

    rows = [
        CustomerPivotRow(
            customer_id=customer_id,
            customer_name=b.customer_name,
            order_count=len(b.order_numbers),
            order_numbers=sorted(b.order_numbers),
            total_quantity=b.total_quantity,
            recoverable_quantity=b.recoverable_quantity,
            total_amount=b.total_amount,
            recoverable_amount=b.recoverable_amount,
            max_coefficient=b.max_coefficient,
        )
        for customer_id, b in buckets.items()
    ]
    rows.sort(key=lambda r: r.max_coefficient, reverse=True)
    return rows
