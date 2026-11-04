"""Static checks on the n8n design example. These do NOT prove it runs in n8n (it is labelled untested);
they prove its structure keeps the project's rules: staged, human-gated, draft-only."""

from __future__ import annotations

import json
import pathlib
from collections import deque

ROOT = pathlib.Path(__file__).resolve().parents[1]
WF = json.loads((ROOT / "docs" / "n8n" / "supply_remix_staged_review.json").read_text())
NODES = {n["name"]: n for n in WF["nodes"]}
EDGES = {
    src: [t["node"] for out in spec["main"] for t in out]
    for src, spec in WF["connections"].items()
}


def _upstream(name: str) -> set[str]:
    parents: dict[str, set[str]] = {}
    for src, targets in EDGES.items():
        for t in targets:
            parents.setdefault(t, set()).add(src)
    seen: set[str] = set()
    todo = deque([name])
    while todo:
        for p in parents.get(todo.popleft(), set()):
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return seen


def _calls(path_suffix: str) -> list[str]:
    return [
        n["name"] for n in WF["nodes"]
        if n["type"] == "n8n-nodes-base.httpRequest" and n["parameters"]["url"].endswith(path_suffix)
    ]


def test_labelled_untested_and_inactive() -> None:
    assert "UNTESTED" in WF["name"]
    assert WF["active"] is False
    assert "UNTESTED DESIGN EXAMPLE" in NODES
    assert "Untested design example" in (ROOT / "docs" / "n8n" / "README.md").read_text()


def test_every_connection_points_at_a_real_node() -> None:
    for src, targets in EDGES.items():
        assert src in NODES
        assert all(t in NODES for t in targets)


def test_each_endpoint_is_called_once_in_stage_order() -> None:
    analyze, remix, email = _calls("/analyze"), _calls("/remix"), _calls("/remix/proposal-email.eml")
    assert len(analyze) == len(remix) == len(email) == 1
    assert analyze[0] in _upstream(remix[0]) and remix[0] in _upstream(email[0])


def test_every_call_after_analyze_is_behind_a_human_approval() -> None:
    waits = {n["name"] for n in WF["nodes"] if n["type"] == "n8n-nodes-base.wait"}
    remix, email = _calls("/remix")[0], _calls("/remix/proposal-email.eml")[0]
    assert waits & _upstream(remix)
    # the email needs a SECOND approval that comes after /remix
    assert any(w in _upstream(email) and remix in _upstream(w) for w in waits)


def test_nothing_can_send_and_assistant_is_not_used() -> None:
    sending_types = {"n8n-nodes-base.emailSend", "n8n-nodes-base.sendGrid", "n8n-nodes-base.mailgun",
                     "n8n-nodes-base.microsoftOutlook", "n8n-nodes-base.gmail"}
    for n in WF["nodes"]:
        assert n["type"] not in sending_types, n["name"]
        assert "send" != str(n["parameters"].get("operation", "")).lower(), n["name"]
    assert not _calls("/assistant")
    draft = NODES["Save DRAFT .eml (never sent)"]
    assert draft["type"] == "n8n-nodes-base.readWriteFile" and draft["parameters"]["operation"] == "write"
