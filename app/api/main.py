"""FastAPI service exposing the analysis and remix engines.

Checkpoints (each one is called deliberately; none triggers the next):
  POST /analyze, /pivot, /pivot/export.xlsx -- ranked at-risk (NOK) orders.
  POST /remix                                -- date-ordered containers for ONE chosen order.
  POST /remix/proposal.xlsx, /remix/proposal-email.eml -- customer document and DRAFT email.
  POST /assistant                            -- natural language -> ONE of /analyze or /remix.

All take the order book as part of the request body (this is a
stateless service — no database yet), since the remix engine needs
visibility into the target customer's other orders, not just the
target order itself.
"""

from __future__ import annotations

import math

from datetime import date

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import ValidationError

from app.api.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    AtRiskOrderOut,
    AssistantRequest,
    AssistantResponse,
    ContainerLineOut,
    ErrorResponse,
    GroupFailureOut,
    OrderLineIn,
    ProductionFeedbackEntryIn,
    PlantBreakdownEntry,
    ProposalRequest,
    RemixRequest,
    RemixResponse,
    ShipmentContainerOut,
    StageErrorResponse,
)
from app.ai.assistant import LLM, default_llm, explain, route_request
from app.core.errors import StageError
from app.core.remix import RemixResult, ShipmentContainer, remix_order
from app.core.scoring import score_orders
from app.core.splitter import analyze_orders
from app.data.models import (
    AvailabilityStatus,
    Customer,
    LineSummary,
    OrderAnalysis,
    OrderLine,
    ProductionFeedbackEntry,
)
from app.presentation.contacts import Contact, generate_synthetic_contact_directory, resolve_contact
from app.presentation.email_draft import render_proposal_email
from app.presentation.pivot import render_pivot_html
from app.presentation.pivot_export import render_pivot_workbook
from app.presentation.proposal_document import render_proposal_workbook

app = FastAPI(
    title="Supply Chain Remix Decision Support",
    description=(
        "Personal portfolio project reimplementing supply-planning decision "
        "logic (originally an Excel/VBA tool) as a Python service. All data "
        "is synthetic. Not affiliated with, and contains no code or data "
        "from, any employer.\n\n"
        "The business problem: customer orders ship direct to the customer "
        "as a single unit, so one delayed line blocks every other line on "
        "the same order even when they're physically ready. This service "
        "detects that trapped volume and proposes ways to release it early "
        "by combining it with other orders for the same customer.\n\n"
        "**This tool is deliberately staged, not end-to-end.** That came "
        "from the original users, not a technical preference, for two "
        "reasons: (1) trust — a tool that acts without showing its "
        "reasoning at each stage doesn't get used, and (2) failure "
        "visibility — a single end-to-end run makes it hard to tell which "
        "stage broke when something goes wrong; a checkpoint at every "
        "transition (data -> analysis -> scoring -> remix) makes that "
        "obvious instead. `/analyze` is one checkpoint; `/remix` is the "
        "next. **Neither this API nor any AI layer built on top of it "
        "should chain them together automatically.** A request that would "
        "span both checkpoints (e.g. \"analyze my orders and remix "
        "whatever needs it\") should return the /analyze output and stop "
        "there — the human picks which order to remix next, on purpose, "
        "every time.\n\n"
        "Every endpoint here produces a *review* output for a human "
        "planner — nothing is auto-executed."
    ),
    version="0.1.0",
)


@app.exception_handler(StageError)
async def stage_error_handler(request: Request, exc: StageError) -> JSONResponse:
    # CHECKPOINT failure handling (deliberate — see app/core/errors.py):
    # a stage that can't process its input says so specifically, here,
    # rather than surfacing as a generic unhandled 500. The stage name
    # and expected/received values are the whole point: they tell the
    # caller exactly where in the pipeline to look.
    return JSONResponse(
        status_code=422,
        content={
            "stage": exc.stage,
            "detail": str(exc),
            "expected": exc.expected,
            "received": exc.received,
        },
    )


# ---------------------------------------------------------------------------
# Conversion: API schemas -> internal domain dataclasses
# ---------------------------------------------------------------------------


def _to_production_feedback_entry(entry_in: ProductionFeedbackEntryIn) -> ProductionFeedbackEntry:
    return ProductionFeedbackEntry(
        available_qty=entry_in.available_qty,
        available_date=entry_in.available_date,
        status=AvailabilityStatus(entry_in.status),
    )


