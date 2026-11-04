"""Tests for the criticality scoring engine.

Focus: weights come from config (not hard-coded), the critical window
and backorder logic behave as specified, and scoring never filters —
it only ranks.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.config.scoring_config import ScoringConfig
from app.core.scoring import score_order, score_orders
from app.core.splitter import analyze_orders
from app.data.models import OrderAnalysis
from tests.test_splitter import REQUESTED, confirmed, make_line


def _single_line_order(**overrides: object) -> OrderAnalysis:
    defaults = dict(
        order_number="SO-100",
        order_line_id="SO-100-L1",
        quantity=10,
        requested_delivery_date=REQUESTED,
        production_feedback=[confirmed(10, REQUESTED)],
    )
    defaults.update(overrides)
    line = make_line(**defaults)
    [order] = analyze_orders([line])
    return order


class TestWeightsComeFromConfig:
    def test_changing_abc_weight_changes_coefficient(self) -> None:
        order = _single_line_order(abc_class="A")
        low = ScoringConfig(abc_weights={"A": 1.0})
        high = ScoringConfig(abc_weights={"A": 999.0})

        low_score = score_order(order, low, as_of_date=REQUESTED)
        high_score = score_order(order, high, as_of_date=REQUESTED)

        assert high_score.coefficient > low_score.coefficient
        assert high_score.score_breakdown["abc"] == 999.0
        assert low_score.score_breakdown["abc"] == 1.0

    def test_unknown_category_defaults_to_zero_not_an_error(self) -> None:
        order = _single_line_order(product_category="Nonexistent Category")
        config = ScoringConfig(category_weights={"Lighting": 15.0})
        scored = score_order(order, config, as_of_date=REQUESTED)
        assert scored.score_breakdown["category"] == 0.0


class TestBackorderAndProximity:
    def test_backorder_gets_flat_max_weight(self) -> None:
        order = _single_line_order(requested_delivery_date=date(2026, 1, 1))
        config = ScoringConfig(backorder_weight=80.0, critical_window_days=21)
        as_of = date(2026, 1, 10)  # 9 days past the requested date -> backorder

        scored = score_order(order, config, as_of_date=as_of)
        assert scored.score_breakdown["etd_proximity_or_backorder"] == 80.0

    def test_due_today_gets_max_proximity_weight(self) -> None:
        order = _single_line_order(requested_delivery_date=date(2026, 2, 18))
        config = ScoringConfig(etd_proximity_max_weight=50.0, critical_window_days=21)

        scored = score_order(order, config, as_of_date=date(2026, 2, 18))
        assert scored.score_breakdown["etd_proximity_or_backorder"] == 50.0

    def test_edge_of_window_gets_near_zero_proximity_weight(self) -> None:
        order = _single_line_order(requested_delivery_date=date(2026, 2, 18))
        config = ScoringConfig(etd_proximity_max_weight=50.0, critical_window_days=21)

        # exactly 21 days out -> right at the edge of the window
        scored = score_order(order, config, as_of_date=date(2026, 1, 28))
        assert scored.score_breakdown["etd_proximity_or_backorder"] == 0.0

    def test_outside_window_gets_zero(self) -> None:
        order = _single_line_order(requested_delivery_date=date(2026, 6, 1))
        config = ScoringConfig(critical_window_days=21)
        scored = score_order(order, config, as_of_date=date(2026, 1, 1))
        assert scored.score_breakdown["etd_proximity_or_backorder"] == 0.0


class TestRecoverableQuantityContribution:
    def test_more_recoverable_quantity_scores_higher_below_saturation(self) -> None:
        line_small = make_line(
            "SO-200", "SO-200-L1", 40, REQUESTED,
            [confirmed(10, date(2026, 2, 10)), confirmed(30, date(2026, 3, 20))],
        )
        line_big = make_line(
            "SO-201", "SO-201-L1", 40, REQUESTED,
            [confirmed(35, date(2026, 2, 10)), confirmed(5, date(2026, 3, 20))],
        )
        [order_small] = analyze_orders([line_small])
        [order_big] = analyze_orders([line_big])

        config = ScoringConfig(
            abc_weights={"A": 0}, category_weights={}, bi_fs_weights={},
            seasonality_weight=0, backorder_weight=0, etd_proximity_max_weight=0,
            recoverable_saturation_point=100, recoverable_weight=100.0,
        )
        as_of = date(2026, 1, 1)
        score_small = score_order(order_small, config, as_of)
        score_big = score_order(order_big, config, as_of)

        assert order_small.recoverable_quantity == 10
        assert order_big.recoverable_quantity == 35
        # Below saturation (100), contribution scales linearly with qty.
        assert score_small.coefficient == pytest.approx(10.0)
        assert score_big.coefficient == pytest.approx(35.0)
        assert score_big.coefficient > score_small.coefficient

    def test_saturates_at_configured_point(self) -> None:
        """Recoverable quantity far beyond the saturation point should
        not out-contribute an order right at the saturation point —
        this is exactly the "700 low-value units shouldn't automatically
        dominate" behaviour the saturating formula is meant to enforce.
        """
        line_at_saturation = make_line(
            "SO-210", "SO-210-L1", 400, REQUESTED,
            [confirmed(400, date(2026, 3, 20))],
        )
        # will be NOK/late so recoverable=0; instead build via split so a
        # portion is genuinely recoverable at exactly the saturation point
        line_at_saturation = make_line(
            "SO-210", "SO-210-L1", 800, REQUESTED,
            [confirmed(400, date(2026, 2, 10)), confirmed(400, date(2026, 3, 20))],
        )
        line_way_over = make_line(
            "SO-211", "SO-211-L1", 1400, REQUESTED,
            [confirmed(700, date(2026, 2, 10)), confirmed(700, date(2026, 3, 20))],
        )
        [order_at] = analyze_orders([line_at_saturation])
        [order_over] = analyze_orders([line_way_over])

        config = ScoringConfig(
            abc_weights={}, category_weights={}, bi_fs_weights={},
            seasonality_weight=0, backorder_weight=0, etd_proximity_max_weight=0,
            recoverable_saturation_point=400, recoverable_weight=100.0,
        )
        as_of = date(2026, 1, 1)
        score_at = score_order(order_at, config, as_of)
        score_over = score_order(order_over, config, as_of)

        assert order_at.recoverable_quantity == 400
        assert order_over.recoverable_quantity == 700
        # Both saturate to the same max contribution despite very
        # different recoverable quantities.
        assert score_at.coefficient == pytest.approx(100.0)
        assert score_over.coefficient == pytest.approx(100.0)

    def test_small_a_class_backorder_can_outrank_large_low_priority_order(self) -> None:
        """The exact business scenario this rescaling is meant to fix:
        a small, A-class, already-backordered order should be able to
        outrank a much larger C-class order with no urgency."""
        small_urgent = make_line(
            "SO-220", "SO-220-L1", 50, date(2026, 1, 1),
            [confirmed(50, date(2026, 1, 1))],
            abc_class="A", bi_fs="BI", product_category="Small Appliances",
        )
        # Force a NOK/recoverable state on the small order via a second,
        # slightly-late line so recoverable_quantity > 0.
        small_urgent_blocker = make_line(
            "SO-220", "SO-220-L2", 5, date(2026, 1, 1),
            [confirmed(5, date(2026, 1, 20))],
            abc_class="C", bi_fs="FS", product_category="Storage",
        )
        large_low_priority = make_line(
            "SO-221", "SO-221-L1", 700, date(2026, 6, 1),
            [confirmed(350, date(2026, 2, 10)), confirmed(350, date(2026, 6, 20))],
            abc_class="C", bi_fs="FS", product_category="Storage",
        )
        [order_small] = analyze_orders([small_urgent, small_urgent_blocker])
        [order_large] = analyze_orders([large_low_priority])

        config = ScoringConfig()  # realistic defaults
        as_of = date(2026, 1, 15)  # small order is in backorder; large is far out

        scored = score_orders([order_small, order_large], config=config, as_of_date=as_of)
        assert scored[0].order.order_number == "SO-220"


class TestAggregationMethod:
    """The exact failure mode called out for `max` aggregation: one
    small A-class line shouldn't make a large C-class-dominated order
    score as if it were fully A-class under `quantity_weighted`."""

    def test_max_lets_small_a_class_line_dominate(self) -> None:
        line_a = make_line(
            "SO-400", "SO-400-L1", 10, REQUESTED, [confirmed(10, REQUESTED)],
            abc_class="A",
        )
        line_c = make_line(
            "SO-400", "SO-400-L2", 500, REQUESTED, [confirmed(500, REQUESTED)],
            abc_class="C",
        )
        [order] = analyze_orders([line_a, line_c])
        config = ScoringConfig(
            aggregation_method="max", abc_weights={"A": 60.0, "C": 10.0}
        )
        scored = score_order(order, config, as_of_date=REQUESTED)
        assert scored.score_breakdown["abc"] == 60.0

    def test_quantity_weighted_reflects_actual_volume_split(self) -> None:
        line_a = make_line(
            "SO-401", "SO-401-L1", 10, REQUESTED, [confirmed(10, REQUESTED)],
            abc_class="A",
        )
        line_c = make_line(
            "SO-401", "SO-401-L2", 500, REQUESTED, [confirmed(500, REQUESTED)],
            abc_class="C",
        )
        [order] = analyze_orders([line_a, line_c])
        config = ScoringConfig(
            aggregation_method="quantity_weighted", abc_weights={"A": 60.0, "C": 10.0}
        )
        scored = score_order(order, config, as_of_date=REQUESTED)
        expected = (10 * 60.0 + 500 * 10.0) / 510
        assert scored.score_breakdown["abc"] == pytest.approx(expected)
        assert scored.score_breakdown["abc"] < 60.0  # much closer to the C weight

    def test_invalid_aggregation_method_raises(self) -> None:
        line = make_line("SO-402", "SO-402-L1", 10, REQUESTED, [confirmed(10, REQUESTED)])
        [order] = analyze_orders([line])
        config = ScoringConfig(aggregation_method="bogus")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unrecognized aggregation_method"):
            score_order(order, config, as_of_date=REQUESTED)


class TestSeasonalCategoryBonus:
    """Legacy-derived: a flat bonus when a line's category is in its
    peak season. Also verifies the legacy AND-condition bug (which made
    the bonus never fire) is fixed here via month membership."""

    def test_bonus_applies_in_peak_month(self) -> None:
        order = _single_line_order(product_category="Outdoor")
        config = ScoringConfig(
            seasonal_category_bonus={"Outdoor": ((4, 5, 6), 100.0)}
        )
        scored = score_order(order, config, as_of_date=date(2026, 5, 15))
        assert scored.score_breakdown["seasonal_category_bonus"] == 100.0

    def test_bonus_does_not_apply_outside_peak_month(self) -> None:
        order = _single_line_order(product_category="Outdoor")
        config = ScoringConfig(
            seasonal_category_bonus={"Outdoor": ((4, 5, 6), 100.0)}
        )
        scored = score_order(order, config, as_of_date=date(2026, 9, 1))
        assert scored.score_breakdown["seasonal_category_bonus"] == 0.0

    def test_category_not_in_bonus_map_contributes_zero(self) -> None:
        order = _single_line_order(product_category="Furniture")
        config = ScoringConfig(
            seasonal_category_bonus={"Outdoor": ((4, 5, 6), 100.0)}
        )
        scored = score_order(order, config, as_of_date=date(2026, 5, 15))
        assert scored.score_breakdown["seasonal_category_bonus"] == 0.0

    def test_default_rules_are_the_synthetic_examples_and_fire_on_every_peak_month(self) -> None:
        """The shipped defaults are illustrative, not the original tool's rules. Every listed
        month fires (membership, not the AND-condition bug), and other months do not."""
        defaults = ScoringConfig().seasonal_category_bonus
        assert defaults == {
            "Small Domestic Appliances": ((10, 11, 12), 60.0),
            "Hoods": ((3, 4, 5), 60.0),
        }
        for category, (months, bonus) in defaults.items():
            order = _single_line_order(product_category=category)
            for month in range(1, 13):
                got = score_order(order, ScoringConfig(), as_of_date=date(2026, month, 15))
                expected = bonus if month in months else 0.0
                assert got.score_breakdown["seasonal_category_bonus"] == expected, (category, month)


class TestScoreOrdersRanksButNeverFilters:
    def test_all_orders_returned_regardless_of_score(self) -> None:
        ok_order = _single_line_order(order_number="SO-300", order_line_id="SO-300-L1")
        analyses = [ok_order]
        scored = score_orders(analyses, as_of_date=REQUESTED)
        assert len(scored) == len(analyses)

    def test_sorted_descending_by_coefficient(self) -> None:
        line_a = make_line(
            "SO-301", "SO-301-L1", 10, date(2026, 6, 1), [confirmed(10, date(2026, 6, 1))]
        )
        line_b = make_line(
            "SO-302", "SO-302-L1", 10, date(2026, 1, 1), [confirmed(10, date(2026, 1, 1))]
        )
        orders = analyze_orders([line_a, line_b])
        scored = score_orders(orders, as_of_date=date(2026, 1, 5))  # SO-302 is backorder

        coefficients = [s.coefficient for s in scored]
        assert coefficients == sorted(coefficients, reverse=True)
        assert scored[0].order.order_number == "SO-302"  # backorder ranks first
