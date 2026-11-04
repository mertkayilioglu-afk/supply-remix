"""Compare the default greedy packer with the optional MILP packer on the sample data.

    python scripts/compare_packers.py [--time-limit 10] [--retry-timeouts 60] [--out docs/packer_comparison_data.md]

For every at-risk (NOK) order it runs remix_order twice -- once per packer, with
identical pools and rules -- and reports:

* buildable orders: how many of the at-risk orders get at least one container;
* container count: total containers built;
* date spread inside a container: latest minus earliest dated unit (days);
  UNPLANNED units have no date and are left out of the spread;
* unit wait: days a dated unit waits for its container (container ready date
  minus the unit's own date), averaged over units in containers that have a
  ready date. This is what the spread costs the customer;
* runtime, and the MILP solver outcome per group (optimal / feasible-but-not-
  proven / infeasible / time limit hit).

Spread and wait are compared on orders BOTH packers build, so the averages
describe the same goods. Nothing is tuned to make either packer look better.

With --retry-timeouts N, every order whose MILP solve hit the time limit is run
again with N seconds, and listed in a second table next to the greedy result,
so a time-out is never mistaken for "impossible".
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.api.main import _to_order_line  # noqa: E402
from app.api.schemas import AnalyzeRequest  # noqa: E402
from app.config.remix_config import RemixConfig  # noqa: E402
from app.core.remix import RemixResult, ShipmentContainer, remix_order  # noqa: E402
from app.core.splitter import analyze_orders  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
AS_OF = date(2026, 4, 1)  # the generator's simulated "today"


@dataclass
class Run:
    result: RemixResult
    seconds: float


@dataclass
class Totals:
    buildable: int = 0
    containers: int = 0
    spans: list[int] = field(default_factory=list)
    waits: list[int] = field(default_factory=list)
    seconds: float = 0.0
    reasons: Counter[str] = field(default_factory=Counter)


def container_span(c: ShipmentContainer) -> int | None:
    dated = [l.available_date for l in c.lines if l.available_date is not None]
    return (max(dated) - min(dated)).days if dated else None


def unit_waits(c: ShipmentContainer) -> list[int]:
    if c.availability_date is None:  # waits on an UNPLANNED unit: no ready date to measure against
        return []
    return [
        (c.availability_date - l.available_date).days
        for l in c.lines
        if l.available_date is not None
        for _ in range(l.quantity)
    ]


def run(order: str, analyses: list, cfg: RemixConfig) -> Run:
    started = time.perf_counter()
    result = remix_order(order, analyses, cfg, as_of_date=AS_OF)
    return Run(result, time.perf_counter() - started)


def fmt(values: list[int], how: str) -> str:
    if not values:
        return "-"
    return f"{statistics.mean(values):.1f}" if how == "mean" else f"{max(values)}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--time-limit", type=float, default=RemixConfig().milp_time_limit_seconds)
    parser.add_argument("--retry-timeouts", type=float, default=None, metavar="SECONDS")
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "docs" / "packer_comparison_data.md")
    args = parser.parse_args()

    payload = json.loads((ROOT / "data" / "sample_order_book.json").read_text())
    lines = [_to_order_line(l) for l in AnalyzeRequest.model_validate(payload).order_lines]
    analyses = analyze_orders(lines)
    at_risk = [a.order_number for a in analyses if a.mix_status == "NOK"]

    greedy_cfg = RemixConfig()
    milp_cfg = RemixConfig(packer="milp", milp_time_limit_seconds=args.time_limit)

    runs: dict[str, tuple[Run, Run]] = {}
    solver = Counter()
    for i, order in enumerate(at_risk, 1):
        g, m = run(order, analyses, greedy_cfg), run(order, analyses, milp_cfg)
        runs[order] = (g, m)
        solver.update(m.result.solver_status.values())
        print(f"[{i}/{len(at_risk)}] {order}: greedy={len(g.result.containers)} milp={len(m.result.containers)} "
              f"({', '.join(m.result.solver_status.values()) or 'no solve'}) {m.seconds:.1f}s", file=sys.stderr)

    both = [o for o, (g, m) in runs.items() if g.result.containers and m.result.containers]
    tot = {"greedy": Totals(), "milp": Totals()}
    for o, pair in runs.items():
        for name, r in zip(("greedy", "milp"), pair):
            t = tot[name]
            t.seconds += r.seconds
            t.reasons["built" if r.result.containers else r.result.no_proposal_reason or "?"] += 1
            if r.result.containers:
                t.buildable += 1
                t.containers += len(r.result.containers)
            if o in both:
                for c in r.result.containers:
                    span = container_span(c)
                    if span is not None:
                        t.spans.append(span)
                    t.waits.extend(unit_waits(c))

    g, m = tot["greedy"], tot["milp"]
    rows = [
        ("At-risk orders", len(at_risk), len(at_risk)),
        ("Buildable orders (>= 1 container)", g.buildable, m.buildable),
        ("Containers built (all orders)", g.containers, m.containers),
        ("Orders built by both", len(both), len(both)),
        ("Mean container date spread, days (orders built by both)", fmt(g.spans, "mean"), fmt(m.spans, "mean")),
        ("Max container date spread, days (orders built by both)", fmt(g.spans, "max"), fmt(m.spans, "max")),
        ("Mean unit wait for its container, days (orders built by both)", fmt(g.waits, "mean"), fmt(m.waits, "mean")),
        ("Total runtime, seconds", f"{g.seconds:.1f}", f"{m.seconds:.1f}"),
    ]
    out = [
        "# Greedy vs MILP packer on the sample data",
        "",
        "Generated by `python scripts/compare_packers.py --time-limit "
        f"{args.time_limit:g}{f' --retry-timeouts {args.retry_timeouts:g}' if args.retry_timeouts else ''}` "
        f"(as-of {AS_OF.isoformat()}, PuLP/CBC, {args.time_limit:g}s limit per group solve). "
        "Same pools, same hard rules; greedy is the default.",
        "",
        "| Metric | Greedy (default) | MILP |",
        "|---|---:|---:|",
        *[f"| {a} | {b} | {c} |" for a, b, c in rows],
        "",
        "**Outcomes per order**",
        "",
        "| Outcome | Greedy | MILP |",
        "|---|---:|---:|",
        *[f"| {k} | {g.reasons.get(k, 0)} | {m.reasons.get(k, 0)} |" for k in sorted(set(g.reasons) | set(m.reasons))],
        "",
        "**MILP solver outcome per group solve:** "
        + ", ".join(f"{k}: {v}" for k, v in sorted(solver.items())),
        "",
        "**Orders where the two packers differ**",
        "",
        "| Order | Greedy | MILP | Greedy spread (mean/max) | MILP spread (mean/max) |",
        "|---|---|---|---|---|",
    ]
    for o, (gr, mr) in runs.items():
        def desc(r: Run) -> str:
            res = r.result
            return f"{len(res.containers)} containers" if res.containers else (res.no_proposal_reason or "?")

        def spread(r: Run) -> str:
            s = [x for c in r.result.containers if (x := container_span(c)) is not None]
            return f"{statistics.mean(s):.1f} / {max(s)}" if s else "-"

        if desc(gr) != desc(mr) or spread(gr) != spread(mr):
            out.append(f"| {o} | {desc(gr)} | {desc(mr)} | {spread(gr)} | {spread(mr)} |")
    timed_out = [o for o, (_, mr) in runs.items() if any("time limit" in v for v in mr.result.solver_status.values())]
    if args.retry_timeouts and timed_out:
        retry_cfg = RemixConfig(packer="milp", milp_time_limit_seconds=args.retry_timeouts)
        lim = f"{args.retry_timeouts:g}s"
        out += [
            "",
            f"**Orders whose MILP solve hit the {args.time_limit:g}s limit, re-run with {lim}**",
            "",
            f"| Order | Greedy | Greedy spread mean/max, wait | MILP {lim} | MILP {lim} solver | MILP {lim} spread mean/max, wait |",
            "|---|---|---|---|---|---|",
        ]

        def describe(r: Run) -> tuple[str, str]:
            res = r.result
            spans = [x for c in res.containers if (x := container_span(c)) is not None]
            waits = [w for c in res.containers for w in unit_waits(c)]
            what = f"{len(res.containers)} containers" if res.containers else (res.no_proposal_reason or "?")
            how = f"{statistics.mean(spans):.1f} / {max(spans)}, {statistics.mean(waits):.1f}" if spans and waits else "-"
            return what, how

        for o in timed_out:
            print(f"retry {o} at {lim}", file=sys.stderr)
            again = run(o, analyses, retry_cfg)
            gw, gh = describe(runs[o][0])
            mw, mh = describe(again)
            status = ", ".join(f"{k}: {v}" for k, v in again.result.solver_status.items())
            out.append(f"| {o} | {gw} | {gh} | {mw} | {status} | {mh} |")
    text = "\n".join(out) + "\n"
    args.out.write_text(text)
    print(text)


if __name__ == "__main__":
    main()
