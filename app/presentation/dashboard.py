"""Review dashboard: ONE HTML template, rendered live (GET /dashboard) or static (docs/dashboard/).

The template (dashboard_template.html) contains no figures at all. Everything it shows comes
from the injected data:

* ``config`` -- the floor, capacity, as-of default and links, read from app/config/;
* ``snapshot`` (static mode only) -- the engine's own output for the ranked list and a few
  plans, computed by scripts/build_static_dashboard.py.

In live mode the page calls the API one stage at a time, each on a deliberate click:
POST /analyze (step 1) -> POST /dashboard/plan for ONE order (step 2) -> POST
/dashboard/email-preview (a text preview of the draft; nothing can be sent).
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Literal

from app.config.dashboard_config import DashboardConfig
from app.config.generator_config import GeneratorConfig
from app.config.remix_config import RemixConfig
from app.core.remix import RemixResult
from app.presentation.proposal_document import failure_categories

TEMPLATE_PATH = pathlib.Path(__file__).with_name("dashboard_template.html")
DATA_PLACEHOLDER = "/*__DASHBOARD_DATA__*/null"

# Plain-language headline per no-proposal reason. No numbers here: the per-group
# details underneath are the engine's own messages.
NO_PROPOSAL_HEADLINE: dict[str, str] = {
    "no_candidates": "No proposal: there is nothing to remix for this order.",
    "insufficient_volume": "No proposal: not enough volume to fill the containers to the minimum load.",
    "cannot_balance": "No proposal: there is enough volume, but no arrangement keeps every container "
    "within the allowed fill range without taking units from a fully-OK container.",
    "mixed": "No proposal: the order's category groups failed for different reasons.",
}


def not_split_notes(result: RemixResult) -> list[dict[str, str]]:
    """Which categories were not split, and the engine's reason, per failed group."""
    return [
        {
            "categories": ", ".join(failure_categories(result, f)),
            "reason": f.reason,
            "detail": f.message,
        }
        for f in result.group_failures
    ]


def page_config(mode: Literal["live", "static"], config: DashboardConfig | None = None) -> dict[str, Any]:
    cfg = config or DashboardConfig()
    remix = RemixConfig()
    return {
        "mode": mode,
        "floor": remix.min_acceptable_utilization,
        "capacity_cbm": remix.container_capacity_cbm,
        "as_of_default": GeneratorConfig().simulated_today.isoformat(),
        "workflow_url": cfg.workflow_url,
        "repo_url": cfg.repo_url,
    }


def _safe_json(data: Any) -> str:
    """JSON that can sit inside a <script> element without ending it early."""
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def render_dashboard_html(
    mode: Literal["live", "static"],
    snapshot: dict[str, Any] | None = None,
    config: DashboardConfig | None = None,
) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    if template.count(DATA_PLACEHOLDER) != 1:
        raise RuntimeError("dashboard template must contain the data placeholder exactly once")
    data = {"config": page_config(mode, config), "snapshot": snapshot}
    return template.replace(DATA_PLACEHOLDER, _safe_json(data))
