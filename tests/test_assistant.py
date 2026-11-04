"""AI layer: routes to ONE stage, never chains, rejects invented numbers."""

from __future__ import annotations

import json

from app.ai.assistant import explain, route_request


class Fake:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    def complete(self, system: str, user: str) -> str:
        return self.reply


def test_rules_route_ranking_and_remix() -> None:
    assert route_request("which orders are most at risk?").stage == "analyze"
    i = route_request("remix SO-100203")
    assert (i.stage, i.order_number) == ("remix", "SO-100203")


def test_multi_stage_request_runs_only_the_first_stage() -> None:
    i = route_request("rank the orders, remix SO-100203 and email the customer")
    assert i.stage == "analyze"
    assert i.deferred_stages == ["remix", "email"]


def test_presentation_stages_are_never_auto_run() -> None:
    i = route_request("email the customer about SO-100203")
    assert i.stage == "remix" and i.deferred_stages == []


def test_llm_routing_used_and_bad_output_falls_back() -> None:
    good = Fake(json.dumps({"stages": ["remix"], "order_number": "SO-1"}))
    assert route_request("x", good).source == "llm"
    assert route_request("remix SO-5", Fake("not json")).source == "rules"


def test_explanation_with_invented_number_is_rejected() -> None:
    facts = {"containers": 3}
    assert explain(facts, "TEMPLATE 3", Fake("There are 3 containers.")) == "There are 3 containers."
    assert explain(facts, "TEMPLATE 3", Fake("There are 7 containers.")) == "TEMPLATE 3"


def test_no_llm_uses_template() -> None:
    assert explain({}, "TEMPLATE", None) == "TEMPLATE"


def test_llm_cannot_invent_the_order_number() -> None:
    """The model may choose the stage, but the order must come from the user's own text."""
    invents = Fake(json.dumps({"stages": ["remix"], "order_number": "SO-999999"}))
    i = route_request("remix SO-100203", invents)
    assert i.order_number == "SO-100203"
    i = route_request("remix the worst one", invents)
    assert i.order_number is None  # the API then fails visibly instead of remixing an invented order


def test_llm_non_string_order_number_does_not_crash() -> None:
    weird = Fake(json.dumps({"stages": ["remix"], "order_number": 12345}))
    i = route_request("remix SO-7", weird)
    assert (i.stage, i.order_number, i.source) == ("remix", "SO-7", "llm")


def test_stage_that_runs_is_never_also_deferred() -> None:
    i = route_request("draft an email, then remix SO-100203 and email again")
    assert i.stage == "remix"
    assert "remix" not in i.deferred_stages
    assert i.deferred_stages == sorted(set(i.deferred_stages), key=i.deferred_stages.index)
