"""Optional MILP packer (PuLP + CBC) -- an exact comparison point for the greedy packer.

Not the default. Enabled with ``RemixConfig(packer="milp")``. It solves ONE
category group of ONE order at a time, with the same hard rules as the greedy
packer:

* every unit of the pool is placed (late and UNPLANNED units included);
* every container is within [floor, capacity] (94.5%-100% of 30 CBM);
* the container count is the same ``ceil(volume / capacity)`` the rule defines;
* same customer and category group -- it receives exactly the greedy's pool;
* "a fully-OK container never donates": the greedy enforces this as a step in
  its repair. A MILP has no steps, so it is stated as a constraint: the
  date-ordered first fill (the shared ``_fill``) yields ``k`` fully-OK
  containers that are already within bounds (the ones the greedy locks), and
  the MILP must also have at least ``k`` containers that hold ONLY OK units
  and are within bounds. It may not dissolve an OK container to
  rescue a late one; it may shift OK volume only as far as the owner's swap
  rule allows (the container stays fully OK and within bounds).

Objective: minimise the total date spread inside containers (latest minus
earliest dated unit; UNPLANNED units have no date and do not count).

Units of one segment are identical (same date, CBM, OK status), so the model
uses an integer quantity per segment and container, not one variable per unit.
Every solution is re-checked by plain Python after the solve; the solver's
word is not trusted on its own.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from app.core.remix import _Unit

MilpStatus = Literal["optimal", "feasible", "infeasible", "timeout", "unavailable"]


@dataclass(frozen=True)
class MilpOutcome:
    status: MilpStatus
    boxes: list[list["_Unit"]] | None  # None unless status is optimal/feasible
    seconds: float


def cbc_solver(time_limit_seconds: float) -> Any:
    """CBC through PuLP. With the ``pulp[cbc]`` extra (PuLP 3.x/4.x) it is COIN_CMD;
    old PuLP 2.x bundles it as PULP_CBC_CMD (deprecated in 3.x, removed in 4.x).
    Same solver either way; COIN_CMD is preferred whenever it can find CBC."""
    import pulp

    solver = pulp.COIN_CMD(msg=False, timeLimit=time_limit_seconds)
    legacy = getattr(pulp, "PULP_CBC_CMD", None)
    if legacy is not None and not solver.available():
        return legacy(msg=False, timeLimit=time_limit_seconds)
    return solver


def pulp_available() -> bool:
    try:
        return bool(cbc_solver(1.0).available())
    except Exception:  # ImportError, or PuLP present without a CBC binary
        return False


def pack_milp(
    units: list["_Unit"],
    n: int,
    cap: float,
    floor: float,
    min_fully_ok: int,
    time_limit_seconds: float,
) -> MilpOutcome:
    """Solve one group. ``units`` is the expanded, date-sorted pool."""
    import time

    if not pulp_available():
        return MilpOutcome("unavailable", None, 0.0)
    import pulp

    started = time.perf_counter()

    by_seg: dict[str, list["_Unit"]] = defaultdict(list)
    for u in units:
        by_seg[u.seg.segment_id].append(u)
    segs = list(by_seg)
    qty = {s: len(by_seg[s]) for s in segs}
    cbm = {s: by_seg[s][0].cbm for s in segs}
    ok = {s: by_seg[s][0].ok for s in segs}
    dates = {s: by_seg[s][0].seg.available_date for s in segs}
    dated = [s for s in segs if dates[s] is not None]
    base = min((dates[s] for s in dated), default=None)
    day = {s: (dates[s] - base).days for s in dated}  # type: ignore[operator]
    big_m = max(day.values(), default=0)
    boxes_idx = range(n)

    prob = pulp.LpProblem("container_remix", pulp.LpMinimize)
    def var(name: str, low: float | None, up: float | None, cat: str) -> Any:
        # PuLP 4.x creates variables on the problem; PuLP 2.x via LpVariable.
        if hasattr(prob, "add_variable"):
            return prob.add_variable(name, low, up, cat)
        return pulp.LpVariable(name, low, up, cat=cat)

    x = {(s, b): var(f"x_{i}_{b}", 0, qty[s], "Integer") for i, s in enumerate(segs) for b in boxes_idx}
    z = {(s, b): var(f"z_{i}_{b}", 0, 1, "Binary") for i, s in enumerate(segs) for b in boxes_idx}
    y = {b: var(f"fully_ok_{b}", 0, 1, "Binary") for b in boxes_idx}
    latest = {b: var(f"latest_{b}", 0, big_m, "Continuous") for b in boxes_idx}
    earliest = {b: var(f"earliest_{b}", 0, big_m, "Continuous") for b in boxes_idx}

    prob += pulp.lpSum(latest[b] - earliest[b] for b in boxes_idx)  # total date spread

    for s in segs:
        prob += pulp.lpSum(x[s, b] for b in boxes_idx) == qty[s]  # every unit placed
        for b in boxes_idx:
            prob += x[s, b] <= qty[s] * z[s, b]  # z = 1 if any unit of s is in b
            prob += z[s, b] <= x[s, b]
            if not ok[s]:
                prob += z[s, b] <= 1 - y[b]  # a fully-OK container holds no NOK unit
            if s in day:
                prob += latest[b] >= day[s] * z[s, b]
                prob += earliest[b] <= day[s] + big_m * (1 - z[s, b])
    for b in boxes_idx:
        load = pulp.lpSum(cbm[s] * x[s, b] for s in segs)
        prob += load >= floor  # 94.5% floor, every container
        prob += load <= cap  # 100% capacity
        prob += latest[b] >= earliest[b]
        if b + 1 < n:
            prob += latest[b] <= latest[b + 1]  # number containers by date (symmetry breaking)
    prob += pulp.lpSum(y.values()) >= min_fully_ok  # never-donate, as a constraint

    outcome = _classify(prob.solve(cbc_solver(time_limit_seconds)), prob)
    seconds = time.perf_counter() - started
    if outcome in ("infeasible", "timeout"):
        return MilpOutcome(outcome, None, seconds)

    boxes: list[list["_Unit"]] = [[] for _ in boxes_idx]
    for s in segs:
        pool = list(by_seg[s])
        for b in boxes_idx:
            k = int(round(x[s, b].value() or 0))
            boxes[b].extend(pool[:k])
            del pool[:k]
        if pool:  # rounding left units unplaced: treat as no valid solution
            return MilpOutcome("timeout", None, seconds)
    for box in boxes:
        box.sort(key=lambda u: u.key)
    return MilpOutcome(outcome, boxes, seconds)


def _classify(ret: Any, prob: Any) -> MilpStatus:
    """optimal | feasible (integer solution, optimality not proven, e.g. time limit) |
    infeasible (proven) | timeout (no solution found in time). Handles PuLP 4.x
    (solve() returns LpSolveStats) and PuLP 2.x (status codes on the problem)."""
    import pulp

    if hasattr(ret, "has_solution"):  # PuLP 4.x
        status = ret.status
        if status == pulp.LpSolveStatus.Infeasible:
            return "infeasible"
        if not ret.has_solution:
            return "timeout"
        # CBC often stops with GapLimit at a zero gap, i.e. proven optimal. The
        # relative gap is meaningless when the objective is ~0 (it reads 1.0), so
        # the absolute gap (objective is in whole days) is checked too.
        zero_gap = (ret.gap_abs is not None and abs(ret.gap_abs) <= 1e-6) or (
            ret.gap_rel is not None and ret.gap_rel <= 1e-9
        )
        proven = status == pulp.LpSolveStatus.Optimal or (status == pulp.LpSolveStatus.GapLimit and zero_gap)
        return "optimal" if proven else "feasible"
    sol = getattr(prob, "sol_status", None)  # PuLP 2.x
    if sol == pulp.LpSolutionInfeasible or prob.status == pulp.LpStatusInfeasible:
        return "infeasible"
    if sol == pulp.LpSolutionOptimal:
        return "optimal"
    if sol == pulp.LpSolutionIntegerFeasible:
        return "feasible"
    return "timeout"
