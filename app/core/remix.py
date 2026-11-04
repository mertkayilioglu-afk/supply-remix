"""Remix engine -- date-ordered containers for ALL units of a customer's NOK orders.

Mirrors the original tool's potential-availability view. For one target order:

    target order
      -> find_remix_pool: every unit (OK, late AND UNPLANNED) of the same
         customer's NOK orders, in the target's category group(s)
      -> per group: sort units by availability date, fill containers
         earliest-first (first container gets the earliest material)
      -> rebalance: if the last container is under the floor, borrow units
         from the container(s) above WITHOUT pushing the donor under the
         floor and NEVER from a fully-OK donor
      -> verify: every container within [floor, capacity] or no proposal

Late/UNPLANNED units stay in the containers (with their own dates) so the
customer sees them waiting rather than cancelled; a container's status is NOK
if any of its units is NOK, and that is shown, not hidden.

The hard rule (94.5% floor on EVERY container) is all-or-nothing per group:
if total volume V cannot satisfy ``floor * ceil(V / capacity)``, containers
cannot be created -- the missing volume does not exist anywhere else for this
customer/group either -- and the group fails with an explicit reason.

Reasons: ``no_candidates`` | ``insufficient_volume`` | ``cannot_balance``, or
``mixed`` when the order's category groups failed for different reasons (each
group's own reason is in ``group_failures``).

CHECKPOINT: this stage only runs for an order a human picked from the ranked
list (see app/core/errors.py). The criticality coefficient is shown beside the
result as ``target_order_coefficient`` but is deliberately not mixed into any
packing decision.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.config.analysis_config import AnalysisConfig
from app.config.remix_config import RemixConfig
from app.config.scoring_config import ScoringConfig
from app.core.errors import StageError
from app.core.scoring import score_order
from app.core.splitter import within_tolerance
from app.data.models import OrderAnalysis, OrderLineSegment

STAGE = "Step 3 -- remix"
EPS = 1e-9


@dataclass(frozen=True)
class ContainerLine:
    """Units of one source segment placed in one container."""

    order_number: str
    order_line_id: str
    segment_id: str
    sku: str
    product_category: str
    quantity: int
    available_date: date | None
    requested_delivery_date: date
    is_ok: bool
    cbm: float
    net_amount: float
    calendar_shifted_from: date | None = None  # original date if the working-day calendar moved it


@dataclass(frozen=True)
class ShipmentContainer:
    reference: str  # new order reference
    group: str
    index: int  # 1 = earliest
    lines: list[ContainerLine]
    cbm: float
    utilization: float
    availability_date: date | None  # latest unit date; None if any UNPLANNED
    status: str  # "OK" only if every unit is OK, else "NOK"
    ok_quantity: int
    total_quantity: int
    net_amount: float


@dataclass(frozen=True)
class GroupFailure:
    group: str
    reason: str  # insufficient_volume | cannot_balance
    message: str
    total_cbm: float
    containers_needed: int


@dataclass(frozen=True)
class RemixResult:
    target_order_number: str
    customer_id: str
    target_order_coefficient: float
    category_groups: list[str]
    # category -> group for every category in the target order. Lets the
    # document stage reconcile against the engine's own grouping instead of
    # re-deriving it from container contents.
    category_group_of: dict[str, str]
    containers: list[ShipmentContainer]
    group_failures: list[GroupFailure]
    no_proposal_reason: str | None  # set when there are no containers at all; "mixed" if groups disagree
    message: str | None
    # Only with packer="milp": group -> solver outcome (optimal / feasible /
    # infeasible / timeout ...), so a comparison can tell a proven "no" from a
    # time-out. Empty for the default greedy packer.
    solver_status: dict[str, str] = field(default_factory=dict)


@dataclass
class _Unit:
    seg: OrderLineSegment
    ok: bool
    cbm: float

    @property
    def key(self) -> tuple[bool, date, str, str]:
        d = self.seg.available_date
        return (d is None, d or date.max, self.seg.order_number, self.seg.segment_id)


def group_of(category: str, config: RemixConfig) -> str:
    """Category group; unlisted categories are their own group."""
    return config.category_groups.get(category, category)


def find_remix_pool(
    order_number: str, analyses: list[OrderAnalysis], config: RemixConfig
) -> tuple[OrderAnalysis, dict[str, list[OrderLineSegment]]]:
    """All segments of the target customer's NOK orders, split by the
    target's category group(s). Empty pools if the target itself is OK."""
    target = next((a for a in analyses if a.order_number == order_number), None)
    if target is None:
        raise StageError(
            stage=STAGE,
            message=f"Order {order_number!r} was not found.",
            expected="an order_number present in the provided analyses",
            received=repr(order_number),
        )

    if target.mix_status != "NOK":
        return target, {}
    groups = {group_of(s.product_category, config) for s in target.segments}
    pools: dict[str, list[OrderLineSegment]] = {g: [] for g in groups}
    for order in analyses:
        if order.customer_id != target.customer_id or order.mix_status != "NOK":
            continue
        for seg in order.segments:
            g = group_of(seg.product_category, config)
            if g in pools:
                pools[g].append(seg)
    return target, pools


