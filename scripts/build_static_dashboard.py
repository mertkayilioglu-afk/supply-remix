"""Render the review dashboard as one static page (docs/dashboard/index.html) for GitHub Pages.

    python scripts/build_static_dashboard.py [--out docs/dashboard/index.html]

Same template as the live GET /dashboard; the difference is only the data. Here the engine runs
once, now, on the bundled sample order book, and its output is embedded as a snapshot:

* the ranked at-risk orders (exactly what POST /analyze returns);
* the plan for the top N orders, plus the highest-ranked order of every other outcome
  (full plan, partial plan, insufficient_volume, mixed, ...), see DashboardConfig;
* the draft email preview for each of those orders (or the visible error, e.g. no contact).

No AI model is used for the static build (template explanations only), so the output is
deterministic and can be checked by a test.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.api.dashboard import build_email_preview, build_plan_view  # noqa: E402
from app.api.main import _build_analyze_response  # noqa: E402
from app.api.schemas import AnalyzeRequest, DashboardPlanResponse, ProposalRequest  # noqa: E402
from app.config.dashboard_config import DashboardConfig  # noqa: E402
from app.config.generator_config import GeneratorConfig  # noqa: E402
from app.core.errors import StageError  # noqa: E402
from app.presentation.dashboard import render_dashboard_html  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "docs" / "dashboard" / "index.html"


def outcome(view: DashboardPlanResponse) -> str:
    r = view.remix
    if r.containers:
        return "partial plan" if r.group_failures else "full plan"
    return r.no_proposal_reason or "unknown"


def build_snapshot(config: DashboardConfig | None = None) -> dict[str, Any]:
    cfg = config or DashboardConfig()
    book = json.loads((ROOT / "data" / "sample_order_book.json").read_text(encoding="utf-8"))
    as_of = GeneratorConfig().simulated_today
    request = AnalyzeRequest(order_lines=book["order_lines"], as_of_date=as_of)
    ranked = _build_analyze_response(request)

    def plan(order: str) -> DashboardPlanResponse:
        return build_plan_view(
            ProposalRequest(order_lines=request.order_lines, target_order_number=order, as_of_date=as_of), llm=None
        )

    order_numbers = [o.order_number for o in ranked.at_risk_orders]
    plans: dict[str, DashboardPlanResponse] = {o: plan(o) for o in order_numbers[: cfg.static_top_n]}
    if cfg.static_include_each_outcome:
        seen = {outcome(v) for v in plans.values()}
        for o in order_numbers[cfg.static_top_n:]:
            view = plan(o)
            if outcome(view) not in seen:
                plans[o] = view
                seen.add(outcome(view))

    emails: dict[str, Any] = {}
    for o, view in plans.items():
        if not view.remix.containers:
            continue  # the page only offers a draft when there is a plan
        try:
            emails[o] = build_email_preview(
                ProposalRequest(order_lines=request.order_lines, target_order_number=o, as_of_date=as_of)
            ).model_dump(mode="json")
        except StageError as exc:  # e.g. no contact on file: shown as the visible error it is
            emails[o] = {"error": {"stage": exc.stage, "detail": str(exc)}}
        except HTTPException as exc:
            emails[o] = {"error": {"stage": None, "detail": str(exc.detail)}}

    return {
        "as_of": as_of.isoformat(),
        "ranked": ranked.model_dump(mode="json"),
        "plans": {o: v.model_dump(mode="json") for o, v in plans.items()},
        "outcomes": {o: outcome(v) for o, v in plans.items()},
        "emails": emails,
    }


def build(out: pathlib.Path = DEFAULT_OUT, config: DashboardConfig | None = None) -> pathlib.Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_dashboard_html("static", build_snapshot(config), config), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT)
    path = build(parser.parse_args().out)
    print(f"wrote {path} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
