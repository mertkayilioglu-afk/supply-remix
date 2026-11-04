"""Demo: run the splitter + scoring engine over the synthetic order
book and print a few illustrative examples, plus the ranked summary.

Not part of the API — just a readable way to inspect Step 2's output.
"""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config.generator_config import GeneratorConfig
from app.config.scoring_config import ScoringConfig
from app.core.scoring import score_orders
from app.core.splitter import analyze_orders
from app.data.generator import SyntheticDataGenerator
from app.data.models import OrderAnalysis


def print_order_detail(order: OrderAnalysis) -> None:
    print(f"\n{'=' * 78}")
    print(f"ORDER {order.order_number}  |  customer: {order.customer_name} ({order.customer_id})")
    print(f"{'=' * 78}")
    print(f"Requested delivery date : {order.requested_delivery_date}")
    print(f"Availability of MIX     : {order.availability_of_mix or 'UNPLANNED (no resolvable date)'}")
    print(f"MIX status              : {order.mix_status}")
    print(f"Total qty               : {order.total_quantity}")
    print(f"On-time qty             : {order.on_time_quantity}")
    print(f"Recoverable qty         : {order.recoverable_quantity}")
    print(f"Delayed qty (planned)   : {order.delayed_quantity}")
    print(f"Unplanned qty           : {order.unplanned_quantity}")

    print("\n  Lines:")
    for ls in order.line_summaries:
        print(
            f"    {ls.order_line_id}  sku={ls.sku:<9} qty={ls.quantity:<4} "
            f"item_avail={str(ls.item_availability_date or 'UNPLANNED'):<12} "
            f"status={ls.item_status:<3} on_time={ls.on_time_quantity:<4} "
            f"recoverable={ls.recoverable_quantity:<4} delayed={ls.delayed_quantity:<4} "
            f"unplanned={ls.unplanned_quantity}"
        )
        line_segments = [s for s in order.segments if s.order_line_id == ls.order_line_id]
        for seg in line_segments:
            print(
                f"        segment {seg.segment_id:<16} qty={seg.available_qty:<4} "
                f"date={str(seg.available_date or 'UNPLANNED'):<12} "
                f"status={seg.status.value:<10} on_time={seg.is_on_time!s:<5} "
                f"recoverable={seg.is_recoverable}"
            )


def main() -> None:
    generator = SyntheticDataGenerator(GeneratorConfig())
    order_lines = generator.generate_orders()
    analyses = {a.order_number: a for a in analyze_orders(order_lines)}

    # Hand-picked illustrative examples (found via inspection of this
    # seed's output) covering the four required cases:
    examples = {
        "Order where every line is OK (nothing blocked)": "SO-100029",
        "Order blocked by a single late line (bottleneck)": "SO-100008",
        "Order containing an UNPLANNED chunk": "SO-100000",
    }
    for label, order_number in examples.items():
        print(f"\n>>> {label}")
        print_order_detail(analyses[order_number])

    # Also explicitly show a split-line-with-partial-recoverability case:
    # one line, two dates, one on-time+recoverable, one genuinely delayed.
    print("\n>>> Line split across dates with partial recoverability")
    print_order_detail(analyses["SO-100003"])

    # --- Ranked criticality summary ------------------------------------
    # Only NOK orders compete, exactly as /analyze does: OK orders have
    # nothing trapped to release.
    scored = [
        s
        for s in score_orders(
            list(analyses.values()), config=ScoringConfig(), as_of_date=date(2026, 4, 1)
        )
        if s.order.mix_status == "NOK"
    ]
    print(f"\n\n{'=' * 100}")
    print("RANKED AT-RISK SUMMARY (top 15 of {} NOK orders, for human review)".format(len(scored)))
    print(f"{'=' * 100}")
    header = (
        f"{'Order':<11} {'Customer':<20} {'Req. Date':<11} {'Avail of MIX':<14} "
        f"{'Mix':<4} {'Total':>6} {'Recov.':>7} {'Coef.':>7}"
    )
    print(header)
    print("-" * len(header))
    for s in scored[:15]:
        o = s.order
        print(
            f"{o.order_number:<11} {o.customer_name[:20]:<20} {str(o.requested_delivery_date):<11} "
            f"{str(o.availability_of_mix or 'UNPLANNED'):<14} {o.mix_status:<4} "
            f"{o.total_quantity:>6} {o.recoverable_quantity:>7} {s.coefficient:>7.1f}"
        )


if __name__ == "__main__":
    main()
