"""Pivot review table — the human-facing presentation of /analyze.

This is the first of two presentation-layer views (see also the remix
proposal document, built next). It renders customer-level aggregates
of the same `AnalyzeResponse` the JSON /analyze endpoint returns — the
CHECKPOINT where a planner picks which customer, then which order, to
remix next. Rows are customers (not individual orders): a planner
triages by "who is most exposed" before drilling into "which specific
order." This mirrors the original tool's own PivotTable, which grouped
by customer first.

This module only renders/aggregates; it never re-derives analysis
data itself (see app/presentation/customer_pivot.py), so the HTML,
JSON, and Excel export can never silently disagree with each other.

Nothing on this page triggers /remix automatically — see the staged-
pipeline rationale in app/core/errors.py.
"""

from __future__ import annotations

import html
import json

from app.api.schemas import AnalyzeResponse
from app.presentation.customer_pivot import (
    CustomerPivotRow,
    aggregate_by_customer,
    all_plants,
)

_STYLE = """
:root {
    --bg: #F3F5F6;
    --panel: #FFFFFF;
    --ink: #1B2430;
    --muted: #5B6670;
    --line: #E4E8E7;
    --teal: #0F6A63;
    --teal-dim: #E4EFEE;
    --rust: #A6432A;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font: 15px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
    -webkit-font-smoothing: antialiased;
}
.mono {
    font-family: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
}
header {
    padding: 28px 32px 20px;
    border-bottom: 1px solid var(--line);
    background: var(--panel);
}
header h1 { font-size: 19px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }
header p.subtitle { margin: 0; color: var(--muted); font-size: 13.5px; }
.stats {
    display: flex;
    margin-top: 18px;
    border: 1px solid var(--line);
    border-radius: 6px;
    overflow: hidden;
    width: fit-content;
}
.stat { padding: 10px 20px; border-right: 1px solid var(--line); }
.stat:last-child { border-right: none; }
.stat .label { color: var(--muted); font-size: 12px; margin-bottom: 2px; }
.stat .value { font-size: 17px; font-weight: 600; }
.controls {
    padding: 16px 32px;
    display: flex;
    align-items: center;
    gap: 14px;
    flex-wrap: wrap;
}
.controls input[type=search], .controls select {
    padding: 8px 12px;
    border: 1px solid var(--line);
    border-radius: 6px;
    font-size: 14px;
    background: var(--panel);
    color: var(--ink);
}
.controls input[type=search] { flex: 0 1 320px; }
.controls input[type=search]:focus, .controls select:focus {
    outline: 2px solid var(--teal);
    outline-offset: 1px;
}
.controls label { font-size: 12.5px; color: var(--muted); display: flex; align-items: center; gap: 6px; }
.controls .hint { color: var(--muted); font-size: 12.5px; }
.controls .spacer { flex: 1; }
.export-link {
    padding: 8px 14px;
    border: none;
    border-radius: 6px;
    background: var(--teal);
    color: #fff;
    text-decoration: none;
    font: inherit;
    font-size: 13.5px;
    font-weight: 600;
    cursor: pointer;
}
.export-link:hover { background: #0C544F; }
main { padding: 0 32px 40px; }
table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
}
thead th {
    text-align: left;
    padding: 10px 14px;
    font-size: 12.5px;
    font-weight: 600;
    color: var(--muted);
    border-bottom: 1px solid var(--line);
    cursor: pointer;
    user-select: none;
    white-space: nowrap;
}
thead th:hover { color: var(--ink); }
thead th.num, td.num { text-align: right; }
thead th .caret { display: inline-block; margin-left: 4px; opacity: 0.35; font-size: 10px; }
thead th.sorted .caret { opacity: 1; color: var(--teal); }
tbody td { padding: 11px 14px; border-bottom: 1px solid var(--line); vertical-align: top; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover { background: #FAFBFB; }
.customer-name { font-weight: 500; }
.customer-id { color: var(--muted); font-size: 12.5px; }
.order-list { color: var(--muted); font-size: 12px; margin-top: 2px; max-width: 320px; }
.coefficient { font-weight: 700; color: var(--teal); }
.amount { font-weight: 600; }
.recoverable { color: var(--teal); font-weight: 600; }
.muted { color: var(--muted); }
.empty { padding: 48px 32px; text-align: center; color: var(--muted); }
"""


def _fmt_amount(value: float) -> str:
    return f"${value:,.0f}"


