"""Customer-facing container plan -- ONE Excel document per order.

Mirrors the original tool's potential-availability view: every container the remix
engine built for the order, ordered by date, each with its fill % and OK/NOK
status. Late and UNPLANNED units are inside their containers (dated, or "to be
confirmed"), so nothing the customer ordered silently disappears.

Every unit of the TARGET order that falls in a containerised category group
must appear exactly once across the containers -- enforced by `reconcile`,
which raises StageError instead of producing a document that does not match
the customer's own records. Groups that could not be containerised are listed
in plain words with their reason (never silently omitted).
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from app.core.errors import StageError
from app.core.remix import GroupFailure, RemixResult, ShipmentContainer
from app.data.models import OrderAnalysis

STAGE = "Step 5 -- proposal document"
_TITLE = Font(name="Arial", size=13, bold=True)
_SUB = Font(name="Arial", size=10, color="5B6670")
_SEC = Font(name="Arial", size=11, bold=True, color="FFFFFF")
_FILL_OK = PatternFill(start_color="0F6A63", end_color="0F6A63", fill_type="solid")
_FILL_NOK = PatternFill(start_color="A6432A", end_color="A6432A", fill_type="solid")
_FILL_INFO = PatternFill(start_color="5B6670", end_color="5B6670", fill_type="solid")
_HDR = Font(name="Arial", size=10, bold=True, color="1B2430")
_HDR_FILL = PatternFill(start_color="E4E8E7", end_color="E4E8E7", fill_type="solid")
_BODY = Font(name="Arial", size=10)
_NOTE = Font(name="Arial", size=9, italic=True, color="5B6670")
_BOLD = Font(name="Arial", size=10, bold=True)

TBC = "To be confirmed"


def date_text(d: date | None) -> str:
    return d.isoformat() if d else TBC


def ready_text(d: date | None) -> str:
    """"ready 2026-03-03", or "ready date to be confirmed" when the date is unknown (UNPLANNED)."""
    return f"ready {d.isoformat()}" if d else "ready date to be confirmed"


def failure_categories(result: RemixResult, failure: GroupFailure) -> list[str]:
    """The target order's own categories that belong to a failed group."""
    cats = sorted(c for c, g in result.category_group_of.items() if g == failure.group)
    return cats or [failure.group]


def unsplit_note(result: RemixResult, failure: GroupFailure) -> str:
    """Customer-facing sentence: which category is NOT split, and why. Only
    restates figures the engine produced (pool CBM, containers needed)."""
    cats = " and ".join(failure_categories(result, failure))
    if failure.reason == "insufficient_volume":
        why = (
            f"your open items in this category add up to {failure.total_cbm:.1f} CBM, which is not "
            f"enough to fill {failure.containers_needed} container(s) to the minimum load"
        )
    elif failure.reason == "cannot_balance":
        why = (
            "there is enough volume in total, but it cannot be spread so that every container is "
            "loaded to the minimum without moving items that are already ready on time"
        )
    else:  # defensive: an unknown reason is shown, never hidden
        why = failure.message
    return f"{cats}: not split. These items stay on your current order and ship with it, because {why}."


def reconcile(result: RemixResult, target: OrderAnalysis) -> None:
    """Every unit of the target order inside a containerised group appears
    exactly once across the containers.

    The category -> group mapping comes from the engine
    (``result.category_group_of``), not from the container contents: deriving
    it from the containers would let a unit that is missing from every
    container also drop out of the expected total, and pass unnoticed."""
    built = {c.group for c in result.containers}
    expected: dict[str, int] = defaultdict(int)
    for seg in target.segments:
        group = result.category_group_of.get(seg.product_category, seg.product_category)
        if group in built:
            expected[seg.segment_id] += seg.available_qty
    got: dict[str, int] = defaultdict(int)
    for c in result.containers:
        for l in c.lines:
            if l.order_number == target.order_number:
                got[l.segment_id] += l.quantity
    if dict(expected) != dict(got):
        raise StageError(
            stage=STAGE,
            message="The containers do not account for every unit of the order.",
            expected=f"{sum(expected.values())} units of {target.order_number} across containers",
            received=f"{sum(got.values())} units",
        )


def _write_container(ws: Worksheet, row: int, c: ShipmentContainer) -> int:
    fill = _FILL_OK if c.status == "OK" else _FILL_NOK
    ws.cell(row=row, column=1, value=(
        f"Container {c.index} -- {c.reference} -- {c.status} -- "
        f"{ready_text(c.availability_date)} -- {c.utilization:.1%} full"
    )).font = _SEC
    for col in range(1, 8):
        ws.cell(row=row, column=col).fill = fill
    row += 1
    for i, h in enumerate(["Your original order #", "Product (SKU)", "Category", "Quantity", "Available date", "Status", "Net Amount"], 1):
        cell = ws.cell(row=row, column=i, value=h)
        cell.font, cell.fill = _HDR, _HDR_FILL
    row += 1
    for l in c.lines:
        vals = [l.order_number, l.sku, l.product_category, l.quantity, date_text(l.available_date), "OK" if l.is_ok else "NOK", l.net_amount]
        for i, v in enumerate(vals, 1):
            cell = ws.cell(row=row, column=i, value=v)
            cell.font = _BODY
        ws.cell(row=row, column=4).number_format = "#,##0"
        ws.cell(row=row, column=7).number_format = "$#,##0"
        row += 1
    ws.cell(row=row, column=3, value="Container total").font = _BOLD
    ws.cell(row=row, column=4, value=c.total_quantity).font = _BOLD
    ws.cell(row=row, column=7, value=c.net_amount).font = _BOLD
    ws.cell(row=row, column=4).number_format = "#,##0"
    ws.cell(row=row, column=7).number_format = "$#,##0"
    return row + 2


def render_proposal_workbook(result: RemixResult, target: OrderAnalysis) -> bytes:
    reconcile(result, target)
    wb = Workbook()
    ws = wb.active
    ws.title = "Container plan"
    ws["A1"] = f"Shipment plan for order {target.order_number}"
    ws["A1"].font = _TITLE
    ws["A2"] = f"{target.customer_name} ({target.customer_id})"
    ws["A2"].font = _SUB
    row = 4
    ws.cell(row=row, column=1, value="Current plan").font = _SEC
    for col in range(1, 8):
        ws.cell(row=row, column=col).fill = _FILL_INFO
    row += 1
    ws.cell(row=row, column=1, value=(
        f"Today the order ships as one shipment, {ready_text(target.availability_of_mix)} "
        f"(status {target.mix_status}), {target.total_quantity:,} units, Net Amount ${target.total_amount:,.0f}."
    )).font = _BODY
    row += 2
    if result.containers:
        ws.cell(row=row, column=1, value=(
            "Proposed plan: the units below are split into containers by availability date. "
            "Containers marked NOK still contain units that are late or not yet planned; they ship once "
            "their last unit is ready, and nothing is cancelled."
        )).font = _NOTE
        row += 2
        for c in sorted(result.containers, key=lambda c: (c.group, c.index)):
            row = _write_container(ws, row, c)
    if result.group_failures:
        ws.cell(row=row, column=1, value="Not split").font = _SEC
        for col in range(1, 8):
            ws.cell(row=row, column=col).fill = _FILL_INFO
        row += 1
        for f in result.group_failures:
            ws.cell(row=row, column=1, value=unsplit_note(result, f)).font = _BODY
            row += 1
    for i, w in enumerate([22, 18, 24, 12, 18, 10, 14], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.sheet_view.showGridLines = False
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