def _expand(pool: list[OrderLineSegment], tolerance_days: int) -> list[_Unit]:
    units: list[_Unit] = []
    for seg in pool:
        ok = within_tolerance(seg.available_date, seg.requested_delivery_date, tolerance_days)
        units.extend(_Unit(seg, ok, seg.cbm_per_unit) for _ in range(seg.available_qty))
    units.sort(key=lambda u: u.key)
    return units


def _cbm(units: list[_Unit]) -> float:
    return sum(u.cbm for u in units)


def _fill(units: list[_Unit], n: int, cap: float, floor: float, reserve: float = 0.0) -> list[list[_Unit]]:
    """Earliest-first fill. Container i may take at most
    ``min(cap, remaining - floor * (n - i))`` so the containers after it can
    still reach the floor; it takes the earliest units that fit (a unit that
    does not fit is skipped in favour of a later, smaller one). Whatever is
    left lands in the last container."""
    remaining = list(units)
    boxes: list[list[_Unit]] = []
    for i in range(n - 1):
        left = _cbm(remaining)
        limit = min(cap, left - (floor + reserve) * (n - 1 - i))
        box: list[_Unit] = []
        used = 0.0
        rest: list[_Unit] = []
        for u in remaining:
            if used + u.cbm <= limit + EPS:
                box.append(u)
                used += u.cbm
            else:
                rest.append(u)
        # Granularity fix: if still under the floor, swap a box unit for a
        # bigger leftover unit (latest box units first) until it lands.
        improved = True
        while used < floor - EPS and improved:
            improved = False
            for bi in range(len(box) - 1, -1, -1):
                best_ri, best_gain = None, 0.0
                for ri, r in enumerate(rest):
                    gain = r.cbm - box[bi].cbm
                    if gain > EPS and used + gain <= limit + EPS and gain > best_gain:
                        best_ri, best_gain = ri, gain
                if best_ri is not None:
                    box[bi], rest[best_ri] = rest[best_ri], box[bi]
                    used += best_gain
                    improved = True
                    break
        box.sort(key=lambda u: u.key)
        rest.sort(key=lambda u: u.key)
        boxes.append(box)
        remaining = rest
    boxes.append(remaining)
    return boxes


def _fully_ok(box: list[_Unit]) -> bool:
    return bool(box) and all(u.ok for u in box)


def _violation(boxes: list[list[_Unit]], floor: float, cap: float) -> float:
    return sum(max(0.0, floor - _cbm(b)) + max(0.0, _cbm(b) - cap) for b in boxes)