def _to_order_line(line_in: OrderLineIn) -> OrderLine:
    return OrderLine(
        order_number=line_in.order_number,
        order_line_id=line_in.order_line_id,
        customer_id=line_in.customer_id,
        customer_name=line_in.customer_name,
        sku=line_in.sku,
        product_category=line_in.product_category,
        abc_class=line_in.abc_class,
        seasonality_factor=line_in.seasonality_factor,
        supplier_country=line_in.supplier_country,
        bi_fs=line_in.bi_fs,
        plant=line_in.plant,
        unit_price=line_in.unit_price,
        quantity=line_in.quantity,
        cbm_per_unit=line_in.cbm_per_unit,
        order_date=line_in.order_date,
        requested_delivery_date=line_in.requested_delivery_date,
        production_feedback=[
            _to_production_feedback_entry(e) for e in line_in.production_feedback
        ],
    )


def _plant_breakdown(order: OrderAnalysis) -> list[PlantBreakdownEntry]:
    """Group an OrderAnalysis's line summaries by plant. Plant lives on
    the line (a product/master attribute), not the order, so one order
    can span several plants — this is what lets the pivot filter and
    re-aggregate by plant without a second API call."""
    by_plant: dict[str, list[LineSummary]] = {}
    for ls in order.line_summaries:
        by_plant.setdefault(ls.plant, []).append(ls)
    return [
        PlantBreakdownEntry(
            plant=plant,
            quantity=sum(ls.quantity for ls in lines),
            recoverable_quantity=sum(ls.recoverable_quantity for ls in lines),
            total_amount=math.fsum(ls.total_amount for ls in lines),
            recoverable_amount=math.fsum(ls.recoverable_amount for ls in lines),
        )
        for plant, lines in sorted(by_plant.items())
    ]


# ---------------------------------------------------------------------------
# Conversion: internal domain dataclasses -> API schemas
# ---------------------------------------------------------------------------


def _container_to_schema(c: ShipmentContainer) -> ShipmentContainerOut:
    return ShipmentContainerOut(
        reference=c.reference,
        category_group=c.group,
        index=c.index,
        status=c.status,
        availability_date=c.availability_date,
        utilization=c.utilization,
        total_cbm=c.cbm,
        total_quantity=c.total_quantity,
        ok_quantity=c.ok_quantity,
        net_amount=c.net_amount,
        lines=[
            ContainerLineOut(
                order_number=l.order_number,
                order_line_id=l.order_line_id,
                sku=l.sku,
                product_category=l.product_category,
                quantity=l.quantity,
                available_date=l.available_date,
                requested_delivery_date=l.requested_delivery_date,
                line_status="OK" if l.is_ok else "NOK",
                calendar_shifted_from=l.calendar_shifted_from,
                cbm=l.cbm,
                net_amount=l.net_amount,
            )
            for l in c.lines
        ],
    )


def _result_to_schema(result: RemixResult) -> RemixResponse:
    return RemixResponse(
        target_order_number=result.target_order_number,
        customer_id=result.customer_id,
        target_order_coefficient=result.target_order_coefficient,
        category_groups=result.category_groups,
        containers=[_container_to_schema(c) for c in result.containers],
        group_failures=[
            GroupFailureOut(
                category_group=f.group,
                reason=f.reason,
                message=f.message,
                total_cbm=f.total_cbm,
                containers_needed=f.containers_needed,
            )
            for f in result.group_failures
        ],
        no_proposal_reason=result.no_proposal_reason,
        message=result.message,
    )


def _build_analyze_response(request: AnalyzeRequest) -> AnalyzeResponse:
    """Shared logic behind both /analyze (JSON) and /pivot (HTML) — the
    two views must never disagree, so both call this single function
    rather than each recomputing the analysis independently.
    """
    order_lines = [_to_order_line(l) for l in request.order_lines]
    as_of_date = request.as_of_date or date.today()

    analyses = analyze_orders(order_lines)
    scored = score_orders(analyses, as_of_date=as_of_date)
    at_risk = [s for s in scored if s.order.mix_status == "NOK"]

    return AnalyzeResponse(
        as_of_date=as_of_date,
        at_risk_orders=[
            AtRiskOrderOut(
                order_number=s.order.order_number,
                customer_id=s.order.customer_id,
                customer_name=s.order.customer_name,
                requested_delivery_date=s.order.requested_delivery_date,
                availability_of_mix=s.order.availability_of_mix,
                mix_status=s.order.mix_status,
                total_quantity=s.order.total_quantity,
                recoverable_quantity=s.order.recoverable_quantity,
                total_amount=s.order.total_amount,
                recoverable_amount=s.order.recoverable_amount,
                plant_breakdown=_plant_breakdown(s.order),
                coefficient=s.coefficient,
            )
            for s in at_risk
        ],
    )


