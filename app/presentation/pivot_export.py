"""Excel export of the customer-level pivot — the same aggregation
used by the HTML view (app.presentation.customer_pivot), written out
as a formatted .xlsx workbook via openpyxl.

Values here are static snapshot data (this is a point-in-time export
of an API response, not an editable model), so cells are written as
computed values rather than formulas — consistent with this being a
report, not a workbook the user rolls forward themselves.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.api.schemas import AnalyzeResponse
from app.presentation.customer_pivot import aggregate_by_customer

_HEADER_FILL = PatternFill(start_color="1B2430", end_color="1B2430", fill_type="solid")
_HEADER_FONT = Font(name="Arial", size=10, bold=True, color="FFFFFF")
_TITLE_FONT = Font(name="Arial", size=13, bold=True)
_SUBTITLE_FONT = Font(name="Arial", size=9, italic=True, color="5B6670")
_BODY_FONT = Font(name="Arial", size=10)
_NUMBER_FORMAT = "#,##0"
_CURRENCY_FORMAT = "$#,##0"

_COLUMNS = [
    ("Customer ID", "customer_id", 14, None),
    ("Customer", "customer_name", 24, None),
    ("At-risk orders", "order_count", 13, _NUMBER_FORMAT),
    ("Order numbers", "order_numbers", 40, None),
    ("Total qty", "total_quantity", 12, _NUMBER_FORMAT),
    ("Recoverable qty", "recoverable_quantity", 15, _NUMBER_FORMAT),
    ("Net Amount", "total_amount", 14, _CURRENCY_FORMAT),
    ("Recoverable Net Amount", "recoverable_amount", 20, _CURRENCY_FORMAT),
    ("Max coefficient", "max_coefficient", 14, "0.0"),
]


def render_pivot_workbook(response: AnalyzeResponse, plant: str | None = None) -> bytes:
    """Build the customer-level pivot as an .xlsx workbook. Same
    aggregation as the HTML pivot (see customer_pivot.aggregate_by_customer)
    — this export and the HTML page can never show different numbers
    for the same plant filter.
    """
    rows = aggregate_by_customer(response, plant=plant)

    wb = Workbook()
    ws = wb.active
    ws.title = "At-risk customers"

    ws["A1"] = "At-risk customers — pivot review"
    ws["A1"].font = _TITLE_FONT
    plant_label = plant if plant else "All plants"
    ws["A2"] = f"As of {response.as_of_date.isoformat()}  |  Plant filter: {plant_label}"
    ws["A2"].font = _SUBTITLE_FONT
    ws["A3"] = (
        "Net Amount = quantity x unit price, summed per line, matching the "
        "source system's field naming and per-line computation. Not a scoring "
        "input; shown for prioritization context alongside quantity."
    )
    ws["A3"].font = _SUBTITLE_FONT
    ws["A3"].alignment = Alignment(wrap_text=True)
    ws.merge_cells("A3:I3")
    ws.row_dimensions[3].height = 28

    header_row = 5
    for col_idx, (label, _, width, _) in enumerate(_COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=label)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    for r_idx, row in enumerate(rows, start=header_row + 1):
        values = {
            "customer_id": row.customer_id,
            "customer_name": row.customer_name,
            "order_count": row.order_count,
            "order_numbers": ", ".join(row.order_numbers),
            "total_quantity": row.total_quantity,
            "recoverable_quantity": row.recoverable_quantity,
            "total_amount": round(row.total_amount, 2),
            "recoverable_amount": round(row.recoverable_amount, 2),
            "max_coefficient": round(row.max_coefficient, 1),
        }
        for col_idx, (_, key, _, number_format) in enumerate(_COLUMNS, start=1):
            cell = ws.cell(row=r_idx, column=col_idx, value=values[key])
            cell.font = _BODY_FONT
            if number_format:
                cell.number_format = number_format

    ws.freeze_panes = ws.cell(row=header_row + 1, column=1)
    if rows:
        ws.auto_filter.ref = (
            f"A{header_row}:{get_column_letter(len(_COLUMNS))}{header_row + len(rows)}"
        )

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
