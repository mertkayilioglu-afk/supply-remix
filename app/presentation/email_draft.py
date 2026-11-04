"""Draft customer email for a remix proposal — ONE email per order,
covering every proposal for it (see proposal_document.py for why: a
customer reconciling this wants one document/email for their order,
not one per product category).

This module ONLY constructs an RFC 822 message object and serializes
it to .eml bytes. It never imports smtplib, never opens a network
connection, and has no send capability of any kind — the same rule
that applies to the rest of this codebase (see app/core/errors.py for
why every stage here is a deliberate, human-reviewed checkpoint). The
caller is responsible for saving the returned bytes to a file for a
person to open, review, and send themselves.
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.core.remix import RemixResult
from app.data.models import OrderAnalysis
from app.presentation.contacts import Contact
from app.presentation.proposal_document import date_text, unsplit_note

_DEFAULT_SENDER_NAME = "Supply Planning Team"
_DEFAULT_SENDER_EMAIL = "planning-team@example.com"  # placeholder -- replace with the real sender


def _build_body(result: RemixResult, target: OrderAnalysis, contact: Contact) -> str:
    lines = [f"Dear {contact.contact_name},", ""]
    lines.append(
        f"Your order {target.order_number} currently ships as a single shipment, ready "
        f"{date_text(target.availability_of_mix)}, because it has to wait for its slowest item. "
        + (
            "To get you your goods sooner, we propose splitting it into the containers below, in order of availability. "
            "Nothing is cancelled: items that are still late or not yet planned are in their own containers and ship once they are ready."
            if result.containers
            else "We looked at splitting it into earlier containers, but cannot propose a split at this time; the reasons are below. "
            "Nothing is cancelled: the order ships as planned."
        )
    )
    lines.append("")
    for c in sorted(result.containers, key=lambda c: (c.group, c.index)):
        state = (
            "all items ready on time"
            if c.status == "OK"
            else f"{c.total_quantity - c.ok_quantity} of {c.total_quantity} units still late or not yet planned"
        )
        lines.append(
            f"  - Container {c.index} ({c.group}): ready {date_text(c.availability_date)}, "
            f"{c.total_quantity:,} units, {c.utilization:.1%} full -- {state}."
        )
    if result.group_failures:
        lines += ["", "Not split:"]
        lines += [f"  - {unsplit_note(result, f)}" for f in result.group_failures]
    lines += [
        "",
        "We are sending this for your review before anything is finalised. Could you confirm this works for you, "
        "or let us know if you would rather we keep everything together until the full order is ready? "
        "The attached document lists every product by your original order number, with quantities, dates and Net Amount.",
        "",
        "Best regards,",
        _DEFAULT_SENDER_NAME,
        "",
    ]
    return "\n".join(lines)


def email_subject(target: OrderAnalysis) -> str:
    return f"Your order {target.order_number} — proposal for an early partial shipment"


def render_proposal_email_preview(
    result: RemixResult, target: OrderAnalysis, contact: Contact
) -> dict[str, str]:
    """The draft's text for on-screen review (dashboard). Same body and subject as
    the .eml; nothing is sent and no message object is created."""
    return {
        "to": f"{contact.contact_name} <{contact.email}>",
        "subject": email_subject(target),
        "body": _build_body(result, target, contact),
    }


def render_proposal_email(
    result: RemixResult,
    target: OrderAnalysis,
    contact: Contact,
    attachment_bytes: bytes,
    attachment_filename: str,
) -> bytes:
    """Build a complete .eml file (RFC 822 message, with the proposal
    document attached) as raw bytes, ready to save to disk. Covers
    every proposal for the target order in one email. Does not send
    anything — there is no SMTP call in this function or anywhere in
    this codebase. The person opens the saved .eml in their mail
    client, reviews it, and sends it themselves.
    """
    msg = EmailMessage()
    msg["From"] = f"{_DEFAULT_SENDER_NAME} <{_DEFAULT_SENDER_EMAIL}>"
    msg["To"] = f"{contact.contact_name} <{contact.email}>"
    msg["Subject"] = email_subject(target)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["X-Draft-Only"] = "true"  # explicit marker: this file was generated, never sent

    msg.set_content(_build_body(result, target, contact))
    msg.add_attachment(
        attachment_bytes,
        maintype="application",
        subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=attachment_filename,
    )

    return msg.as_bytes()
