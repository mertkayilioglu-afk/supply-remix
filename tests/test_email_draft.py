"""Tests for the draft customer email: addressed correctly, attaches the
proposal, describes the container plan in prose, and never sends anything."""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

from app.config.remix_config import RemixConfig
from app.core.remix import remix_order
from app.core.splitter import analyze_orders
from app.presentation.contacts import Contact
from app.presentation.email_draft import render_proposal_email
from app.presentation.proposal_document import render_proposal_workbook
from tests.test_remix import AS_OF, EARLY, LATE, MID, line
from tests.test_splitter import REQUESTED, confirmed


def draft():
    analyses = analyze_orders([line("SO-300", "SO-300-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])])
    result = remix_order("SO-300", analyses, RemixConfig(), as_of_date=AS_OF)
    target = analyses[0]
    contact = Contact("CUST-001", "Jamie Rossi", "jamie.rossi@customerco.example.com")
    xlsx = render_proposal_workbook(result, target)
    return message_from_bytes(render_proposal_email(result, target, contact, xlsx, "proposal.xlsx"), policy=policy.default), xlsx


def test_addressed_to_contact_and_subject_has_order() -> None:
    msg, _ = draft()
    assert "jamie.rossi@customerco.example.com" in msg["To"]
    assert "SO-300" in msg["Subject"]
    assert msg["X-Draft-Only"] == "true"


def test_attachment_matches_document() -> None:
    msg, xlsx = draft()
    [att] = list(msg.iter_attachments())
    assert att.get_filename() == "proposal.xlsx"
    assert att.get_content() == xlsx


def test_body_lists_containers_and_says_nothing_is_cancelled() -> None:
    msg, _ = draft()
    body = msg.get_body(preferencelist=("plain",)).get_content()
    assert "Container 1" in body and "Container 3" in body
    assert "Nothing is cancelled" in body


def test_module_has_no_send_capability() -> None:
    src = Path("app/presentation/email_draft.py").read_text()
    assert "import smtplib" not in src and "sendmail(" not in src and "send_message(" not in src
