"""API contract: staged checkpoints, visible StageErrors, assistant runs ONE stage, drafts are never sent."""

from __future__ import annotations

import ast
import email
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from app.ai import assistant as assistant_module
from app.api.main import app

ROOT = pathlib.Path(__file__).resolve().parents[1]
SAMPLE = json.loads((ROOT / "data" / "sample_order_book.json").read_text())
AS_OF = "2026-04-01"


@pytest.fixture(autouse=True)
def no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tool must work without a model; tests never call one."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(scope="module")
def ranked(client: TestClient) -> list[dict]:
    r = client.post("/analyze", json={**SAMPLE, "as_of_date": AS_OF})
    assert r.status_code == 200
    return r.json()["at_risk_orders"]


def _containerisable(client: TestClient, ranked: list[dict]) -> str:
    for o in ranked:
        r = client.post("/remix", json={**SAMPLE, "target_order_number": o["order_number"], "as_of_date": AS_OF})
        if r.json()["containers"]:
            return o["order_number"]
    raise AssertionError("sample data has no containerisable order")


def test_analyze_returns_only_nok_orders_ranked(ranked: list[dict]) -> None:
    assert len(ranked) == 61
    assert {o["mix_status"] for o in ranked} == {"NOK"}
    coefs = [o["coefficient"] for o in ranked]
    assert coefs == sorted(coefs, reverse=True)


def test_sample_data_outcome_is_23_built_33_volume_5_mixed(client: TestClient, ranked: list[dict]) -> None:
    outcomes = {"built": 0, "insufficient_volume": 0, "cannot_balance": 0, "mixed": 0}
    for o in ranked:
        body = client.post("/remix", json={**SAMPLE, "target_order_number": o["order_number"], "as_of_date": AS_OF}).json()
        outcomes["built" if body["containers"] else body["no_proposal_reason"]] += 1
    # All 5 non-volume failures are "mixed": each order also has a group that fails on volume.
    assert outcomes == {"built": 23, "insufficient_volume": 33, "cannot_balance": 0, "mixed": 5}


def test_analyze_does_not_run_remix(client: TestClient) -> None:
    body = client.post("/analyze", json={**SAMPLE, "as_of_date": AS_OF}).json()
    assert set(body) == {"as_of_date", "at_risk_orders"}


def test_stage_error_is_422_with_stage_expected_received(client: TestClient) -> None:
    line = dict(SAMPLE["order_lines"][0])
    line["quantity"] = line["quantity"] + 1  # no longer matches its production feedback
    r = client.post("/analyze", json={"order_lines": [line]})
    assert r.status_code == 422
    body = r.json()
    assert body["stage"].startswith("Step 2")
    assert body["expected"] and body["received"]


def test_remix_unknown_order_is_404(client: TestClient) -> None:
    r = client.post("/remix", json={**SAMPLE, "target_order_number": "SO-NOPE"})
    assert r.status_code == 404


def test_assistant_analyze_without_key_runs_one_stage_and_defers_the_rest(client: TestClient) -> None:
    r = client.post("/assistant", json={**SAMPLE, "as_of_date": AS_OF,
                                        "message": "rank the orders, remix the top one and email the customer"})
    assert r.status_code == 200
    body = r.json()
    assert body["stage_run"] == "analyze" and body["routed_by"] == "rules"
    assert body["deferred_stages"] == ["remix", "email"]
    assert "containers" not in body["result"]  # remix did not run
    assert "61 orders are at risk" in body["explanation"]


def test_assistant_remix_without_key(client: TestClient, ranked: list[dict]) -> None:
    order = _containerisable(client, ranked)
    body = client.post("/assistant", json={**SAMPLE, "as_of_date": AS_OF, "message": f"remix {order}"}).json()
    assert body["stage_run"] == "remix"
    assert body["result"]["target_order_number"] == order
    assert body["result"]["containers"]


def test_assistant_remix_without_order_number_fails_visibly(client: TestClient) -> None:
    r = client.post("/assistant", json={**SAMPLE, "message": "build containers please"})
    assert r.status_code == 422
    assert r.json()["stage"] == "AI layer -- routing"


def test_assistant_rejects_model_number_the_engine_did_not_produce(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Inventive:
        def complete(self, system: str, user: str) -> str:
            if "route" in system:
                return json.dumps({"stages": ["analyze"], "order_number": None})
            return "Remixing would save 37.5% of planner time."

    monkeypatch.setattr("app.api.main.default_llm", lambda: Inventive())
    body = client.post("/assistant", json={**SAMPLE, "as_of_date": AS_OF, "message": "what is at risk?"}).json()
    assert body["routed_by"] == "llm"
    assert "37.5" not in body["explanation"]
    assert body["explanation"].startswith("61 orders are at risk")


def test_email_is_a_draft_with_the_document_attached(client: TestClient, ranked: list[dict]) -> None:
    order = _containerisable(client, ranked)
    r = client.post("/remix/proposal-email.eml", json={**SAMPLE, "target_order_number": order, "as_of_date": AS_OF})
    if r.status_code == 422:  # this customer has no synthetic contact on file: also a visible failure
        assert "contact" in r.json()["stage"]
        return
    msg = email.message_from_bytes(r.content)
    assert msg["X-Draft-Only"] == "true"
    assert any(p.get_filename() == f"proposal_{order}.xlsx" for p in msg.walk())


def test_no_send_capability_anywhere_in_the_code() -> None:
    """No module imports a mail-sending library or calls a send method (docstrings may mention them)."""
    forbidden_modules = {"smtplib", "imaplib", "poplib"}
    forbidden_calls = {"sendmail", "send_message", "starttls"}
    for path in (ROOT / "app").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert not {a.name.split(".")[0] for a in node.names} & forbidden_modules, path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden_modules, path
            if isinstance(node, ast.Attribute):
                assert node.attr not in forbidden_calls, path


def test_assistant_never_routes_straight_to_presentation() -> None:
    i = assistant_module.route_request("email the customer about SO-1 and build the excel")
    assert i.stage == "remix"
    assert "remix" not in i.deferred_stages
