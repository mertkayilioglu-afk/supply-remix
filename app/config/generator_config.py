"""Configuration for the synthetic order-book generator.

Keeping every tunable value here (instead of scattered through the
generator code) means the dataset's realism/edge-case mix can be
adjusted without touching generation logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class GeneratorConfig:
    # --- reproducibility -----------------------------------------------
    seed: int = 42

    # --- population sizes ------------------------------------------------
    n_customers: int = 40
    n_products: int = 70
    n_orders: int = 480
    min_lines_per_order: int = 1
    max_lines_per_order: int = 4

    # --- customer category concentration -----------------------------------
    # Real customers tend to order repeatedly within a narrow product
    # range (a furniture retailer doesn't also buy lighting from the
    # same supplier every week). Without this, a small synthetic
    # dataset spreads each customer's orders near-uniformly across all
    # categories, making same-customer-same-category candidate pools
    # for the remix engine rare — starving Step 3 of the cross-order
    # combinations it exists to find.
    n_preferred_categories_per_customer: int = 2
    # Probability a given order line's product is drawn from the
    # customer's preferred categories rather than uniformly from all
    # products. Kept below 1.0 so customers still occasionally order
    # outside their usual range (realistic, and keeps some single-
    # category-group remixes from Step 3's earlier "degenerate case").
    preferred_category_bias: float = 0.8

    # --- anchor date -------------------------------------------------
    # All relative dates (requested delivery, production feedback) are
    # generated relative to this fixed "as-of" date rather than the
    # real wall-clock date, so the dataset is reproducible and always
    # contains a realistic mix of backorders / near-term / future dates
    # regardless of when the generator is actually run.
    simulated_today: date = date(2026, 4, 1)

    # requested delivery date offset from simulated_today, in days.
    # Negative values create orders that are already in backorder.
    delivery_offset_min_days: int = -30
    delivery_offset_max_days: int = 75

    # --- quantities & dimensions -----------------------------------------
    quantity_min: int = 5
    quantity_max: int = 250

    # --- product master ------------------------------------------------
    # This tool's domain is home appliances, not general homeware — BI/FS (Built-In vs.
    # Free-Standing) is a real, meaningful distinction for an oven or a
    # dishwasher and meaningless for, say, a tablecloth. Categories are
    # deliberately every one an appliance category so bi_fs stays
    # meaningful everywhere it's used as a scoring input.
    categories: tuple[str, ...] = (
        "Refrigerators",
        "Washing Machines",
        "Tumble Dryers",
        "Dishwashers",
        "Ovens",
        "Hobs",
        "Hoods",
        "Small Domestic Appliances",
    )

    # Per-category unit volume (m^3) and unit price ($) ranges. These
    # matter beyond cosmetics: CBM drives container packing (a washing
    # machine and a small appliance have wildly different volumes, so
    # packing only behaves realistically with realistic per-category
    # volumes) and unit price drives Net Amount aggregation (small
    # domestic appliances are deliberately low volume AND low value
    # relative to large white goods, which changes how a container
    # fills and how Net Amount rolls up — a container full of kettles
    # looks very different from one full of fridges, in both weight
    # and euros).
    category_cbm_range: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "Refrigerators": (0.45, 1.10),       # large standing white good
            "Washing Machines": (0.35, 0.65),    # large white good
            "Tumble Dryers": (0.30, 0.60),       # similar footprint to washers
            "Dishwashers": (0.25, 0.45),         # built-in-sized white good
            "Ovens": (0.06, 0.14),               # built-in cavity, compact
            "Hobs": (0.02, 0.05),                # flat, thin built-in unit
            "Hoods": (0.05, 0.16),               # moderate built-in unit
            "Small Domestic Appliances": (0.004, 0.03),  # kettles, toasters, etc.
        }
    )
    category_unit_price_range: dict[str, tuple[float, float]] = field(
        default_factory=lambda: {
            "Refrigerators": (300.0, 1600.0),
            "Washing Machines": (250.0, 950.0),
            "Tumble Dryers": (200.0, 750.0),
            "Dishwashers": (250.0, 850.0),
            "Ovens": (200.0, 950.0),
            "Hobs": (150.0, 750.0),
            "Hoods": (100.0, 500.0),
            "Small Domestic Appliances": (15.0, 150.0),
        }
    )

    # Baseline seasonality factor per category (>1 = amplifies demand
    # sensitivity / risk during peak season, <1 = dampens it). This is
    # a starting point per product; the generator adds per-product jitter.
    category_seasonality_baseline: dict[str, float] = field(
        default_factory=lambda: {
            "Refrigerators": 1.1,             # mild summer peak
            "Washing Machines": 1.0,
            "Tumble Dryers": 1.3,              # winter demand peak
            "Dishwashers": 0.9,
            "Ovens": 1.0,
            "Hobs": 0.9,
            "Hoods": 0.9,
            "Small Domestic Appliances": 1.2,  # gifting-season peak
        }
    )
    abc_class_weights: dict[str, float] = field(
        default_factory=lambda: {"A": 0.25, "B": 0.35, "C": 0.40}
    )
    # SYNTHETIC supplier "countries": ISO 3166-1 reserves XA-XZ for user
    # assignment, so these codes can never be mistaken for a real country.
    supplier_countries: tuple[str, ...] = ("XA", "XB", "XC", "XD", "XE")

    # Originating plant/factory code. Modeled as 1-2 plants per supplier
    # country, since a country typically has a small number of factory
    # sites in this kind of dataset. The code format is deliberately
    # synthetic (e.g. "PLANT-XA1"), not an ERP-style plant code.
    plants_per_country: int = 2
    plant_code_format: str = "PLANT-{country}{number}"

    # Unit price per product, used to compute Net Amount (quantity x
    # unit_price — see LineSummary.total_amount). The per-line,
    # quantity-times-unit-price computation follows the original tool:
    # it took a total amount and total quantity from an external sales
    # report for the exact (order, material, item) combination, derived
    # a unit amount (amount / quantity), then wrote
    # `Net Amount = this_line_qty * unit_amount`. We don't have an
    # equivalent external pricing report for synthetic data, so —
    # consistent with putting a price on the product master rather than
    # inventing a new mechanism — unit_price lives directly on the
    # product master (per category range, see category_unit_price_range
    # above) and Net Amount is computed the same way (qty * unit_price)
    # at the line level.

    # Built-In vs Free-Standing classification. This is an independent
    # product dimension from `categories` above (e.g. a refrigerator can
    # be BI or FS) and is one of the criticality scoring inputs.
    bi_fs_weights: dict[str, float] = field(default_factory=lambda: {"BI": 0.45, "FS": 0.55})

    # --- production feedback (availability split) -------------------------
    # Probability a line ships complete on a single date vs. being split
    # across 2 or 3 production feedback dates.
    prob_single_date: float = 0.55
    prob_two_dates: float = 0.30
    prob_three_dates: float = 0.15

    # Each line has exactly one "risk" chunk that can land late: the
    # single chunk itself for single-date lines, or the last chunk for
    # split lines. `prob_delay_chunk` is the probability that risk chunk
    # actually lands late. This is the main lever for the overall
    # NOK rate of the generated order book: with ~2.5 lines per order on
    # average, a per-line NOK order-level rate of `p` needs a per-chunk
    # prob_delay_chunk considerably lower than `p`, since ANY late/
    # unplanned line on an order makes the whole order NOK.
    # (At the current defaults this yields roughly 15-25% of orders NOK,
    # which is what tends to make a criticality ranking useful — with
    # everything at risk, ranking has nothing to prioritise; see also
    # prob_status_unplanned below, which independently pushes toward NOK.)
    prob_delay_chunk: float = 0.08

    # Offsets are in days relative to the order's requested delivery
    # date. Non-risk chunks (and the risk chunk when it doesn't land
    # late) are drawn from the "on time" range; a chunk that does land
    # late is drawn from the "late" range.
    on_time_offset_min_days: int = -18
    on_time_offset_max_days: int = 0
    late_offset_min_days: int = 3
    late_offset_max_days: int = 55

    # Status of each production feedback chunk, mirroring how a real
    # production feedback report distinguishes a firm date from a
    # forecast from "not yet scheduled" (the kind of report that produces
    # 9999-12-31-style sentinel dates). Sampled independently per
    # chunk; must sum to 1.0. `prob_status_unplanned` also pushes orders
    # toward NOK (an unplanned chunk always blocks the mix), so it's
    # kept low for the same reason `prob_delay_chunk` is kept low.
    #   CONFIRMED  -> firm production date, used as given
    #   POTENTIAL  -> tentative/forecast date, may still move
    #   UNPLANNED  -> no date committed yet; available_date is None
    prob_status_confirmed: float = 0.87
    prob_status_potential: float = 0.12
    prob_status_unplanned: float = 0.01
