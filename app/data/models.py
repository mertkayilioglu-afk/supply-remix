"""Domain models shared across the generator, scoring engine, and API layer.

These are plain dataclasses rather than pydantic models because they are
used internally (generation, pandas conversion, business logic). The API
layer (Step 4) defines its own pydantic request/response schemas that
wrap these where needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class AvailabilityStatus(str, Enum):
    """Status of a single production feedback chunk.

    Replaces the legacy sentinel-date pattern (e.g. 31.12.9999 meaning
    "no date yet"). UNPLANNED chunks carry `available_date = None`
    rather than a placeholder date, so date arithmetic downstream must
    explicitly handle the unplanned case instead of silently producing
    nonsense (e.g. "available in 2.9 million days").
    """

    CONFIRMED = "CONFIRMED"  # firm production date
    POTENTIAL = "POTENTIAL"  # tentative/forecast date, may still move
    UNPLANNED = "UNPLANNED"  # no date committed yet


@dataclass(frozen=True)
class Product:
    """A product master record. Attributes here are properties of the
    SKU itself, so every order line for a given SKU shares them."""

    sku: str
    product_category: str
    abc_class: str
    cbm_per_unit: float
    seasonality_factor: float
    supplier_country: str
    bi_fs: str  # "BI" (Built-In) or "FS" (Free-Standing)
    plant: str  # originating plant/factory code (synthetic codes, see GeneratorConfig)
    unit_price: float  # used to compute Net Amount (see LineSummary.total_amount)


@dataclass(frozen=True)
class Customer:
    customer_id: str
    customer_name: str
    region: str
    # Categories this customer's orders skew toward (see
    # GeneratorConfig.preferred_category_bias). Without this, a
    # customer's orders spread near-uniformly across all categories,
    # which makes same-customer-same-category candidate pools for the
    # remix engine rare in a small synthetic dataset — unrealistic,
    # since real B2B customers tend to order repeatedly within a
    # narrow product range.
    preferred_categories: tuple[str, ...]


@dataclass(frozen=True)
class ProductionFeedbackEntry:
    """One quantity chunk of a line with its own availability date.

    A single order line can have several of these when production
    output is staggered — this is the raw signal the availability
    splitter (Step 2) turns into separate logical records.

    `available_date` is None exactly when `status` is UNPLANNED — no
    committed date exists yet, so no date arithmetic should be
    performed on this chunk until it gets a real date.

    Deliberately NOT included here: whether this chunk is "recoverable"
    or whether its parent item is OK/NOK. Those require comparing this
    chunk against the other chunks of the same item/order, which is
    exactly what the Step 2 splitter/scoring engine computes — putting
    it here would duplicate that logic on the raw data.
    """

    available_qty: int
    available_date: date | None
    status: AvailabilityStatus


@dataclass(frozen=True)
class OrderLineSegment:
    """One quantity-level record produced by the availability splitter.

    This is the atomic unit the rest of the analysis works on: one
    order line's production feedback chunk, with its own on-time /
    recoverable status resolved against the order's `availability_of_mix`.

    `is_on_time`: this chunk's own available_date is on or before the
    requested delivery date (STRICT — not tolerance-banded). This is
    what "available" means for recoverability purposes. Always False
    for UNPLANNED chunks.

    `is_recoverable`: this chunk is on time BUT the order it belongs to
    cannot ship on time overall (mix_status is NOK, using the
    TOLERANCE-BAND definition — see AnalysisConfig) because some other
    chunk/line is late beyond the tolerance window, AND the gap between
    this chunk and the order's availability_of_mix meets the
    configured materiality threshold. This is the trapped, physically-
    ready volume the remix step targets. A chunk can never be both the
    cause of a NOK mix and recoverable itself, because a recoverable
    chunk is by definition on time.
    """

    segment_id: str
    order_number: str
    order_line_id: str
    customer_id: str
    customer_name: str
    sku: str
    product_category: str
    abc_class: str
    bi_fs: str
    plant: str
    unit_price: float
    seasonality_factor: float
    requested_delivery_date: date
    available_qty: int
    available_date: date | None
    status: AvailabilityStatus
    is_on_time: bool
    is_recoverable: bool
    cbm_per_unit: float
    # Set only when the working-day calendar moved available_date: the date the
    # production feedback originally gave. None means "not shifted".
    calendar_shifted_from: date | None = None


@dataclass(frozen=True)
class LineSummary:
    """Item-level (order line) rollup of its segments.

    `total_amount`/`recoverable_amount` are the monetary equivalents of
    `quantity`/`recoverable_quantity`: this line's Net Amount (quantity
    x unit_price, the same per-line computation as the original tool —
    see app/config/generator_config.py for the note) and the portion of it that's recoverable. Amount is NOT part
    of the criticality coefficient (see ScoringConfig) — it's a
    visibility figure for the pivot, not a scoring input, unless a
    future config change deliberately adds it.
    """

    order_line_id: str
    order_number: str
    sku: str
    product_category: str
    abc_class: str
    bi_fs: str
    plant: str
    seasonality_factor: float
    quantity: int
    on_time_quantity: int
    recoverable_quantity: int
    delayed_quantity: int
    unplanned_quantity: int
    total_amount: float  # Net Amount: quantity * unit_price for this line
    recoverable_amount: float  # recoverable_quantity * unit_price
    # The line's own "ready" date: the latest date across its segments.
    # None if any segment on this line is UNPLANNED.
    item_availability_date: date | None
    item_status: str  # "OK" or "NOK" — tolerance-band, see AnalysisConfig


@dataclass(frozen=True)
class OrderAnalysis:
    """Order-level (MIX) rollup: the core artifact the criticality
    scoring engine and, later, the remix engine both consume."""

    order_number: str
    customer_id: str
    customer_name: str
    requested_delivery_date: date
    # Latest availability date across all lines in the order. None if
    # any segment anywhere in the order is UNPLANNED (worst case: the
    # order has no resolvable ship date at all).
    availability_of_mix: date | None
    mix_status: str  # "OK" or "NOK" — tolerance-band, see AnalysisConfig
    total_quantity: int
    on_time_quantity: int
    recoverable_quantity: int
    delayed_quantity: int
    unplanned_quantity: int
    total_amount: float  # Net Amount summed across lines
    recoverable_amount: float  # recoverable portion of total_amount
    line_summaries: list[LineSummary]
    segments: list[OrderLineSegment]


@dataclass(frozen=True)
class OrderLine:
    """One product line on a customer order, as it exists in the
    order book before quantity-level splitting.

    `quantity` is the total ordered quantity for this line; it always
    equals the sum of `production_feedback[*].available_qty`.
    """

    order_number: str
    order_line_id: str
    customer_id: str
    customer_name: str
    sku: str
    product_category: str
    abc_class: str
    seasonality_factor: float
    supplier_country: str
    bi_fs: str
    plant: str  # originating plant/factory code
    unit_price: float  # used to compute Net Amount = quantity * unit_price
    quantity: int
    cbm_per_unit: float
    order_date: date
    requested_delivery_date: date
    production_feedback: list[ProductionFeedbackEntry] = field(default_factory=list)
