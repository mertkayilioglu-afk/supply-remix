"""Configuration for the criticality scoring engine.

Every weight and threshold used to compute an order's coefficient
lives here. The scoring engine (`app/core/scoring.py`) never hard-codes
a number — it only reads from a `ScoringConfig` instance.

Design notes on how each input is aggregated to order level, since an
order can contain multiple lines with different ABC classes,
categories, etc. See `aggregation_method` below for the two supported
strategies and their trade-off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class ScoringConfig:
    # --- line -> order aggregation --------------------------------------
    # How ABC class, category, BI/FS, and seasonality weights (each a
    # per-line value) are rolled up into a single order-level value.
    #   "max"              -> the order takes the highest weight among
    #                         its lines. Simple and defensible ("one
    #                         A-class line makes the order worth
    #                         attention"), but has a real failure mode:
    #                         an order with one A-class line of 10 units
    #                         and four C-class lines of 500 units gets
    #                         scored as if it were fully A-class, even
    #                         though the A-class line is a small fraction
    #                         of the shipment.
    #   "quantity_weighted" -> the order takes the quantity-weighted
    #                         average across its lines, so a large
    #                         low-priority line pulls the score down
    #                         (and a large high-priority line pulls it
    #                         up) proportionally to how much volume it
    #                         actually represents.
    # Defaults to "max" to match the tool's original behaviour.
    aggregation_method: Literal["max", "quantity_weighted"] = "max"

    # --- critical window --------------------------------------------------
    # Orders whose requested delivery date falls within this many days
    # (from the as-of date) are inside the "critical window" and start
    # accruing ETD-proximity score. Also used as the denominator for the
    # linear proximity taper below.
    critical_window_days: int = 21

    # --- ABC classification -----------------------------------------------
    abc_weights: dict[str, float] = field(
        default_factory=lambda: {"A": 60.0, "B": 30.0, "C": 10.0}
    )

    # --- product category -------------------------------------------------
    # Configurable per-category weight. Categories not listed default to 0.
    category_weights: dict[str, float] = field(
        default_factory=lambda: {
            "Refrigerators": 25.0,
            "Washing Machines": 22.0,
            "Tumble Dryers": 15.0,
            "Dishwashers": 18.0,
            "Ovens": 15.0,
            "Hobs": 12.0,
            "Hoods": 10.0,
            "Small Domestic Appliances": 8.0,
        }
    )

    # --- Built-In vs Free-Standing -----------------------------------------
    bi_fs_weights: dict[str, float] = field(
        default_factory=lambda: {"BI": 70.0, "FS": 30.0}
    )

    # --- seasonality --------------------------------------------------
    # Contribution = seasonality_weight * max(seasonality_factor across lines).
    # Seasonality factors are centered around ~1.0, so this weight is
    # roughly the score contribution for an average-seasonality order.
    seasonality_weight: float = 20.0

    # --- seasonal category bonus -----------------------------------------
    # The original tool encoded seasonality not as a continuous factor
    # but as a few hard-coded category+month special cases. We keep our
    # generic `category_weights` above AND add this mechanism alongside
    # it, since they capture different things: `category_weights` is a
    # static priority per category, while this is a bonus that only
    # applies during that category's peak season.
    #
    # The defaults below are SYNTHETIC, illustrative examples (not the
    # original tool's rules): each entry is category -> (peak months,
    # bonus). Replace them with your own business's peaks.
    #
    # NOTE: in the original tool, one such month condition was written
    # with AND across several months, so it could never be true and that
    # bonus never fired. We use membership (`month in months`) instead,
    # deliberately fixing that bug rather than reproducing it.
    seasonal_category_bonus: dict[str, tuple[tuple[int, ...], float]] = field(
        default_factory=lambda: {
            "Small Domestic Appliances": ((10, 11, 12), 60.0),  # synthetic example
            "Hoods": ((3, 4, 5), 60.0),  # synthetic example
        }
    )

    # --- backorder / ETD proximity -----------------------------------------
    # An order whose requested delivery date has already passed (as of
    # the analysis date) is in backorder: flat, maximum urgency weight.
    backorder_weight: float = 80.0
    # An order whose requested date is still ahead but within the
    # critical window scores on a linear taper from 0 (at the edge of
    # the window) up to this max weight (at 0 days remaining / due today).
    etd_proximity_max_weight: float = 50.0

    # --- recoverable quantity -----------------------------------------
    # Saturating (not linear) contribution: fraction = min(qty / saturation, 1.0),
    # contribution = fraction * recoverable_weight. A raw per-unit weight
    # was tried first and rejected: at 0.5/unit, a single order with a
    # few hundred recoverable units (common in this dataset) produced a
    # contribution of 150-350, dwarfing every other component (which cap
    # in the 10-80 range). That turns the "weighted coefficient" into a
    # sort by recoverable quantity with decoration on top, and it's
    # wrong on the business side too: a large low-value order shouldn't
    # automatically outrank a small A-class, already-backordered one.
    #
    # `recoverable_saturation_point` = 400 units: in this dataset, among
    # orders with any recoverable quantity, the median is ~270 and the
    # 75th percentile is ~390 units (see scripts/demo_step2.py output).
    # 400 represents a genuinely large recovery opportunity — an order
    # needs to be in roughly the top quartile of recoverable volume to
    # max out this component, while a modest-but-real recoverable
    # quantity (e.g. 50-150 units) still contributes proportionally
    # rather than being rounded down to "doesn't matter."
    recoverable_saturation_point: int = 400
    # Capped at a weight comparable to the other components (backorder
    # is 80, the ABC/BI-FS/category/seasonality ceiling is roughly
    # 60+70+25+28=183 combined) so recoverable volume is a meaningful
    # factor in the ranking without being able to single-handedly
    # outrank everything else regardless of business priority.
    recoverable_weight: float = 100.0