@app.post(
    "/analyze",
    response_model=AnalyzeResponse,
    responses={422: {"model": StageErrorResponse, "description": "A pipeline stage could not process the input — see stage/expected/received."}},
    summary="Rank at-risk (NOK) orders for human review",
    description=(
        "Splits every order line into quantity-level availability records, "
        "computes each order's mix-level bottleneck (availability_of_mix) "
        "and recoverable quantity, scores every order with the configured "
        "criticality weights, and returns the actionable (NOK) ones ranked "
        "by coefficient descending. This is a review output — it ranks and "
        "presents, it never filters based on the score or acts on an order.\n\n"
        "JSON for programmatic use; see /pivot for the same data as a "
        "browsable HTML table."
    ),
    tags=["analysis"],
)
def analyze(request: AnalyzeRequest) -> AnalyzeResponse:
    # CHECKPOINT (deliberate, not partial automation — see the API
    # description and app/core/errors.py): this endpoint is the first
    # of two checkpoints. It does not call /remix's logic itself, even
    # implicitly — the response is meant to be reviewed before a human
    # (or an AI layer acting on the human's explicit next instruction)
    # picks a specific order and requests /remix for it.
    return _build_analyze_response(request)


@app.post(
    "/pivot",
    response_class=HTMLResponse,
    responses={422: {"model": StageErrorResponse, "description": "A pipeline stage could not process the input — see stage/expected/received."}},
    summary="Human-facing pivot review table (HTML)",
    description=(
        "Renders the exact same ranked, actionable-orders data as /analyze, "
        "as a browsable HTML table for a planner instead of raw JSON — the "
        "presentation-layer view of the first checkpoint. Sortable columns, "
        "a search box, and a backorder/upcoming badge per order. This page "
        "does not call /remix or trigger anything automatically; it exists "
        "so a human can pick an order number to take to /remix next."
    ),
    tags=["presentation"],
)
def pivot(request: AnalyzeRequest) -> HTMLResponse:
    # CHECKPOINT (deliberate — see the API description and
    # app/core/errors.py): this is the presentation of the FIRST
    # checkpoint. It reuses _build_analyze_response rather than
    # recomputing anything, so this page and /analyze's JSON can never
    # silently disagree. Nothing here calls /remix.
    response_data = _build_analyze_response(request)
    return HTMLResponse(
        content=render_pivot_html(response_data, request_payload_json=request.model_dump_json())
    )


@app.post(
    "/pivot/export.xlsx",
    summary="Export the customer-level pivot to Excel",
    description=(
        "Form-encoded endpoint backing the pivot page's 'Export to Excel' button: "
        "`payload` carries the same JSON body /analyze and /pivot accept, and "
        "`plant` (optional) restricts the export to one plant's contribution, "
        "using the exact same aggregation as the HTML pivot — the two can never "
        "disagree, since both call app.presentation.customer_pivot.aggregate_by_customer. "
        "Not intended for direct JSON use; it exists for the HTML page's native "
        "form submission (no JS fetch/blob handling needed for the file download)."
    ),
    tags=["presentation"],
)
def pivot_export(payload: str = Form(...), plant: str | None = Form(None)) -> Response:
    try:
        request = AnalyzeRequest.model_validate_json(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=f"Malformed export payload: {exc}")

    response_data = _build_analyze_response(request)
    plant_filter = None if not plant or plant == "__all__" else plant
    workbook_bytes = render_pivot_workbook(response_data, plant=plant_filter)

    return Response(
        content=workbook_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=at_risk_customers.xlsx"},
    )


