"""Review dashboard routes: the page, the bundled sample data, step 2 (plan) and step 3 (draft preview).

Step 1 uses the existing POST /analyze. Each route serves ONE stage and is called by the page only
after a deliberate click. Nothing here can send anything.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from app.ai.assistant import LLM, default_llm
from app.api.schemas import (
    DashboardPlanResponse,
    DashboardTarget,
    EmailPreviewResponse,
    NotSplitNote,
    ProposalRequest,
    StageErrorResponse,
)
from app.presentation.dashboard import NO_PROPOSAL_HEADLINE, not_split_notes, render_dashboard_html
from app.presentation.email_draft import render_proposal_email_preview

ROOT = pathlib.Path(__file__).resolve().parents[2]
SAMPLE_ORDER_BOOK = ROOT / "data" / "sample_order_book.json"

router = APIRouter(tags=["dashboard"])
_STAGE_ERRORS: dict[int | str, dict[str, Any]] = {
    422: {"model": StageErrorResponse, "description": "A stage could not process the input."}
}


def build_plan_view(request: ProposalRequest, llm: LLM | None) -> DashboardPlanResponse:
    """Engine result for ONE order + the assistant layer's explanation. Shared by the live
    route and the static build, so both show exactly the same thing."""
    from app.api.main import _result_to_schema, _run_remix_for_order, remix_explanation

    result, target = _run_remix_for_order(request)
    res = _result_to_schema(result)
    text, source = remix_explanation(res, llm)
    return DashboardPlanResponse(
        target=DashboardTarget(
            order_number=target.order_number,
            customer_name=target.customer_name,
            requested_delivery_date=target.requested_delivery_date,
            availability_of_mix=target.availability_of_mix,
        ),
        remix=res,
        explanation=text,
        explanation_source=source,  # type: ignore[arg-type]
        no_proposal_headline=None if res.containers else NO_PROPOSAL_HEADLINE.get(res.no_proposal_reason or ""),
        not_split=[NotSplitNote(**n) for n in not_split_notes(result)],
    )


def build_email_preview(request: ProposalRequest) -> EmailPreviewResponse:
    from app.api.main import _run_remix_for_order, resolve_customer_contact

    result, target = _run_remix_for_order(request)
    contact = resolve_customer_contact(request.order_lines, result.customer_id)
    return EmailPreviewResponse(**render_proposal_email_preview(result, target, contact))


@router.get("/dashboard", response_class=HTMLResponse, summary="Review dashboard (staged, read-only)")
def dashboard_page() -> HTMLResponse:
    return HTMLResponse(render_dashboard_html("live"))


@router.get("/dashboard/sample-order-book", summary="The bundled synthetic order book (for the dashboard)")
def sample_order_book() -> JSONResponse:
    return JSONResponse(json.loads(SAMPLE_ORDER_BOOK.read_text(encoding="utf-8")))


@router.post("/dashboard/plan", response_model=DashboardPlanResponse, responses=_STAGE_ERRORS,
             summary="Step 2: container plan for ONE order, with its explanation")
def dashboard_plan(request: ProposalRequest) -> DashboardPlanResponse:
    return build_plan_view(request, default_llm())


@router.post("/dashboard/email-preview", response_model=EmailPreviewResponse, responses=_STAGE_ERRORS,
             summary="Step 3: text preview of the DRAFT customer email (never sent)")
def dashboard_email_preview(request: ProposalRequest) -> EmailPreviewResponse:
    return build_email_preview(request)