def _ready_anchor(box: list[_Unit]) -> date | None:
    """A container's ready date for the delay guard. None = no limit: the box is
    empty, or already waits on an UNPLANNED unit (its ready date is unknown)."""
    dates = [u.seg.available_date for u in box]
    if not dates or any(d is None for d in dates):
        return None
    return max(dates)  # type: ignore[type-var]


def _rebalance(
    boxes: list[list[_Unit]], floor: float, cap: float, max_delay_days: int | None = None
) -> None:
    """Repair containers outside [floor, cap] by moving units between them.

    Rules: a fully-OK container never gives a unit away. It may only swap one
    of its units for an OK unit, and only if after the swap it is still fully
    OK AND still within [floor, cap] (owner decision); borrowing from
    containers ABOVE (earlier) is tried first, and later ones only if that
    cannot fix it. A step is kept only if it strictly lowers the total
    violation, so the loop always terminates.

    Delay guard (``max_delay_days``, owner decision): a container that RECEIVES
    a unit (by a move, or either side of a swap) may not end up ready more than
    that many days after its ready date at the start of this repair."""
    anchors = [_ready_anchor(b) for b in boxes]

    def may_receive(i: int, unit: _Unit) -> bool:
        if max_delay_days is None or anchors[i] is None:
            return True
        d = unit.seg.available_date
        return d is not None and d <= anchors[i] + timedelta(days=max_delay_days)  # type: ignore[operator]

    for scope in ("above", "any"):
        for _ in range(5000):
            sizes = [_cbm(b) for b in boxes]
            cur = sum(max(0.0, floor - c) + max(0.0, c - cap) for c in sizes)
            if cur <= EPS:
                return
            best: tuple[float, str, int, int, int, int] | None = None
            bad = [k for k, b in enumerate(boxes) if _cbm(b) < floor - EPS or _cbm(b) > cap + EPS]
            for k in bad:
                donors = range(k) if scope == "above" else [j for j in range(len(boxes)) if j != k]
                for j in donors:
                    lock = _fully_ok(boxes[j])
                    for ui, u in enumerate(boxes[j]):
                        if not lock and may_receive(k, u):  # plain move j -> k
                            ck, cj = sizes[k] + u.cbm, sizes[j] - u.cbm
                            new = _violation_pair(sizes, k, j, ck, cj, floor, cap)
                            if new < cur - EPS and (best is None or new < best[0] - EPS):
                                best = (new, "move", k, j, ui, -1)
                        for vi, v in enumerate(boxes[k]):  # swap u (in j) <-> v (in k)
                            if lock and not v.ok:
                                continue
                            if not (may_receive(k, u) and may_receive(j, v)):
                                continue  # would delay a container beyond the guard
                            d = u.cbm - v.cbm
                            if abs(d) < EPS:
                                continue
                            ck, cj = sizes[k] + d, sizes[j] - d
                            if lock and not (floor - EPS <= cj <= cap + EPS):
                                continue  # a fully-OK container must stay within bounds
                            new = _violation_pair(sizes, k, j, ck, cj, floor, cap)
                            if new < cur - EPS and (best is None or new < best[0] - EPS):
                                best = (new, "swap", k, j, ui, vi)
            if best is None:
                break
            _, kind, k, j, ui, vi = best
            u = boxes[j].pop(ui)
            if kind == "swap":
                boxes[j].append(boxes[k].pop(vi))
            boxes[k].append(u)
            boxes[j].sort(key=lambda x: x.key)
            boxes[k].sort(key=lambda x: x.key)


def _violation_pair(
    sizes: list[float], k: int, j: int, ck: float, cj: float, floor: float, cap: float
) -> float:
    total = 0.0
    for i, base in enumerate(sizes):
        c = ck if i == k else cj if i == j else base
        total += max(0.0, floor - c) + max(0.0, c - cap)
    return total


