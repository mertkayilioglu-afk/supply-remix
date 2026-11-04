"""Pydantic request/response schemas for the API layer.

Every field carries a `description` so the auto-generated docs at
/docs are readable on their own — a planner (or another engineer)
should be able to understand what a field means without reading the
source. No endpoint accepts or returns a raw dict/list at the top
level; everything is a typed model.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Shared input: the order book, as sent to both /analyze and /remix
# ---------------------------------------------------------------------------


class ProductionFeedbackEntryIn(BaseModel):
    """One quantity-level availability record for a line: a chunk of
    its total quantity, with its own date and confirmation status."""

    available_qty: int = Field(..., gt=0, description="Units available on this specific date.")
    available_date: date | None = Field(
        None,
        description=(
            "Date this quantity becomes available. Null means the status is "
            "UNPLANNED (no production date committed yet) — never a placeholder "
            "date like 9999-12-31."
        ),
    )
    status: Literal["CONFIRMED", "POTENTIAL", "UNPLANNED"] = Field(
        ...,
        description=(
            "CONFIRMED = firm production date. POTENTIAL = tentative/forecast, "
            "may still move. UNPLANNED = no date committed yet; available_date "
            "must be null in that case."
        ),
    )

    @model_validator(mode="after")
    def _unplanned_requires_null_date(self) -> "ProductionFeedbackEntryIn":
        if self.status == "UNPLANNED" and self.available_date is not None:
            raise ValueError("available_date must be null when status is UNPLANNED")
        if self.status != "UNPLANNED" and self.available_date is None:
            raise ValueError("available_date is required unless status is UNPLANNED")
        return self


class OrderLineIn(BaseModel):
    """One product line on a customer order, as it exists before
    quantity-level splitting. `quantity` must equal the sum of
    `production_feedback[*].available_qty`."""

    order_number: str = Field(
        ..., description="Customer order number. Multiple lines can share one order number."
    )
    order_line_id: str = Field(..., description="Unique identifier for this line within the order.")
    customer_id: str = Field(
        ...,
        description=(
            "Ship-to customer identifier. Remix candidates are restricted to the "
            "same customer, since these orders ship direct to the customer."
        ),
    )
    customer_name: str = Field(..., description="Display name for the customer.")
    sku: str = Field(..., description="Product SKU.")
    product_category: str = Field(
        ...,
        description=(
            "Product category. Remix candidates are restricted to the same (or "
            "an explicitly linked) category, since goods in different categories "
            "ship from different plants of origin."
        ),
    )
    abc_class: str = Field(..., description="ABC classification (e.g. 'A', 'B', 'C'); used in criticality scoring.")
    seasonality_factor: float = Field(
        ..., description="Relative seasonal demand sensitivity for this product; used in criticality scoring."
    )
    supplier_country: str = Field(..., description="ISO-ish country code of the supplying factory.")
    bi_fs: str = Field(
        ..., description="Built-In ('BI') or Free-Standing ('FS') classification; used in criticality scoring."
    )
    plant: str = Field(
        ...,
        description=(
            "Originating plant/factory code. Remix candidates are restricted to the "
            "same customer and category — plant is not itself a remix constraint, but "
            "is used as a pivot filter, since it's the point of origin a planner "
            "reasons about operationally."
        ),
    )
    unit_price: float = Field(
        ..., gt=0, description="Unit price used to compute Net Amount (quantity x unit_price) for this line."
    )
    quantity: int = Field(..., gt=0, description="Total ordered quantity for this line.")
    cbm_per_unit: float = Field(..., gt=0, description="Volume per unit in cubic meters; used for container utilization.")
    order_date: date = Field(..., description="Date the order was placed.")
    requested_delivery_date: date = Field(..., description="The date the customer requested this order.")
    production_feedback: list[ProductionFeedbackEntryIn] = Field(
        ...,
        min_length=1,
        description="Quantity-level availability: how this line's total quantity is split across production dates.",
    )


# ---------------------------------------------------------------------------
# POST /analyze
# ---------------------------------------------------------------------------


class AnalyzeRequest(BaseModel):
    order_lines: list[OrderLineIn] = Field(
        ..., min_length=1, description="The order book to analyze."
    )
    as_of_date: date | None = Field(
        None,
        description=(
            "Reference 'today' used for backorder/proximity scoring. "
            "Defaults to the current date if omitted."
        ),
    )


class PlantBreakdownEntry(BaseModel):
    """This order's quantity/amount contribution from one plant. An
    order can span multiple plants since its lines can be different
    products sourced from different factories. Used by the pivot view
    to filter and re-aggregate by plant without another API round trip."""

    plant: str = Field(..., description="Originating plant/factory code.")
    quantity: int = Field(..., description="Quantity on this order sourced from this plant.")
    recoverable_quantity: int = Field(..., description="Recoverable quantity from this plant.")
    total_amount: float = Field(..., description="Net Amount from this plant (quantity x unit_price).")
    recoverable_amount: float = Field(..., description="Recoverable portion of total_amount from this plant.")


class AtRiskOrderOut(BaseModel):
    order_number: str = Field(..., description="The order number.")
    customer_id: str = Field(..., description="Ship-to customer identifier.")
    customer_name: str = Field(..., description="Display name for the customer.")
    requested_delivery_date: date = Field(..., description="The date the customer requested this order.")
    availability_of_mix: date | None = Field(
        ...,
        description=(
            "Latest availability date across all lines in the order. Null if "
            "any line is still UNPLANNED — the order has no resolvable ship "
            "date at all, treated as the worst case."
        ),
    )
    mix_status: Literal["OK", "NOK"] = Field(
        ...,
        description=(
            "NOK if the order's overall delay exceeds the configured tolerance "
            "window; these are the orders needing attention. Always NOK here, "
            "since /analyze only returns actionable (NOK) orders."
        ),
    )
    total_quantity: int = Field(..., description="Total ordered quantity across all lines on the order.")
    recoverable_quantity: int = Field(
        ...,
        description=(
            "Quantity that is physically ready on or before the requested date "
            "but blocked by a slower line on the same order — the volume a "
            "remix could release early."
        ),
    )
    total_amount: float = Field(
        ...,
        description=(
            "Net Amount across the order (sum of quantity x unit_price per line). "
            "A monetary view alongside total_quantity — 200 delayed units of a "
            "low-value item is not the same priority as 200 units of a high-value one."
        ),
    )
    recoverable_amount: float = Field(
        ..., description="The recoverable_quantity portion of total_amount — the monetary value trapped, not just the volume."
    )
    plant_breakdown: list[PlantBreakdownEntry] = Field(
        ...,
        description="This order's quantity/amount split by originating plant, for pivot filtering.",
    )
    coefficient: float = Field(
        ...,
        description=(
            "Step 2 criticality score: higher means more worth reviewing first. "
            "Combines ABC class, category, seasonality, backorder/proximity, "
            "and recoverable quantity per the configured weights."
        ),
    )


class AnalyzeResponse(BaseModel):
    as_of_date: date = Field(..., description="The reference date used for this analysis.")
    at_risk_orders: list[AtRiskOrderOut] = Field(
        ...,
        description=(
            "NOK (actionable) orders only, ranked by coefficient descending. "
            "This is a review list for a human — nothing here is auto-actioned."
        ),
    )


# ---------------------------------------------------------------------------
# POST /remix
# ---------------------------------------------------------------------------


class RemixRequest(BaseModel):
    order_lines: list[OrderLineIn] = Field(
        ...,
        min_length=1,
        description=(
            "The order book. Must include the target order plus enough of the "
            "surrounding book (same customer's other orders) for candidate "
            "search to find anything — remix needs visibility beyond just the "
            "target order itself."
        ),
    )
    target_order_number: str = Field(..., description="The order number selected by the planner to remix.")
    as_of_date: date | None = Field(
        None, description="Reference 'today' for scoring the target order's coefficient. Defaults to the current date."
    )


class ProposalRequest(RemixRequest):
    """The proposal document and email cover the WHOLE target order's
    container plan, not one container at a time."""


class ContainerLineOut(BaseModel):
    order_number: str = Field(..., description="Original order this quantity belongs to.")
    order_line_id: str = Field(..., description="Original order line.")
    sku: str = Field(..., description="Product SKU.")
    product_category: str = Field(..., description="Product category.")
    quantity: int = Field(..., description="Units of this line placed in this container.")
    available_date: date | None = Field(None, description="This quantity's own availability date. Null = UNPLANNED (no production date yet).")
    requested_delivery_date: date = Field(..., description="This line's original requested delivery date.")
    line_status: Literal["OK", "NOK"] = Field(..., description="OK if its delay vs. its own requested date is within tolerance; UNPLANNED is always NOK.")
    calendar_shifted_from: date | None = Field(
        None,
        description="Set only when the working-day calendar moved available_date off a weekend/holiday: the original date.",
    )
    cbm: float = Field(..., description="Volume of this quantity in CBM.")
    net_amount: float = Field(..., description="Net Amount: quantity x unit price.")


class ShipmentContainerOut(BaseModel):
    """One proposed container/order. Containers are ordered by date; late and
    UNPLANNED units are included (shown NOK) rather than dropped, so the
    customer sees them waiting instead of cancelled."""

    reference: str = Field(..., description="New order reference for this container.")
    category_group: str = Field(..., description="Category group (a container never mixes groups).")
    index: int = Field(..., description="1 = earliest container.")
    status: Literal["OK", "NOK"] = Field(..., description="OK only if every unit in it is OK; otherwise it waits for its latest unit.")
    availability_date: date | None = Field(None, description="Latest unit date in the container. Null if any unit is UNPLANNED.")
    utilization: float = Field(..., description="Fraction of capacity used, e.g. 0.998 = 99.8%. Always at least the configured floor (94.5%).")
    total_cbm: float = Field(..., description="Volume in CBM.")
    total_quantity: int = Field(..., description="Total units.")
    ok_quantity: int = Field(..., description="Units that are OK.")
    net_amount: float = Field(..., description="Net Amount of everything in the container.")
    lines: list[ContainerLineOut] = Field(..., description="Contents, ordered by availability date.")


class GroupFailureOut(BaseModel):
    category_group: str = Field(..., description="The category group that could not be containerised.")
    reason: Literal["insufficient_volume", "cannot_balance"] = Field(..., description="Why.")
    message: str = Field(..., description="Human-readable explanation.")
    total_cbm: float = Field(..., description="Pool volume.")
    containers_needed: int = Field(..., description="Containers the volume would need.")


class RemixResponse(BaseModel):
    target_order_number: str = Field(..., description="The order the planner selected.")
    customer_id: str = Field(..., description="Ship-to customer identifier.")
    target_order_coefficient: float = Field(
        ...,
        description="The target order's criticality coefficient, shown beside the result and deliberately not used in packing.",
    )
    category_groups: list[str] = Field(..., description="Category groups considered (those of the target order).")
    containers: list[ShipmentContainerOut] = Field(
        ..., description="Every container, per group ordered by date, each at least 94.5% full."
    )
    group_failures: list[GroupFailureOut] = Field(
        ..., description="Category groups for which containers could not be created, with reasons."
    )
    no_proposal_reason: Literal["no_candidates", "insufficient_volume", "cannot_balance", "mixed"] | None = Field(
        None,
        description=(
            "Set only when there are no containers at all. 'no_candidates' = the order is "
            "already OK or the customer has no NOK units in its category group. "
            "'insufficient_volume' = total volume < 94.5% x container count, so containers "
            "cannot be created. 'cannot_balance' = enough volume, but no distribution keeps "
            "every container within 94.5%-100% without borrowing from a fully-OK container. "
            "'mixed' = the category groups failed for different reasons; see group_failures for each one."
        ),
    )
    message: str | None = Field(None, description="Human-readable explanation.")


class AssistantRequest(BaseModel):
    message: str = Field(..., min_length=3, description="Plain-language request, e.g. 'which orders are most at risk?' or 'remix SO-100203'.")
    order_lines: list[OrderLineIn] = Field(..., min_length=1, description="The order book.")
    as_of_date: date | None = Field(None, description="Reference 'today'.")


class AssistantResponse(BaseModel):
    """What /assistant ran (exactly ONE stage) and a plain-language explanation
    of the engine's own output. Any later stage is listed, never executed."""

    stage_run: Literal["analyze", "remix"] = Field(..., description="The single stage that was run.")
    routed_by: Literal["rules", "llm"] = Field(..., description="Whether routing came from the model or the rule-based fallback.")
    deferred_stages: list[str] = Field(
        ..., description="Stages the request also mentioned. Not run: a human triggers each one deliberately."
    )
    result: AnalyzeResponse | RemixResponse = Field(..., description="The engine output of the stage that ran.")
    explanation: str = Field(
        ...,
        description="Plain-language summary. Only restates engine numbers; otherwise a deterministic template is used.",
    )
    next_step: str = Field(..., description="What the human could do next. Nothing is done automatically.")


