"""AI layer -- a natural-language front door that ROUTES and EXPLAINS.

It never decides and never chains. The deterministic engine (splitter,
scoring, remix) makes every number; the model only (1) turns a sentence into
one stage call and (2) explains the engine's own output in plain words.

Rules (see app/core/errors.py for why the pipeline is staged):
  * One request runs ONE stage. If the sentence spans checkpoints ("rank the
    orders, remix the top one and email the customer") only the first stage runs;
    the rest is returned as ``deferred_stages`` for the human to trigger.
  * The model only sees numbers the engine computed, and its explanation is
    rejected -- replaced by a deterministic template -- if it contains any
    number not present in the engine output.
  * No API key / model failure never blocks the tool: routing falls back to
    rules and explanation to the template.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

KNOWN_STAGES = ("analyze", "remix", "document", "email")
ORDER_RE = re.compile(r"\bSO-\d+\b", re.IGNORECASE)
NUM_RE = re.compile(r"\d[\d,]*\.?\d*")


class LLM(Protocol):
    def complete(self, system: str, user: str) -> str: ...


class AnthropicLLM:
    """Thin wrapper around the Anthropic Messages API (needs ANTHROPIC_API_KEY)."""

    def __init__(self, model: str | None = None) -> None:
        import anthropic  # imported lazily so the rest of the tool needs no SDK

        self._client = anthropic.Anthropic()
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")

    def complete(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self._model, max_tokens=600, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


def default_llm() -> LLM | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return AnthropicLLM()
    except Exception:
        return None


@dataclass(frozen=True)
class Intent:
    stage: str  # analyze | remix
    order_number: str | None = None
    deferred_stages: list[str] = field(default_factory=list)
    source: str = "rules"  # rules | llm


_ROUTER_SYSTEM = (
    "You route a supply planner's request to ONE stage of a staged tool. "
    "Stages in order: analyze (rank at-risk orders), remix (propose date-ordered containers "
    "for ONE named order), document (customer xlsx), email (draft). "
    'Reply with JSON only: {"stages": [<every stage the request mentions, in order>], '
    '"order_number": <SO-number or null>}.'
)


def _rules_stages(text: str) -> list[str]:
    t = text.lower()
    found: list[tuple[int, str]] = []
    for stage, words in {
        "analyze": ("rank", "at risk", "at-risk", "critical", "which order", "worst", "priorit"),
        "remix": ("remix", "container", "split", "propos", "rescue"),
        "document": ("document", "excel", "xlsx", "spreadsheet"),
        "email": ("email", "mail", "draft"),
    }.items():
        pos = [t.find(w) for w in words if w in t]
        if pos:
            found.append((min(pos), stage))
    return [s for _, s in sorted(found)]


def route_request(text: str, llm: LLM | None = None) -> Intent:
    """Map a sentence to ONE stage.

    The order number always comes from the user's own text. The model may
    pick the stage, but an order number it returns is accepted only if it
    appears in the text -- otherwise the model could steer a remix towards an
    order the planner never named."""
    in_text = {m.upper() for m in ORDER_RE.findall(text)}
    order = ORDER_RE.search(text).group(0).upper() if in_text else None
    stages: list[str] = []
    source = "rules"
    if llm is not None:
        try:
            data = json.loads(llm.complete(_ROUTER_SYSTEM, text))
            stages = [s for s in data.get("stages", []) if s in KNOWN_STAGES]
            llm_order = data.get("order_number")
            if isinstance(llm_order, str) and llm_order.upper() in in_text:
                order = llm_order.upper()
            source = "llm"
        except Exception:
            stages = []
    if not stages:
        stages, source = _rules_stages(text), "rules"
    if not stages:
        stages = ["remix"] if order else ["analyze"]
    first, rest = stages[0], stages[1:]
    if first in ("document", "email"):  # presentation is only reachable from /remix/... deliberately
        first = "remix" if order else "analyze"
    deferred: list[str] = []
    for stage in rest:  # the stage being run is never also "deferred"; no duplicates
        if stage != first and stage not in deferred:
            deferred.append(stage)
    return Intent(stage=first, order_number=order, deferred_stages=deferred, source=source)


def numbers_in(text: str) -> set[str]:
    return {n.rstrip(".,").replace(",", "") for n in NUM_RE.findall(text)}


def explain(facts: dict[str, Any], template: str, llm: LLM | None = None) -> str:
    """Plain-language explanation. The model may only restate numbers in ``facts``."""
    if llm is None:
        return template
    try:
        text = llm.complete(
            "You explain a supply-planning result to a planner in 3-5 sentences. Use ONLY the facts given. "
            "Never invent or compute new numbers. Do not recommend sending anything to a customer.",
            json.dumps(facts, default=str),
        ).strip()
    except Exception:
        return template
    allowed = numbers_in(json.dumps(facts, default=str)) | numbers_in(template)
    if numbers_in(text) - allowed:
        return template  # the model said a number the engine never produced
    return text
