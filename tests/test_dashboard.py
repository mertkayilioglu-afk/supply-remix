"""Review dashboard: same engine numbers live and static, staged, and no way to send anything."""

from __future__ import annotations

import json
import pathlib
import re
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.main import _to_order_line, app
from app.api.schemas import AnalyzeRequest
from app.config.dashboard_config import DashboardConfig
from app.config.generator_config import GeneratorConfig
from app.config.remix_config import RemixConfig
from app.core.remix import remix_order
from app.core.splitter import analyze_orders
from app.presentation.dashboard import TEMPLATE_PATH, _safe_json, render_dashboard_html
from scripts.build_static_dashboard import DEFAULT_OUT, build

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "data" / "sample_order_book.json").read_text())
AS_OF: date = GeneratorConfig().simulated_today
DATA_RE = re.compile(r'<script id="dashboard-data" type="application/json">(.*?)</script>', re.S)


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="module")
def static_html(tmp_path_factory: pytest.TempPathFactory) -> str:
    return build(tmp_path_factory.mktemp("dash") / "index.html").read_text(encoding="utf-8")


def _data(html: str) -> dict:
    return json.loads(DATA_RE.search(html).group(1))


def _engine_analyses() -> list:
    return analyze_orders([_to_order_line(l) for l in AnalyzeRequest.model_validate(SAMPLE).order_lines])


# ---------------------------------------------------------------- static build


def test_static_build_contains_engine_ranking_and_container_counts(static_html: str) -> None:
    snap = _data(static_html)["snapshot"]
    analyses = _engine_analyses()
    nok = {a.order_number for a in analyses if a.mix_status == "NOK"}
    ranked = [o["order_number"] for o in snap["ranked"]["at_risk_orders"]]
    assert set(ranked) == nok and len(ranked) == 61
    top = ranked[: DashboardConfig().static_top_n]
    assert set(top) <= set(snap["plans"])
    for order, view in snap["plans"].items():
        assert order in static_html
        engine = remix_order(order, analyses, RemixConfig(), as_of_date=AS_OF)
        assert len(view["remix"]["containers"]) == len(engine.containers), order
        assert [c["utilization"] for c in view["remix"]["containers"]] == [c.utilization for c in engine.containers]
        assert view["remix"]["no_proposal_reason"] == engine.no_proposal_reason


def test_static_snapshot_shows_every_outcome_in_the_sample(static_html: str) -> None:
    outcomes = set(_data(static_html)["snapshot"]["outcomes"].values())
    assert {"full plan", "partial plan", "insufficient_volume", "mixed"} <= outcomes


def test_static_partial_plan_names_the_category_not_split(static_html: str) -> None:
    snap = _data(static_html)["snapshot"]
    partial = [o for o, k in snap["outcomes"].items() if k == "partial plan"]
    view = snap["plans"][partial[0]]
    assert view["remix"]["containers"] and view["not_split"]
    assert all(n["categories"] and n["detail"] for n in view["not_split"])


def test_committed_static_page_is_up_to_date(static_html: str) -> None:
    """docs/dashboard/index.html must be exactly what the engine produces now."""
    assert DEFAULT_OUT.read_text(encoding="utf-8") == static_html, "run: python scripts/build_static_dashboard.py"


def test_static_page_has_banner_and_workflow_link(static_html: str) -> None:
    assert "Synthetic data. Illustrative parameters." in static_html
    cfg = _data(static_html)["config"]
    assert cfg["mode"] == "static" and cfg["workflow_url"].endswith("docs/AI_WORKFLOW.md")


# ---------------------------------------------------------------- page safety


def test_page_has_no_send_or_submit_to_customer_action() -> None:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    assert "<form" not in template.lower()
    assert "mailto:" not in template.lower()
    assert "proposal-email.eml" not in template  # the .eml endpoint is never called from the page
    button_texts = re.findall(r'el\("button",[^)]*text:\s*"([^"]+)"', template)
    assert button_texts and not [t for t in button_texts if re.search(r"\bsend\b|submit", t, re.I)]
    # the page can only reach these endpoints, one per deliberate click
    assert set(re.findall(r'post\("([^"]+)"', template)) == {"analyze", "dashboard/plan", "dashboard/email-preview"}
    assert re.findall(r'fetch\("([^"]+)"', template) == ["dashboard/sample-order-book"]


def test_template_contains_no_hard_coded_figures() -> None:
    """Floor, capacity and dates come from config/engine data, never from the page."""
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    script = template[template.index("<script>"):]
    for figure in ("94.5", "0.945", "30 CBM", "2026", "SO-1"):
        assert figure not in script, figure


def test_embedded_data_cannot_close_the_script_element() -> None:
    hostile = {"x": "</script><script>alert(1)</script>"}
    assert "</script>" not in _safe_json(hostile)
    assert json.loads(_safe_json(hostile)) == hostile
    html = render_dashboard_html("static", {"note": "</script>"})
    assert html.count("</script>") == TEMPLATE_PATH.read_text().count("</script>")


# ---------------------------------------------------------------- live routes


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_live_page_is_served_without_any_data(client: TestClient) -> None:
    r = client.get("/dashboard")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    data = _data(r.text)
    assert data["config"]["mode"] == "live" and data["snapshot"] is None  # nothing pre-computed or auto-run
    assert data["config"]["floor"] == RemixConfig().min_acceptable_utilization


def test_live_plan_matches_remix_and_assistant(client: TestClient, static_html: str) -> None:
    order = "SO-100275"
    body = {**SAMPLE, "target_order_number": order, "as_of_date": AS_OF.isoformat()}
    plan = client.post("/dashboard/plan", json=body).json()
    remix = client.post("/remix", json=body).json()
    assistant = client.post("/assistant", json={**SAMPLE, "as_of_date": AS_OF.isoformat(), "message": f"remix {order}"}).json()
    assert plan["remix"] == remix
    assert plan["explanation"] == assistant["explanation"] and plan["explanation_source"] == "template"
    assert plan == _data(static_html)["snapshot"]["plans"][order]  # live and static show the same thing


def test_live_no_proposal_has_plain_language_headline(client: TestClient) -> None:
    body = {**SAMPLE, "target_order_number": "SO-100279", "as_of_date": AS_OF.isoformat()}
    plan = client.post("/dashboard/plan", json=body).json()
    assert plan["remix"]["containers"] == [] and plan["remix"]["no_proposal_reason"] == "mixed"
    assert plan["no_proposal_headline"].startswith("No proposal:")
    assert len(plan["not_split"]) == len(plan["remix"]["group_failures"])


def test_email_preview_is_a_draft_only(client: TestClient) -> None:
    body = {**SAMPLE, "target_order_number": "SO-100275", "as_of_date": AS_OF.isoformat()}
    r = client.post("/dashboard/email-preview", json=body)
    assert r.status_code == 200
    d = r.json()
    assert d["draft_only"] is True and "SO-100275" in d["subject"] and d["body"].startswith("Dear ")


def test_unknown_order_fails_visibly(client: TestClient) -> None:
    r = client.post("/dashboard/plan", json={**SAMPLE, "target_order_number": "SO-NOPE"})
    assert r.status_code == 404