class ErrorResponse(BaseModel):
    detail: str = Field(..., description="A human-readable explanation of what went wrong.")


class StageErrorResponse(BaseModel):
    """Returned when a pipeline stage receives data it cannot process.
    Deliberately structured (stage/expected/received), not a bare
    string — see app/core/errors.py for why: a checkpoint should fail
    by pointing straight at itself, not require debugging the whole
    pipeline to find where things went wrong."""

    stage: str = Field(..., description="Which pipeline stage raised this error (e.g. 'Step 2 — availability splitter').")
    detail: str = Field(..., description="The full human-readable error message.")
    expected: str | None = Field(None, description="What this stage expected to receive.")
    received: str | None = Field(None, description="What this stage actually received.")


# ---------------------------------------------------------------------------
# Review dashboard (GET /dashboard)
# ---------------------------------------------------------------------------


class NotSplitNote(BaseModel):
    categories: str = Field(..., description="The target order's categories in the group that was not split.")
    reason: Literal["insufficient_volume", "cannot_balance"] = Field(..., description="The engine's reason code.")
    detail: str = Field(..., description="The engine's own explanation for this group.")


class DashboardTarget(BaseModel):
    order_number: str
    customer_name: str
    requested_delivery_date: date
    availability_of_mix: date | None = Field(None, description="Null if any unit is UNPLANNED.")


class DashboardPlanResponse(BaseModel):
    """Step 2 of the dashboard: ONE order's container plan plus its plain-language explanation."""

    target: DashboardTarget
    remix: RemixResponse
    explanation: str = Field(..., description="Assistant-layer text; only restates engine numbers.")
    explanation_source: Literal["template", "model"] = Field(
        ..., description="'template' = deterministic text (no model, or the model's text was rejected)."
    )
    no_proposal_headline: str | None = Field(None, description="Plain-language headline when nothing was built.")
    not_split: list[NotSplitNote] = Field(..., description="Category groups not split, with the engine's reason.")


class EmailPreviewResponse(BaseModel):
    """Text preview of the draft customer email. Nothing is sent; there is no send capability."""

    draft_only: Literal[True] = True
    to: str
    subject: str
    body: str
