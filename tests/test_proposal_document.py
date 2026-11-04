"""Tests for the customer-facing container plan (Excel)."""

from __future__ import annotations

import email
import email.policy
import io

import pytest
from openpyxl import load_workbook

from app.config.remix_config import RemixConfig
from app.core.errors import StageError
from app.core.remix import RemixResult, ShipmentContainer, remix_order
from app.core.splitter import analyze_orders
from app.presentation.contacts import Contact
from app.presentation.email_draft import render_proposal_email
from app.presentation.proposal_document import render_proposal_workbook
from tests.test_remix import AS_OF, EARLY, LATE, MID, line
from tests.test_splitter import REQUESTED, confirmed


def build(lines, target):
    analyses = analyze_orders(lines)
    result = remix_order(target, analyses, RemixConfig(), as_of_date=AS_OF)
    return result, next(a for a in analyses if a.order_number == target)


def cells(xlsx: bytes) -> list[str]:
    ws = load_workbook(io.BytesIO(xlsx)).active
    return [str(c.value) for row in ws.iter_rows() for c in row if c.value is not None]


def test_document_lists_every_container_with_status_and_fill() -> None:
    result, target = build([line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])], "SO-1")
    text = "\n".join(cells(render_proposal_workbook(result, target)))
    assert text.count("Container ") >= 3
    assert "OK" in text and "NOK" in text
    assert "% full" in text
    assert "Current plan" in text


def test_original_order_number_and_net_amount_are_shown() -> None:
    result, target = build([line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)], unit_price=5.0)], "SO-1")
    values = cells(render_proposal_workbook(result, target))
    assert "SO-1" in values
    assert "$445" in " ".join(values)  # 89 units x $5 in the current-plan line
    ws = load_workbook(io.BytesIO(render_proposal_workbook(result, target))).active
    totals = [ws.cell(row=r, column=7).value for r in range(1, ws.max_row + 1) if ws.cell(row=r, column=3).value == "Container total"]
    assert sum(totals) == 445


def test_document_states_reason_when_nothing_could_be_built() -> None:
    result, target = build([line("SO-1", "SO-1-L1", 20, REQUESTED, [confirmed(10, EARLY), confirmed(10, LATE)])], "SO-1")
    text = "\n".join(cells(render_proposal_workbook(result, target)))
    assert "SO-1" in text and "Dishwashers: not split" in text


def test_reconcile_fails_visibly_if_a_unit_is_missing() -> None:
    result, target = build([line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])], "SO-1")
    broken = RemixResult(**{**result.__dict__, "containers": result.containers[:-1]})
    with pytest.raises(StageError):
        render_proposal_workbook(broken, target)


def test_reconcile_catches_a_missing_unit_in_a_linked_group() -> None:
    """Ovens and Hobs are linked. If every Oven unit of the target vanished from the
    containers, the old reconciliation (which guessed groups from container contents)
    stopped expecting them and passed. It must fail."""
    cfg = RemixConfig(category_groups={"Ovens": "Cooking", "Hobs": "Cooking"})
    lines = [
        line("SO-1", "SO-1-L1", 2, REQUESTED, [confirmed(1, EARLY), confirmed(1, LATE)], product_category="Ovens", sku="OV"),
        line("SO-1", "SO-1-L2", 28, REQUESTED, [confirmed(28, EARLY)], product_category="Hobs", sku="HB"),
    ]
    analyses = analyze_orders(lines)
    result = remix_order("SO-1", analyses, cfg, as_of_date=AS_OF)
    target = analyses[0]
    assert result.containers and result.category_group_of == {"Ovens": "Cooking", "Hobs": "Cooking"}
    render_proposal_workbook(result, target)  # the real result reconciles
    c = result.containers[0]
    stripped = [ln for ln in c.lines if ln.product_category != "Ovens"]
    broken_container = ShipmentContainer(**{**c.__dict__, "lines": stripped})
    broken = RemixResult(**{**result.__dict__, "containers": [broken_container] + result.containers[1:]})
    with pytest.raises(StageError):
        render_proposal_workbook(broken, target)


def _partial() -> tuple[RemixResult, object]:
    """Dishwashers fill one container; Ovens (10 CBM) cannot fill one."""
    return build([
        line("SO-1", "SO-1-L1", 29, REQUESTED, [confirmed(20, EARLY), confirmed(9, LATE)]),
        line("SO-1", "SO-1-L2", 10, REQUESTED, [confirmed(10, LATE)], product_category="Ovens", sku="OV"),
    ], "SO-1")


def test_partial_proposal_document_states_which_category_is_not_split_and_why() -> None:
    result, target = _partial()
    assert result.containers and [f.reason for f in result.group_failures] == ["insufficient_volume"]
    text = "\n".join(cells(render_proposal_workbook(result, target)))
    assert "Not split" in text
    assert "Ovens: not split. These items stay on your current order" in text
    assert "10.0 CBM" in text and "minimum load" in text


def test_partial_proposal_email_states_which_category_is_not_split_and_why() -> None:
    result, target = _partial()
    contact = Contact(customer_id="CUST-001", contact_name="Sam Kim", email="sam@example.com")
    body = email.message_from_bytes(
        render_proposal_email(result, target, contact, b"x", "p.xlsx"), policy=email.policy.default
    ).get_body(preferencelist=("plain",)).get_content()
    assert "Ovens: not split" in body and "10.0 CBM" in body
    assert "we propose splitting it" in body


def test_email_with_no_containers_does_not_promise_a_split() -> None:
    result, target = build([line("SO-1", "SO-1-L1", 20, REQUESTED, [confirmed(10, EARLY), confirmed(10, LATE)])], "SO-1")
    contact = Contact(customer_id="CUST-001", contact_name="Sam Kim", email="sam@example.com")
    body = email.message_from_bytes(
        render_proposal_email(result, target, contact, b"x", "p.xlsx"), policy=email.policy.default
    ).get_body(preferencelist=("plain",)).get_content()
    assert "cannot propose a split" in body
    assert "containers below" not in body