@app.post(
    "/remix",
    response_model=RemixResponse,
    responses={
        404: {"model": ErrorResponse, "description": "target_order_number not found in order_lines"},
        422: {"model": StageErrorResponse, "description": "A pipeline stage could not process the input — see stage/expected/received."},
    },
    summary="Generate a remix proposal for a planner-selected order",
    description=(
        "For a planner-selected order, takes EVERY unit (OK, late and UNPLANNED) of "
        "the same customer's NOK orders in the same (or linked) category group and "
        "distributes it into containers ordered by availability date, mirroring the "
        "original tool's potential-availability view. Each container shows its fill % and OK/NOK "
        "status; late/unplanned units stay in their containers so the customer sees "
        "them waiting, not cancelled. Every container is at least 94.5% full: if the "
        "pool's volume cannot satisfy 94.5% x container count, no containers are "
        "created ('insufficient_volume'). 'No proposal' is a legitimate, distinguished "
        "result — see no_proposal_reason.\n\n"
        "This endpoint is the SECOND checkpoint (see /analyze and the API "
        "description) — it must be called deliberately, for one specific "
        "order, never automatically chained after /analyze."
    ),
    tags=["remix"],
)
def remix(request: RemixRequest) -> RemixResponse:
    # CHECKPOINT (deliberate — see the API description and
    # app/core/errors.py): this is the second, separate checkpoint.
    # Calling this requires a specific target_order_number the human
    # (or an AI layer acting on their explicit instruction) chose —
    # this function is never invoked as a side effect of /analyze.
    order_lines = [_to_order_line(l) for l in request.order_lines]
    as_of_date = request.as_of_date or date.today()

    analyses = analyze_orders(order_lines)
    known_order_numbers = {a.order_number for a in analyses}
    if request.target_order_number not in known_order_numbers:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Order '{request.target_order_number}' was not found in the "
                "provided order_lines. Check the order number, or make sure "
                "the request includes that order's lines."
            ),
        )

    result = remix_order(request.target_order_number, analyses, as_of_date=as_of_date)
    return _result_to_schema(result)


def _run_remix_for_order(
    request: ProposalRequest,
) -> tuple[RemixResult, OrderAnalysis]:
    """Runs remix_order for the target and returns the result plus the
    target's analysis -- the document/email cover the whole container plan."""
    order_lines = [_to_order_line(l) for l in request.order_lines]
    as_of_date = request.as_of_date or date.today()

    analyses = analyze_orders(order_lines)
    analyses_by_number = {a.order_number: a for a in analyses}
    if request.target_order_number not in analyses_by_number:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Order '{request.target_order_number}' was not found in the "
                "provided order_lines."
            ),
        )
    result = remix_order(request.target_order_number, analyses, as_of_date=as_of_date)
    return result, analyses_by_number[request.target_order_number]


@app.post(
    "/remix/proposal.xlsx",
    responses={
        404: {"model": ErrorResponse, "description": "target_order_number not found"},
        422: {"model": StageErrorResponse, "description": "A pipeline stage could not process the input, or the current/proposed totals fail to reconcile."},
    },
    summary="Generate the customer-facing proposal document for an order (Excel)",
    description=(
        "The THIRD checkpoint's presentation layer: ONE document per order. Shows the "
        "CURRENT plan (the order as it stands today, one bottleneck date) and then every "
        "container the remix built, ordered by date, each with its fill %, OK/NOK status "
        "and contents by original order number. Late and UNPLANNED units appear inside "
        "their containers ('To be confirmed' when undated), and category groups that "
        "could not be containerised are listed with their reason. Every unit of the "
        "target order in a containerised group must appear exactly once, enforced by a "
        "reconciliation check that fails visibly (422) instead of producing a document "
        "that does not match the customer's own records."
    ),
    tags=["presentation"],
)
def remix_proposal_document(request: ProposalRequest) -> Response:
    result, target = _run_remix_for_order(request)
    workbook_bytes = render_proposal_workbook(result, target)
    return Response(
        content=workbook_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=proposal_{target.order_number}.xlsx"},
    )


@app.post(
    "/remix/proposal-email.eml",
    responses={
        404: {"model": ErrorResponse, "description": "target_order_number not found"},
        422: {"model": StageErrorResponse, "description": "A pipeline stage could not process the input — including no contact on file for this customer, or a reconciliation failure."},
    },
    summary="Draft the customer email for an order's proposal(s) (does not send)",
    description=(
        "The LAST checkpoint in the chain, and deliberately so (see app/core/errors.py): "
        "drafts ONE .eml per order — plain-prose body covering every proposed shipment "
        "for it, plus the proposal document attached — addressed to the contact on "
        "file for this customer. It never sends anything: there is no SMTP call "
        "anywhere in this codebase. The person opens the returned .eml in their mail "
        "client, reviews it, and sends it themselves. If no contact is on file for "
        "this customer, this fails visibly (422) rather than drafting an email to nobody."
    ),
    tags=["presentation"],
)
def remix_proposal_email(request: ProposalRequest) -> Response:
    result, target = _run_remix_for_order(request)
    contact = resolve_customer_contact(request.order_lines, result.customer_id)

    workbook_bytes = render_proposal_workbook(result, target)
    attachment_filename = f"proposal_{target.order_number}.xlsx"
    eml_bytes = render_proposal_email(
        result, target, contact, workbook_bytes, attachment_filename
    )

    return Response(
        content=eml_bytes,
        media_type="message/rfc822",
        headers={
            "Content-Disposition": f"attachment; filename=proposal_{target.order_number}.eml"
        },
    )


