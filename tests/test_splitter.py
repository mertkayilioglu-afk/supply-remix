"""Tests for the quantity-level availability splitter.

Covers the four scenarios called out in the Step 2 spec:
  1. A line split across dates with partial recoverability.
  2. An order where every line is OK.
  3. An order blocked by a single late line.
  4. An order containing an UNPLANNED chunk.
Plus explicit checks that None dates never enter date arithmetic.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.config.analysis_config import AnalysisConfig
from app.core.splitter import analyze_orders
from app.data.models import (
    AvailabilityStatus,
    OrderLine,
    ProductionFeedbackEntry,
)


def make_line(
    order_number: str,
    order_line_id: str,
    quantity: int,
    requested_delivery_date: date,
    production_feedback: list[ProductionFeedbackEntry],
    *,
    customer_id: str = "CUST-001",
    sku: str = "SKU-0001",
    abc_class: str = "A",
    product_category: str = "Lighting",
    bi_fs: str = "BI",
    cbm_per_unit: float = 0.1,
    plant: str = "PLANT-XA1",
    unit_price: float = 10.0,
) -> OrderLine:
    """Small builder to keep test cases focused on what varies."""
    return OrderLine(
        order_number=order_number,
        order_line_id=order_line_id,
        customer_id=customer_id,
        customer_name="Test Customer",
        sku=sku,
        product_category=product_category,
        abc_class=abc_class,
        seasonality_factor=1.0,
        supplier_country="XA",
        bi_fs=bi_fs,
        plant=plant,
        unit_price=unit_price,
        quantity=quantity,
        cbm_per_unit=cbm_per_unit,
        order_date=requested_delivery_date,
        requested_delivery_date=requested_delivery_date,
        production_feedback=production_feedback,
    )


def confirmed(qty: int, d: date) -> ProductionFeedbackEntry:
    return ProductionFeedbackEntry(
        available_qty=qty, available_date=d, status=AvailabilityStatus.CONFIRMED
    )


def unplanned(qty: int) -> ProductionFeedbackEntry:
    return ProductionFeedbackEntry(
        available_qty=qty, available_date=None, status=AvailabilityStatus.UNPLANNED
    )


REQUESTED = date(2026, 2, 18)


class TestPartialRecoverability:
    """A single line split into an early (on-time) chunk and a late
    chunk: the early chunk should be recoverable, the late chunk should
    not, and the split from the spec (20 recoverable / 20 late) should
    hold exactly."""

    def test_split_line_partial_recoverability(self) -> None:
        line = make_line(
            "SO-1", "SO-1-L1", 40, REQUESTED,
            [confirmed(20, date(2026, 2, 16)), confirmed(20, date(2026, 3, 20))],
        )
        [order] = analyze_orders([line])

        assert order.mix_status == "NOK"
        assert order.availability_of_mix == date(2026, 3, 20)
        assert order.total_quantity == 40
        assert order.on_time_quantity == 20
        assert order.recoverable_quantity == 20
        assert order.delayed_quantity == 20

        [line_summary] = order.line_summaries
        assert line_summary.item_status == "NOK"
        assert line_summary.recoverable_quantity == 20
        assert line_summary.delayed_quantity == 20

        early_seg, late_seg = sorted(order.segments, key=lambda s: s.available_date)
        assert early_seg.is_on_time is True
        assert early_seg.is_recoverable is True
        assert late_seg.is_on_time is False
        assert late_seg.is_recoverable is False


class TestEveryLineOk:
    """An order where every line is on time: mix_status OK, and
    critically, recoverable_quantity must be 0 — there's nothing
    "trapped" when the whole order already ships on time."""

    def test_all_lines_ok_means_nothing_recoverable(self) -> None:
        line_a = make_line(
            "SO-2", "SO-2-L1", 30, REQUESTED, [confirmed(30, date(2026, 2, 10))]
        )
        line_b = make_line(
            "SO-2", "SO-2-L2", 15, REQUESTED, [confirmed(15, date(2026, 2, 17))]
        )
        [order] = analyze_orders([line_a, line_b])

        assert order.mix_status == "OK"
        assert order.availability_of_mix == date(2026, 2, 17)
        assert order.on_time_quantity == 45
        assert order.recoverable_quantity == 0
        assert order.delayed_quantity == 0
        assert all(ls.item_status == "OK" for ls in order.line_summaries)
        assert all(not seg.is_recoverable for seg in order.segments)


class TestSingleLineBottleneck:
    """An order blocked by exactly one late line: the other, on-time
    lines become fully recoverable; the late line's own on-time portion
    (if it has one) is not recoverable because it IS the bottleneck."""

    def test_one_late_line_blocks_the_rest(self) -> None:
        line_ok_1 = make_line(
            "SO-3", "SO-3-L1", 50, REQUESTED, [confirmed(50, date(2026, 2, 12))]
        )
        line_ok_2 = make_line(
            "SO-3", "SO-3-L2", 25, REQUESTED, [confirmed(25, date(2026, 2, 15))]
        )
        line_late = make_line(
            "SO-3", "SO-3-L3", 10, REQUESTED, [confirmed(10, date(2026, 3, 15))]
        )
        [order] = analyze_orders([line_ok_1, line_ok_2, line_late])

        assert order.mix_status == "NOK"
        assert order.availability_of_mix == date(2026, 3, 15)
        assert order.total_quantity == 85
        assert order.recoverable_quantity == 75  # the two on-time lines
        assert order.delayed_quantity == 10  # only the bottleneck line

        by_line = {ls.order_line_id: ls for ls in order.line_summaries}
        assert by_line["SO-3-L1"].item_status == "OK"
        assert by_line["SO-3-L1"].recoverable_quantity == 50
        assert by_line["SO-3-L2"].item_status == "OK"
        assert by_line["SO-3-L2"].recoverable_quantity == 25
        assert by_line["SO-3-L3"].item_status == "NOK"
        # The bottleneck line's own quantity is late, not recoverable —
        # it's the cause of the block, not trapped behind something else.
        assert by_line["SO-3-L3"].recoverable_quantity == 0
        assert by_line["SO-3-L3"].delayed_quantity == 10


class TestUnplannedChunk:
    """An order containing an UNPLANNED chunk: availability_of_mix must
    be None (never a sentinel date), mix_status must be NOK, and no
    date arithmetic should have been attempted on the unplanned chunk."""

    def test_unplanned_chunk_forces_no_resolvable_mix_date(self) -> None:
        line_ok = make_line(
            "SO-4", "SO-4-L1", 20, REQUESTED, [confirmed(20, date(2026, 2, 10))]
        )
        line_unplanned = make_line(
            "SO-4", "SO-4-L2", 15, REQUESTED, [unplanned(15)]
        )
        [order] = analyze_orders([line_ok, line_unplanned])

        assert order.availability_of_mix is None
        assert order.mix_status == "NOK"
        assert order.unplanned_quantity == 15
        # The on-time line's quantity is on time but trapped behind the
        # unplanned line, so it IS recoverable.
        assert order.recoverable_quantity == 20
        assert order.on_time_quantity == 20
        assert order.delayed_quantity == 0  # nothing has a real late date here

        by_line = {ls.order_line_id: ls for ls in order.line_summaries}
        assert by_line["SO-4-L2"].item_status == "NOK"
        assert by_line["SO-4-L2"].item_availability_date is None
        assert by_line["SO-4-L2"].unplanned_quantity == 15

        unplanned_seg = next(
            s for s in order.segments if s.status == AvailabilityStatus.UNPLANNED
        )
        assert unplanned_seg.available_date is None
        assert unplanned_seg.is_on_time is False
        assert unplanned_seg.is_recoverable is False

    def test_fully_unplanned_line_never_produces_a_fake_date(self) -> None:
        """A line where every chunk is unplanned must never resolve to
        a date — this is the direct regression test for the legacy
        31.12.9999 sentinel-date landmine."""
        line = make_line("SO-5", "SO-5-L1", 40, REQUESTED, [unplanned(25), unplanned(15)])
        [order] = analyze_orders([line])

        assert order.availability_of_mix is None
        assert order.mix_status == "NOK"
        [line_summary] = order.line_summaries
        assert line_summary.item_availability_date is None
        assert line_summary.item_status == "NOK"
        assert line_summary.unplanned_quantity == 40
        assert line_summary.recoverable_quantity == 0


class TestToleranceBandStatus:
    """item_status/mix_status use a tolerance-band definition (mirrors
    the legacy tool): a delay strictly under the configured window is
    still OK, not only a delay of exactly zero."""

    def test_delay_within_window_is_still_ok(self) -> None:
        # 11 days late, well under the default 21-day window.
        line = make_line(
            "SO-7", "SO-7-L1", 30, REQUESTED, [confirmed(30, date(2026, 3, 1))]
        )
        [order] = analyze_orders([line])

        assert order.mix_status == "OK"
        assert order.line_summaries[0].item_status == "OK"
        # Nothing is recoverable when the mix itself is OK (within tolerance).
        assert order.recoverable_quantity == 0

    def test_delay_at_exactly_the_window_boundary_is_nok(self) -> None:
        # Exactly 21 days late == the default window: legacy's "< window"
        # check means the boundary itself is NOT tolerated (NOK).
        line = make_line(
            "SO-8", "SO-8-L1", 30, REQUESTED, [confirmed(30, REQUESTED + timedelta(days=21))]
        )
        [order] = analyze_orders([line])
        assert order.mix_status == "NOK"

    def test_custom_tolerance_window_is_respected(self) -> None:
        line = make_line(
            "SO-9", "SO-9-L1", 30, REQUESTED, [confirmed(30, date(2026, 3, 1))]
        )
        # Same 11-day delay as the first test, but now with a tight
        # 7-day tolerance window: should flip to NOK.
        [order] = analyze_orders([line], config=AnalysisConfig(status_tolerance_days=7))
        assert order.mix_status == "NOK"


class TestRecoverableMaterialityThreshold:
    """is_recoverable requires the gap between an on-time chunk and the
    order's availability_of_mix to meet a configurable materiality
    threshold, not just be positive."""

    def test_default_threshold_of_zero_recovers_any_positive_gap(self) -> None:
        line = make_line(
            "SO-10", "SO-10-L1", 40, REQUESTED,
            [confirmed(20, date(2026, 2, 10)), confirmed(20, date(2026, 3, 20))],
        )
        [order] = analyze_orders([line])  # default recoverable_min_gap_days=0
        assert order.recoverable_quantity == 20

    def test_higher_threshold_can_exclude_a_marginal_gap(self) -> None:
        # Bottleneck is exactly at the 21-day tolerance boundary (NOK),
        # 29 days after the early chunk. A 30-day materiality threshold
        # excludes it even though the default (0) would recover it.
        line = make_line(
            "SO-11", "SO-11-L1", 40, REQUESTED,
            [confirmed(20, date(2026, 2, 10)), confirmed(20, date(2026, 3, 11))],
        )
        [order_default] = analyze_orders([line])
        assert order_default.mix_status == "NOK"
        assert order_default.recoverable_quantity == 20  # default threshold: gap counts

        [order_strict] = analyze_orders(
            [line], config=AnalysisConfig(recoverable_min_gap_days=30)
        )
        assert order_strict.mix_status == "NOK"  # status unaffected by this threshold
        assert order_strict.recoverable_quantity == 0  # gap (29 days) < 30-day bar

    def test_unplanned_mix_always_satisfies_materiality(self) -> None:
        """When availability_of_mix is None (blocked by an UNPLANNED
        chunk), the gap can't be computed — this should not silently
        exclude otherwise-recoverable quantity."""
        line_ok = make_line(
            "SO-12", "SO-12-L1", 20, REQUESTED, [confirmed(20, date(2026, 2, 10))]
        )
        line_unplanned = make_line("SO-12", "SO-12-L2", 15, REQUESTED, [unplanned(15)])
        [order] = analyze_orders(
            [line_ok, line_unplanned], config=AnalysisConfig(recoverable_min_gap_days=999)
        )
        assert order.availability_of_mix is None
        assert order.recoverable_quantity == 20


class TestNetAmount:
    """Net Amount (quantity * unit_price) should roll up through
    LineSummary and OrderAnalysis exactly the way quantity does."""

    def test_total_and_recoverable_amount_computed_from_unit_price(self) -> None:
        line = make_line(
            "SO-100", "SO-100-L1", 40, REQUESTED,
            [confirmed(20, date(2026, 2, 10)), confirmed(20, date(2026, 3, 20))],
            unit_price=5.0,
        )
        [order] = analyze_orders([line])

        assert order.total_amount == 200.0  # 40 * 5.0
        assert order.recoverable_amount == 100.0  # 20 recoverable * 5.0
        [ls] = order.line_summaries
        assert ls.total_amount == 200.0
        assert ls.recoverable_amount == 100.0

    def test_amount_sums_across_multiple_lines_with_different_prices(self) -> None:
        line_a = make_line(
            "SO-101", "SO-101-L1", 10, REQUESTED, [confirmed(10, date(2026, 2, 10))],
            unit_price=2.0,
        )
        line_b = make_line(
            "SO-101", "SO-101-L2", 5, REQUESTED, [confirmed(5, date(2026, 2, 10))],
            unit_price=100.0,
        )
        [order] = analyze_orders([line_a, line_b])
        assert order.total_amount == 10 * 2.0 + 5 * 100.0


class TestInconsistentRequestedDate:
    """Defensive check: an order whose lines disagree on requested
    delivery date is a data integrity problem and should fail loudly,
    not silently pick one value."""

    def test_raises_on_inconsistent_requested_dates(self) -> None:
        line_a = make_line(
            "SO-6", "SO-6-L1", 10, date(2026, 2, 18), [confirmed(10, date(2026, 2, 10))]
        )
        line_b = make_line(
            "SO-6", "SO-6-L2", 10, date(2026, 2, 20), [confirmed(10, date(2026, 2, 10))]
        )
        with pytest.raises(ValueError, match="inconsistent requested delivery dates"):
            analyze_orders([line_a, line_b])