def _build_container(
    ref: str, group: str, index: int, box: list[_Unit], cap: float
) -> ShipmentContainer:
    agg: dict[tuple[str, str], list[_Unit]] = {}
    for u in box:
        k = (u.seg.order_line_id, u.seg.segment_id)
        agg.setdefault(k, []).append(u)
    lines: list[ContainerLine] = []
    for us in agg.values():
        s = us[0].seg
        lines.append(
            ContainerLine(
                order_number=s.order_number,
                order_line_id=s.order_line_id,
                segment_id=s.segment_id,
                sku=s.sku,
                product_category=s.product_category,
                quantity=len(us),
                available_date=s.available_date,
                requested_delivery_date=s.requested_delivery_date,
                is_ok=us[0].ok,
                cbm=len(us) * s.cbm_per_unit,
                net_amount=len(us) * s.unit_price,
                calendar_shifted_from=s.calendar_shifted_from,
            )
        )
    lines.sort(key=lambda l: (l.available_date is None, l.available_date or date.max, l.order_number, l.segment_id))
    dates = [l.available_date for l in lines]
    cbm = sum(l.cbm for l in lines)
    return ShipmentContainer(
        reference=ref,
        group=group,
        index=index,
        lines=lines,
        cbm=cbm,
        utilization=cbm / cap,
        availability_date=None if any(d is None for d in dates) else max(dates),
        status="OK" if all(l.is_ok for l in lines) else "NOK",
        ok_quantity=sum(l.quantity for l in lines if l.is_ok),
        total_quantity=sum(l.quantity for l in lines),
        net_amount=sum(l.net_amount for l in lines),
    )


def _pack_with_milp(
    units: list[_Unit], n: int, cap: float, floor: float, config: RemixConfig
) -> tuple[list[list[_Unit]] | None, str]:
    """Run the optional MILP packer and re-check its answer in plain Python.

    ``k`` (the never-donate bound) is the number of fully-OK containers that are
    already within [floor, cap] after the plain earliest-first fill -- the same
    ``_fill`` step the greedy starts from. Those are exactly the containers the
    greedy repair locks: it never takes from them, and it never adds a late unit
    to them either (only containers outside the bounds receive units). An
    all-OK box UNDER the floor is not protected by the greedy (it may receive a
    late unit), so it is not counted here either."""
    from app.core import milp_packer  # optional dependency, imported only when asked for

    k = sum(
        1
        for box in _fill(units, n, cap, floor, 0.0)
        if _fully_ok(box) and floor - EPS <= _cbm(box) <= cap + EPS
    )
    outcome = milp_packer.pack_milp(units, n, cap, floor, k, config.milp_time_limit_seconds)
    if outcome.status == "unavailable":
        raise StageError(
            stage=STAGE,
            message="RemixConfig.packer is 'milp' but PuLP/CBC is not installed.",
            expected="pip install 'pulp[cbc]' (PuLP plus the CBC solver), or packer='greedy'",
            received="PuLP or its CBC solver is not available",
        )
    if outcome.boxes is None:
        label = {"infeasible": "proven infeasible", "timeout": f"time limit of {config.milp_time_limit_seconds:g}s hit without a solution"}
        return None, label.get(outcome.status, outcome.status)
    boxes = outcome.boxes
    placed = sum(len(b) for b in boxes)
    fully_ok = sum(1 for b in boxes if _fully_ok(b))
    if placed != len(units) or _violation(boxes, floor, cap) > EPS or fully_ok < k:
        return None, f"solver answer rejected by the plain-Python check ({outcome.status})"
    return boxes, outcome.status