@app.post(
    "/assistant",
    response_model=AssistantResponse,
    responses={422: {"model": StageErrorResponse, "description": "Routing needs an order number, or a stage could not process the input."}},
    summary="Natural-language entry point (routes and explains; never chains stages)",
    description=(
        "Turns a sentence into ONE stage call (analyze or remix) and explains the engine's own "
        "output. If the request spans several checkpoints, only the first runs and the rest are "
        "returned in deferred_stages for a human to trigger. Works without an API key (rule-based "
        "routing, templated explanation); with ANTHROPIC_API_KEY the model routes and phrases, "
        "but any number it states that the engine did not produce is rejected."
    ),
    tags=["ai"],
)
def assistant(request: AssistantRequest) -> AssistantResponse:
    llm = default_llm()
    intent = route_request(request.message, llm)
    if intent.stage == "remix" and intent.order_number is None:
        raise StageError(
            stage="AI layer -- routing",
            message="Remix needs a specific order.",
            expected="an order number such as SO-100203 in the request",
            received=repr(request.message),
        )
    base = {"stage_run": intent.stage, "routed_by": intent.source, "deferred_stages": intent.deferred_stages}
    # Exactly one engine call below; the response only lists deferred stages.
    if intent.stage == "analyze":
        res = _build_analyze_response(AnalyzeRequest(order_lines=request.order_lines, as_of_date=request.as_of_date))
        top = res.at_risk_orders[:3]
        template = f"{len(res.at_risk_orders)} orders are at risk; the most critical are " + ", ".join(
            f"{o.order_number} (coefficient {o.coefficient:.1f})" for o in top) + "." if top else "No orders are at risk."
        facts = {"at_risk_count": len(res.at_risk_orders), "top": [o.model_dump(mode="json") for o in top]}
        return AssistantResponse(**base, result=res, explanation=explain(facts, template, llm),
                                 next_step="Pick one order and ask to remix it.")
    res = remix(RemixRequest(order_lines=request.order_lines, target_order_number=intent.order_number, as_of_date=request.as_of_date))
    explanation, _ = remix_explanation(res, llm)
    return AssistantResponse(**base, result=res, explanation=explanation,
                             next_step="Review, then request the customer document/email deliberately.")


def remix_explanation(res: RemixResponse, llm: LLM | None) -> tuple[str, str]:
    """Plain-language explanation of a remix result, shared by /assistant and
    the dashboard. Returns (text, source) where source is "template" or "model".
    The model may only restate engine numbers (see app.ai.assistant.explain)."""
    if res.containers:
        template = f"Order {res.target_order_number} splits into {len(res.containers)} containers: " + "; ".join(
            f"#{c.index} {c.status} {c.utilization:.1%} full" for c in res.containers) + "."
    else:
        template = res.message or "No proposal."
    facts = {"containers": [c.model_dump(mode="json", exclude={"lines"}) for c in res.containers],
             "no_proposal_reason": res.no_proposal_reason, "message": res.message}
    text = explain(facts, template, llm)
    return text, ("template" if text == template else "model")


def resolve_customer_contact(order_lines: list[OrderLineIn], customer_id: str) -> Contact:
    """Synthetic contact for a customer, built from the request's own order lines
    (the API carries no full Customer record). Fails visibly if none is on file."""
    customers_by_id: dict[str, Customer] = {}
    for line_in in order_lines:
        customers_by_id.setdefault(
            line_in.customer_id,
            Customer(
                customer_id=line_in.customer_id,
                customer_name=line_in.customer_name,
                region="",
                preferred_categories=(),
            ),
        )
    directory = generate_synthetic_contact_directory(list(customers_by_id.values()))
    return resolve_contact(directory, customer_id)


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"docs": "/docs"}


# Dashboard routes live in their own module; included last so it can reuse the helpers above.
from app.api.dashboard import router as dashboard_router  # noqa: E402

app.include_router(dashboard_router)
