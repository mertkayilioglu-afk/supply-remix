"""Criticality scoring engine.

Computes a weighted coefficient per order so a human can rank and
review at-risk orders. This module NEVER filters or acts on the data —
it only scores and returns everything, sorted. What to do about any
given order is a human decision (see Step 4's /analyze endpoint and
Step 3's remix, which only runs once a human has picked an order).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

from app.config.scoring_config import ScoringConfig
from app.core.errors import StageError
from app.data.models import LineSummary, OrderAnalysis


@dataclass(frozen=True)
class ScoredOrder:
    """An OrderAnalysis plus its criticality coefficient and a
    transparent breakdown of how that coefficient was built up —
    useful for explaining *why* an order ranked where it did.
    """

    order: OrderAnalysis
    coefficient: float
    score_breakdown: dict[str, float]


def _aggregate_line_weights(
    order: OrderAnalysis,
    config: ScoringConfig,
    weight_fn: Callable[[LineSummary], float],
) -> float:
    """Roll up a per-line weight to a single order-level value, using
    whichever aggregation method the config specifies. See
    `ScoringConfig.aggregation_method` for the trade-off between the
    two supported strategies.
    """
    if not order.line_summaries:
        return 0.0

    if config.aggregation_method == "max":
        return max(weight_fn(ls) for ls in order.line_summaries)

    if config.aggregation_method == "quantity_weighted":
        total_qty = sum(ls.quantity for ls in order.line_summaries)
        if total_qty == 0:
            return 0.0
        return (
            sum(weight_fn(ls) * ls.quantity for ls in order.line_summaries) / total_qty
        )

    raise StageError(
        stage="Step 2 — criticality scoring",
        message="Unrecognized aggregation_method in ScoringConfig.",
        expected="'max' or 'quantity_weighted'",
        received=repr(config.aggregation_method),
    )
def _abc_component(order: OrderAnalysis, config: ScoringConfig) -> float:
    return _aggregate_line_weights(
        order, config, lambda ls: config.abc_weights.get(ls.abc_class, 0.0)
    )


def _category_component(order: OrderAnalysis, config: ScoringConfig) -> float:
    return _aggregate_line_weights(
        order, config, lambda ls: config.category_weights.get(ls.product_category, 0.0)
    )


def _bi_fs_component(order: OrderAnalysis, config: ScoringConfig) -> float:
    return _aggregate_line_weights(
        order, config, lambda ls: config.bi_fs_weights.get(ls.bi_fs, 0.0)
    )


def _seasonality_component(order: OrderAnalysis, config: ScoringConfig) -> float:
    factor = _aggregate_line_weights(order, config, lambda ls: ls.seasonality_factor)
    return config.seasonality_weight * factor


def _seasonal_category_bonus_component(
    order: OrderAnalysis, config: ScoringConfig, as_of_date: date
) -> float:
    """Legacy-derived: a flat bonus when a line's category is in its
    configured peak season (as of the analysis date), on top of the
    generic `category_weights`. See ScoringConfig.seasonal_category_bonus
    for the mapping and the legacy bug this deliberately fixes.
    """

    def line_bonus(ls: LineSummary) -> float:
        rule = config.seasonal_category_bonus.get(ls.product_category)
        if rule is None:
            return 0.0
        peak_months, bonus = rule
        return bonus if as_of_date.month in peak_months else 0.0

    return _aggregate_line_weights(order, config, line_bonus)


def _etd_proximity_component(
    order: OrderAnalysis, config: ScoringConfig, as_of_date: date
) -> float:
    """Backorder gets a flat, maximum-urgency weight. Otherwise, orders
    inside the critical window get a linear taper: closer to the
    requested date scores higher, capped at `etd_proximity_max_weight`
    for an order due today, tapering to 0 at the edge of the window.
    """
    days_until = (order.requested_delivery_date - as_of_date).days

    if days_until < 0:
        return config.backorder_weight

    if days_until <= config.critical_window_days:
        proximity_fraction = 1 - (days_until / config.critical_window_days)
        return config.etd_proximity_max_weight * proximity_fraction

    return 0.0


def _recoverable_quantity_component(order: OrderAnalysis, config: ScoringConfig) -> float:
    """Saturating, not linear: fraction of the way to a "large recovery
    opportunity" (see ScoringConfig.recoverable_saturation_point),
    capped at recoverable_weight. This keeps recoverable quantity
    comparable in scale to the other components instead of swamping
    them — see the config comments for why a raw per-unit weight was
    rejected.

    Note: an OK order always has recoverable_quantity == 0 (nothing is
    trapped when the whole order already ships on time), so it earns
    nothing from this component. That alone does NOT keep OK orders out
    of the ranking -- the ABC, category, BI/FS, seasonality and backorder
    components still score them. "OK orders never compete" is enforced by
    the /analyze stage, which ranks NOK orders only (see
    app.api.main._build_analyze_response). That is deliberate: the tool
    exists to release trapped volume, and an OK order has none to release.
    """
    if config.recoverable_saturation_point <= 0:
        raise ValueError("recoverable_saturation_point must be positive")
    fraction = min(order.recoverable_quantity / config.recoverable_saturation_point, 1.0)
    return fraction * config.recoverable_weight


def score_order(
    order: OrderAnalysis, config: ScoringConfig, as_of_date: date
) -> ScoredOrder:
    breakdown = {
        "abc": _abc_component(order, config),
        "category": _category_component(order, config),
        "bi_fs": _bi_fs_component(order, config),
        "seasonality": _seasonality_component(order, config),
        "seasonal_category_bonus": _seasonal_category_bonus_component(
            order, config, as_of_date
        ),
        "etd_proximity_or_backorder": _etd_proximity_component(order, config, as_of_date),
        "recoverable_quantity": _recoverable_quantity_component(order, config),
    }
    coefficient = sum(breakdown.values())
    return ScoredOrder(order=order, coefficient=coefficient, score_breakdown=breakdown)


def score_orders(
    orders: list[OrderAnalysis],
    config: ScoringConfig | None = None,
    as_of_date: date | None = None,
) -> list[ScoredOrder]:
    """Score every order and return them ranked by coefficient,
    descending. This is a review output for a human — nothing here
    filters, discards, or acts on an order regardless of its score.

    CHECKPOINT (deliberate — see app/core/errors.py for the full
    rationale): this ranked list is meant to be looked at before
    anyone picks an order to remix. This function does not call the
    remix engine itself, and callers on its behalf (including any AI
    layer wrapping this API) should not do so automatically either.
    """
    config = config or ScoringConfig()
    as_of_date = as_of_date or date.today()

    scored = [score_order(order, config, as_of_date) for order in orders]
    return sorted(scored, key=lambda s: s.coefficient, reverse=True)