def remix_order(
    order_number: str,
    analyses: list[OrderAnalysis],
    config: RemixConfig | None = None,
    scoring_config: ScoringConfig | None = None,
    as_of_date: date | None = None,
    analysis_config: AnalysisConfig | None = None,
) -> RemixResult:
    config = config or RemixConfig()
    tolerance_days = (analysis_config or AnalysisConfig()).status_tolerance_days
    cap = config.container_capacity_cbm
    floor = config.min_acceptable_utilization * cap
    target, pools = find_remix_pool(order_number, analyses, config)
    coefficient = score_order(
        target, scoring_config or ScoringConfig(), as_of_date or date.today()
    ).coefficient
    solver_status: dict[str, str] = {}

    def result(
        containers: list[ShipmentContainer],
        failures: list[GroupFailure],
        reason: str | None,
        message: str | None,
    ) -> RemixResult:
        return RemixResult(
            target_order_number=order_number,
            customer_id=target.customer_id,
            target_order_coefficient=coefficient,
            category_groups=sorted(pools),
            category_group_of={
                s.product_category: group_of(s.product_category, config) for s in target.segments
            },
            containers=containers,
            group_failures=failures,
            no_proposal_reason=reason,
            message=message,
            solver_status=dict(solver_status),
        )

    if not any(pools.values()):
        why = (
            "this order is already OK, so there is nothing to remix"
            if target.mix_status != "NOK"
            else "no units were found in the customer's NOK orders for this category group"
        )
        return result([], [], "no_candidates", f"No proposal: {why}.")

    containers: list[ShipmentContainer] = []
    failures: list[GroupFailure] = []
    for group in sorted(pools):
        units = _expand(pools[group], tolerance_days)
        if not units:
            continue
        total = _cbm(units)
        n = max(1, math.ceil(total / cap - EPS))
        if total + EPS < floor * n:
            failures.append(
                GroupFailure(
                    group, "insufficient_volume",
                    f"{total:.2f} CBM cannot fill {n} container(s) to "
                    f"{config.min_acceptable_utilization:.1%} (needs {floor * n:.2f} CBM); "
                    "containers cannot be created.",
                    total, n,
                )
            )
            continue
        if config.packer == "milp":
            boxes, milp_note = _pack_with_milp(units, n, cap, floor, config)
            solver_status[group] = milp_note
            if boxes is None:
                failures.append(
                    GroupFailure(
                        group, "cannot_balance",
                        f"{total:.2f} CBM is enough in total, but the MILP packer found no distribution "
                        f"keeping all {n} containers within {config.min_acceptable_utilization:.1%}-100% "
                        f"without dissolving a fully-OK container ({milp_note}).",
                        total, n,
                    )
                )
                continue
            for i, box in enumerate(boxes, start=1):
                ref = f"RMX-{order_number}-{group}-{i}".replace(" ", "")
                containers.append(_build_container(ref, group, i, box, cap))
            continue
        # Try a growing reserve for later containers so integer-sized units can
        # still reach the floor everywhere; the first arrangement that works wins.
        for reserve in (0.0, 0.5, 1.0, 2.0, 3.0):
            boxes = _fill(units, n, cap, floor, reserve)
            _rebalance(boxes, floor, cap, config.max_balancing_delay_days)
            if _violation(boxes, floor, cap) <= EPS:
                break
        if _violation(boxes, floor, cap) > EPS:
            failures.append(
                GroupFailure(
                    group, "cannot_balance",
                    f"{total:.2f} CBM is enough in total, but no distribution keeps all "
                    f"{n} containers within {config.min_acceptable_utilization:.1%}-100% "
                    "without borrowing from a fully-OK container.",
                    total, n,
                )
            )
            continue
        for i, box in enumerate(boxes, start=1):
            ref = f"RMX-{order_number}-{group}-{i}".replace(" ", "")
            containers.append(_build_container(ref, group, i, box, cap))

    if containers:
        msg = None
        if failures:
            msg = "Some category groups could not be built: " + "; ".join(
                f"{f.group}: {f.reason}" for f in failures
            )
        return result(containers, failures, None, msg)
    if not failures:  # every pooled segment had zero units: nothing to pack
        return result([], [], "no_candidates", "No proposal: the pool holds no units.")
    reasons = {f.reason for f in failures}
    reason = reasons.pop() if len(reasons) == 1 else "mixed"
    return result([], failures, reason, "No proposal: " + " ".join(f.message for f in failures))