def _row_to_dict(row: CustomerPivotRow) -> dict:
    order_list_display = ", ".join(row.order_numbers[:4])
    if len(row.order_numbers) > 4:
        order_list_display += f" +{len(row.order_numbers) - 4} more"
    search_blob = f"{row.customer_id} {row.customer_name} {' '.join(row.order_numbers)}".lower()
    return {
        "customer_id": row.customer_id,
        "customer_name": row.customer_name,
        "order_count": row.order_count,
        "order_list_display": order_list_display,
        "total_quantity": row.total_quantity,
        "total_quantity_display": f"{row.total_quantity:,}",
        "recoverable_quantity": row.recoverable_quantity,
        "recoverable_quantity_display": f"{row.recoverable_quantity:,}",
        "total_amount": row.total_amount,
        "total_amount_display": _fmt_amount(row.total_amount),
        "recoverable_amount": row.recoverable_amount,
        "recoverable_amount_display": _fmt_amount(row.recoverable_amount),
        "max_coefficient": row.max_coefficient,
        "max_coefficient_display": f"{row.max_coefficient:.1f}",
        "search": search_blob,
    }


def render_pivot_html(
    response: AnalyzeResponse, request_payload_json: str, export_url: str = "/pivot/export.xlsx"
) -> str:
    """Render the customer-level pivot as a standalone HTML page.

    Pure rendering + aggregation over an already-computed
    `AnalyzeResponse` (see app.presentation.customer_pivot for the
    aggregation itself) — this function does not call
    analyze_orders/score_orders.

    `request_payload_json` is the original /pivot request body,
    embedded (in a hidden form field) so the "Export to Excel" button
    can re-submit the exact same order data as a real form POST —
    this is a stateless service, so the export endpoint has no other
    way to see the order book than the browser sending it again.
    """
    plants = all_plants(response)

    plant_keys = ["__all__"] + plants
    datasets: dict[str, list[dict]] = {}
    summary: dict[str, dict] = {}
    for key in plant_keys:
        plant = None if key == "__all__" else key
        rows = aggregate_by_customer(response, plant=plant)
        datasets[key] = [_row_to_dict(r) for r in rows]
        summary[key] = {
            "customers": len(rows),
            "orders": sum(r.order_count for r in rows),
            "recoverable_units": sum(r.recoverable_quantity for r in rows),
            "recoverable_amount": _fmt_amount(sum(r.recoverable_amount for r in rows)),
        }

    data_json = json.dumps(datasets).replace("</", "<\\/")
    summary_json = json.dumps(summary).replace("</", "<\\/")

    plant_options = "".join(
        f'<option value="{html.escape(p)}">{html.escape(p)}</option>' for p in plants
    )

    if not datasets["__all__"]:
        body = (
            '<div class="empty">No actionable orders. Every order in this book '
            "ships within its tolerance window — nothing needs a remix right now.</div>"
        )
    else:
        body = """
        <table id="pivot-table">
            <thead>
                <tr>
                    <th data-key="customer_name">Customer <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="order_count" data-numeric="1">At-risk orders <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="total_quantity" data-numeric="1">Total qty <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="recoverable_quantity" data-numeric="1">Recoverable qty <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="total_amount" data-numeric="1">Net Amount <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="recoverable_amount" data-numeric="1">Recoverable Net Amount <span class="caret">&#9662;</span></th>
                    <th class="num" data-key="max_coefficient" data-numeric="1">Max coefficient <span class="caret">&#9662;</span></th>
                </tr>
            </thead>
            <tbody id="pivot-body"></tbody>
        </table>"""

    script = f"""
(function () {{
    const DATA = {data_json};
    const SUMMARY = {summary_json};
    const tbody = document.getElementById('pivot-body');
    const table = document.getElementById('pivot-table');
    const searchInput = document.getElementById('pivot-search');
    const plantSelect = document.getElementById('plant-filter');

    let currentPlant = '__all__';
    let sortState = {{ key: 'max_coefficient', dir: -1 }};

    function fmtAmount(v) {{ return '$' + Math.round(v).toLocaleString(); }}

    function renderStats() {{
        const s = SUMMARY[currentPlant];
        document.getElementById('stat-customers').textContent = s.customers;
        document.getElementById('stat-orders').textContent = s.orders;
        document.getElementById('stat-recoverable-units').textContent = s.recoverable_units.toLocaleString();
        document.getElementById('stat-recoverable-amount').textContent = s.recoverable_amount;
    }}

    function rowHtml(r) {{
        return `<tr>
            <td>
                <div class="customer-name">${{r.customer_name}}</div>
                <div class="customer-id mono">${{r.customer_id}}</div>
                <div class="order-list mono">${{r.order_list_display}}</div>
            </td>
            <td class="num">${{r.order_count}}</td>
            <td class="num mono">${{r.total_quantity_display}}</td>
            <td class="num mono ${{r.recoverable_quantity > 0 ? 'recoverable' : 'muted'}}">${{r.recoverable_quantity_display}}</td>
            <td class="num mono amount">${{r.total_amount_display}}</td>
            <td class="num mono ${{r.recoverable_amount > 0 ? 'recoverable' : 'muted'}}">${{r.recoverable_amount_display}}</td>
            <td class="num coefficient">${{r.max_coefficient_display}}</td>
        </tr>`;
    }}

    function render() {{
        let rows = (DATA[currentPlant] || []).slice();
        const q = searchInput.value.trim().toLowerCase();
        if (q) rows = rows.filter((r) => r.search.includes(q));
        rows.sort((a, b) => {{
            const av = a[sortState.key], bv = b[sortState.key];
            const cmp = typeof av === 'number' ? (av - bv) : String(av).localeCompare(String(bv));
            return cmp * sortState.dir;
        }});
        if (tbody) tbody.innerHTML = rows.map(rowHtml).join('');
        renderStats();
    }}

    if (plantSelect) {{
        plantSelect.addEventListener('change', () => {{
            currentPlant = plantSelect.value;
            render();
        }});
    }}
    if (searchInput) searchInput.addEventListener('input', render);

    // NodeList.forEach isn't supported in every embedded browser
    // engine (e.g. older WebKit builds); convert to a real Array first
    // so this works everywhere, not just in modern desktop browsers.
    function toArray(nodeList) {{ return [].slice.call(nodeList); }}

    if (table) {{
        toArray(table.querySelectorAll('th[data-key]')).forEach((th) => {{
            th.addEventListener('click', () => {{
                const key = th.dataset.key;
                const numeric = th.dataset.numeric === '1';
                sortState = {{ key, dir: sortState.key === key ? -sortState.dir : (numeric ? -1 : 1) }};
                toArray(table.querySelectorAll('th')).forEach((h) => h.classList.remove('sorted'));
                th.classList.add('sorted');
                const caret = th.querySelector('.caret');
                if (caret) caret.textContent = sortState.dir === 1 ? '\\u25B2' : '\\u25BC';
                render();
            }});
        }});
    }}

    render();
}})();
"""

    plant_filter_control = f"""
        <label>Plant
            <select id="plant-filter" name="plant" form="export-form">
                <option value="__all__">All plants</option>
                {plant_options}
            </select>
        </label>""" if plants else '<input type="hidden" id="plant-filter" value="__all__">'

    export_form = f"""
    <form id="export-form" method="post" action="{html.escape(export_url)}" target="_blank">
        <input type="hidden" name="payload" value='{html.escape(request_payload_json, quote=True)}'>
        <button type="submit" class="export-link">Export to Excel</button>
    </form>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pivot review — at-risk customers</title>
<style>{_STYLE}</style>
</head>
<body>
<header>
    <h1>At-risk customers</h1>
    <p class="subtitle">Ranked by each customer's highest at-risk order coefficient. Pick a customer, then an order number, then request its remix proposal — this list does not act on anything by itself.</p>
    <div class="stats">
        <div class="stat"><div class="label">As of</div><div class="value mono">{response.as_of_date.isoformat()}</div></div>
        <div class="stat"><div class="label">Customers affected</div><div class="value" id="stat-customers">0</div></div>
        <div class="stat"><div class="label">At-risk orders</div><div class="value" id="stat-orders">0</div></div>
        <div class="stat"><div class="label">Recoverable units</div><div class="value" id="stat-recoverable-units">0</div></div>
        <div class="stat"><div class="label">Recoverable Net Amount</div><div class="value" id="stat-recoverable-amount">$0</div></div>
    </div>
</header>
<div class="controls">
    <input type="search" id="pivot-search" placeholder="Search customer or order number" aria-label="Search">
    {plant_filter_control}
    <span class="hint">Click a column header to sort. The plant filter also applies to the Excel export.</span>
    <span class="spacer"></span>
    {export_form}
</div>
<main>
{body}
</main>
<script>{script}</script>
</body>
</html>"""
