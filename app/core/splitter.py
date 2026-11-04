"""Quantity-level availability splitter.

Core concept (the "mix constraint"): an order ships as a single unit,
so it can only ship when its slowest line is available. Everything
here follows from that one fact.

item_status/mix_status use a TOLERANCE-BAND definition, not a strict
"meets the exact requested date" check: a delay strictly under
`AnalysisConfig.status_tolerance_days` is still OK. This mirrors the
original spreadsheet tool's behaviour, which used its configurable window
as an escalation threshold, not a pass/fail on the requested date itself.

Pipeline:
    OrderLine (with nested production feedback)
        -> split into OrderLineSegment (one per production feedback chunk)
        -> grouped by order to compute availability_of_mix / mix_status
        -> is_recoverable resolved per segment against the order's mix_status
        -> rolled up into LineSummary (item level) and OrderAnalysis (order level)
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import date
from typing import Iterable

from app.config.analysis_config import AnalysisConfig
from app.config.calendar_config import CalendarConfig
from app.core.calendar import next_working_day, validate_calendar
from app.core.errors import StageError
from app.data.models import (
    AvailabilityStatus,
    LineSummary,
    OrderAnalysis,
    OrderLine,
    OrderLineSegment,
)


def split_order_lines(
    order_lines: list[OrderLine], calendar: CalendarConfig | None = None
) -> list[OrderLineSegment]:
    """Split each order line into one segment per production feedback
    entry — the quantity-level availability records.

    `is_on_time` is resolved here since it only needs the segment's own
    date and its own order's requested delivery date. `is_recoverable`
    is deliberately left False here and resolved in `analyze_orders`,
    since it depends on the order-level `mix_status`, which requires
    seeing every segment on the order first.

    If the working-day calendar is enabled, each availability date on a
    non-working day is moved to the next working day HERE, before any
    status is computed, and the original date is kept on the segment.
    """
    calendar = calendar or CalendarConfig()
    if calendar.enabled:
        validate_calendar(calendar)
    segments: list[OrderLineSegment] = []
    for line in order_lines:
        feedback_total = sum(entry.available_qty for entry in line.production_feedback)
        if feedback_total != line.quantity:
            # CHECKPOINT: this is the first place malformed order data
            # would silently produce wrong recoverable/on-time totals
            # downstream. Fail here, specifically, rather than let a
            # quantity mismatch quietly propagate into a scoring or
            # remix number that looks plausible but is wrong.
            raise StageError(
                stage="Step 2 — availability splitter",
                message=(
                    f"Line {line.order_line_id} quantity does not match its "
                    "production feedback."
                ),
                expected=f"production_feedback quantities summing to {line.quantity}",
                received=f"sum of {feedback_total} across {len(line.production_feedback)} entries",
            )
        for idx, entry in enumerate(line.production_feedback, start=1):
            available_date = next_working_day(entry.available_date, calendar)
            is_on_time = (
                available_date is not None
                and available_date <= line.requested_delivery_date
            )
            segments.append(
                OrderLineSegment(
                    segment_id=f"{line.order_line_id}-S{idx}",
                    order_number=line.order_number,
                    order_line_id=line.order_line_id,
                    customer_id=line.customer_id,
                    customer_name=line.customer_name,
                    sku=line.sku,
                    product_category=line.product_category,
                    abc_class=line.abc_class,
                    bi_fs=line.bi_fs,
                    plant=line.plant,
                    unit_price=line.unit_price,
                    seasonality_factor=line.seasonality_factor,
                    requested_delivery_date=line.requested_delivery_date,
                    available_qty=entry.available_qty,
                    available_date=available_date,
                    status=entry.status,
                    is_on_time=is_on_time,
                    is_recoverable=False,
                    cbm_per_unit=line.cbm_per_unit,
                    calendar_shifted_from=(
                        entry.available_date if available_date != entry.available_date else None
                    ),
                )
            )
    return segments


def _group_availability_date(dates: Iterable[date | None]) -> date | None:
    """The "when is this group actually ready" date: the latest date
    across the group, UNLESS any member has no date at all (UNPLANNED),
    in which case the group has no resolvable date and is treated as
    the worst case — never as "ready now" and never compared as a real
    date. This is the direct replacement for the legacy sentinel-date
    (31.12.9999) behaviour: undefined stays undefined, it doesn't get
    treated as a real, comparable date.
    """
    materialized = list(dates)
    if any(d is None for d in materialized):
        return None
    return max(materialized)  # type: ignore[type-var]


def _consistent_requested_date(segments: list[OrderLineSegment]) -> date:
    """All segments of one order are expected to share one requested
    delivery date (it's an order-level field, copied onto each line).
    Fail loudly if that assumption is ever violated by upstream data,
    rather than silently picking one.
    """
    dates = {seg.requested_delivery_date for seg in segments}
    if len(dates) > 1:
        order_number = segments[0].order_number
        raise StageError(
            stage="Step 2 — availability splitter",
            message=f"Order {order_number} has inconsistent requested delivery dates across its lines.",
            expected="exactly one requested_delivery_date shared by every line on the order",
            received=f"{sorted(dates)}",
        )
    return next(iter(dates))


def within_tolerance(
    candidate_date: date | None, requested_date: date, tolerance_days: int
) -> bool:
    """OK/NOK per the (reverse-engineered) legacy semantics: a delay
    strictly under `tolerance_days` is still tolerated as OK — this is
    an escalation threshold, not a "meets the exact requested date"
    check. UNPLANNED (no date) is always NOK, since there's nothing to
    compare against.
    """
    if candidate_date is None:
        return False
    return (candidate_date - requested_date).days < tolerance_days


def analyze_orders(
    order_lines: list[OrderLine], config: AnalysisConfig | None = None
) -> list[OrderAnalysis]:
    """Run the full availability split + mix-constraint analysis over
    an order book, returning one OrderAnalysis per order.

    CHECKPOINT (deliberate, not incomplete automation — see
    app/core/errors.py for the full rationale): this function's output
    is meant to be inspected before it feeds the scoring engine or the
    remix engine. Nothing downstream of this call happens
    automatically; a human (or the calling code, on the human's
    behalf, one stage at a time) decides whether to proceed.
    """
    config = config or AnalysisConfig()
    segments = split_order_lines(order_lines, config.calendar)

    segments_by_order: dict[str, list[OrderLineSegment]] = defaultdict(list)
    for seg in segments:
        segments_by_order[seg.order_number].append(seg)

    analyses: list[OrderAnalysis] = []
    for order_number, order_segments in segments_by_order.items():
        requested_delivery_date = _consistent_requested_date(order_segments)

        # --- MIX level -------------------------------------------------
        availability_of_mix = _group_availability_date(
            seg.available_date for seg in order_segments
        )
        mix_status = (
            "OK"
            if within_tolerance(
                availability_of_mix, requested_delivery_date, config.status_tolerance_days
            )
            else "NOK"
        )

        # Now that mix_status is known, resolve is_recoverable per segment.
        # Note: is_on_time (strict: on/before the requested date) is what
        # makes a chunk "available"; mix_status (tolerance-banded) is
        # what makes it worth recovering. A chunk within an OK mix is
        # never flagged recoverable even if technically a day late,
        # because a mix inside the tolerance window isn't worth a remix.
        # The materiality threshold (recoverable_min_gap_days) additionally
        # requires the chunk to be meaningfully ahead of the mix
        # bottleneck, not just nominally ahead of it.
        resolved_segments = [
            replace(
                seg,
                is_recoverable=(
                    seg.is_on_time
                    and mix_status == "NOK"
                    and (
                        availability_of_mix is None
                        or (availability_of_mix - seg.available_date).days
                        >= config.recoverable_min_gap_days
                    )
                ),
            )
            for seg in order_segments
        ]

        # --- ITEM (line) level ------------------------------------------
        segments_by_line: dict[str, list[OrderLineSegment]] = defaultdict(list)
        for seg in resolved_segments:
            segments_by_line[seg.order_line_id].append(seg)

        line_summaries: list[LineSummary] = []
        for order_line_id, line_segments in segments_by_line.items():
            first = line_segments[0]
            item_availability_date = _group_availability_date(
                seg.available_date for seg in line_segments
            )
            item_status = (
                "OK"
                if within_tolerance(
                    item_availability_date,
                    requested_delivery_date,
                    config.status_tolerance_days,
                )
                else "NOK"
            )
            quantity = sum(seg.available_qty for seg in line_segments)
            on_time_qty = sum(
                seg.available_qty for seg in line_segments if seg.is_on_time
            )
            recoverable_qty = sum(
                seg.available_qty for seg in line_segments if seg.is_recoverable
            )
            unplanned_qty = sum(
                seg.available_qty
                for seg in line_segments
                if seg.status == AvailabilityStatus.UNPLANNED
            )
            delayed_qty = quantity - on_time_qty - unplanned_qty
            # Net Amount: quantity * unit_price (see LineSummary docstring
            # for why this formula, sourced from the legacy tool). All
            # segments on one line share the same unit_price (it's a
            # product-master attribute), so this is exact, not an estimate.
            total_amount = quantity * first.unit_price
            recoverable_amount = recoverable_qty * first.unit_price

            line_summaries.append(
                LineSummary(
                    order_line_id=order_line_id,
                    order_number=order_number,
                    sku=first.sku,
                    product_category=first.product_category,
                    abc_class=first.abc_class,
                    bi_fs=first.bi_fs,
                    plant=first.plant,
                    seasonality_factor=first.seasonality_factor,
                    quantity=quantity,
                    on_time_quantity=on_time_qty,
                    recoverable_quantity=recoverable_qty,
                    delayed_quantity=delayed_qty,
                    unplanned_quantity=unplanned_qty,
                    total_amount=total_amount,
                    recoverable_amount=recoverable_amount,
                    item_availability_date=item_availability_date,
                    item_status=item_status,
                )
            )

        # --- ORDER level rollup ------------------------------------------
        total_quantity = sum(ls.quantity for ls in line_summaries)
        on_time_quantity = sum(ls.on_time_quantity for ls in line_summaries)
        recoverable_quantity = sum(ls.recoverable_quantity for ls in line_summaries)
        unplanned_quantity = sum(ls.unplanned_quantity for ls in line_summaries)
        delayed_quantity = total_quantity - on_time_quantity - unplanned_quantity
        total_amount = sum(ls.total_amount for ls in line_summaries)
        recoverable_amount = sum(ls.recoverable_amount for ls in line_summaries)

        first_seg = order_segments[0]
        analyses.append(
            OrderAnalysis(
                order_number=order_number,
                customer_id=first_seg.customer_id,
                customer_name=first_seg.customer_name,
                requested_delivery_date=requested_delivery_date,
                availability_of_mix=availability_of_mix,
                mix_status=mix_status,
                total_quantity=total_quantity,
                on_time_quantity=on_time_quantity,
                recoverable_quantity=recoverable_quantity,
                delayed_quantity=delayed_quantity,
                unplanned_quantity=unplanned_quantity,
                total_amount=total_amount,
                recoverable_amount=recoverable_amount,
                line_summaries=line_summaries,
                segments=resolved_segments,
            )
        )

    return analyses
