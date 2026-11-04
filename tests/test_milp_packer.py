"""Optional MILP packer: same hard rules as greedy, behind a flag, with a time limit."""

from __future__ import annotations

import pytest

from app.config.remix_config import RemixConfig
from app.core import milp_packer
from app.core.errors import StageError
from app.core.milp_packer import MilpOutcome, pack_milp
from app.core.remix import _violation, remix_order
from app.core.splitter import analyze_orders
from tests.test_remix import AS_OF, EARLY, LATE, MID, _units, line
from tests.test_splitter import REQUESTED, confirmed

pytest.importorskip("pulp")
if not milp_packer.pulp_available():
    pytest.skip("CBC solver not available (pip install 'pulp[cbc]')", allow_module_level=True)

MILP = RemixConfig(packer="milp", milp_time_limit_seconds=20)


def test_greedy_is_the_default() -> None:
    assert RemixConfig().packer == "greedy"


def test_milp_obeys_bounds_and_places_every_unit() -> None:
    lines = [line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])]
    analyses = analyze_orders(lines)
    g = remix_order("SO-1", analyses, RemixConfig(), as_of_date=AS_OF)
    m = remix_order("SO-1", analyses, MILP, as_of_date=AS_OF)
    assert len(m.containers) == len(g.containers) == 3  # same rule-defined container count
    assert sum(c.total_quantity for c in m.containers) == 89
    assert all(0.945 - 1e-9 <= c.utilization <= 1 + 1e-9 for c in m.containers)
    assert m.solver_status == {"Dishwashers": "optimal"}
    assert g.solver_status == {}


def test_never_donate_constraint_binds() -> None:
    """30 OK units of 1.0 and 54 late units of 0.5 = 57 CBM in 2 containers (floor 28.35).
    A fully-OK container can only hold 29 or 30 CBM, leaving <= 28 for the other: infeasible.
    Without the constraint (k=0) a mixed split exists. So the constraint really bites."""
    units = _units(30, ok=True, tag="A") + _units(54, ok=False, tag="B", cbm=0.5)
    protected = pack_milp(units, 2, 30.0, 28.35, min_fully_ok=1, time_limit_seconds=20)
    free = pack_milp(units, 2, 30.0, 28.35, min_fully_ok=0, time_limit_seconds=20)
    assert protected.status == "infeasible" and protected.boxes is None
    assert free.status == "optimal" and free.boxes is not None
    assert _violation(free.boxes, 28.35, 30.0) == 0.0


def test_ok_box_under_the_floor_is_not_protected_like_in_greedy() -> None:
    """The plain fill gives an all-OK box of 28 CBM (under the floor). Greedy may put a late
    unit into it, so it is not counted as protected; both packers build the same pool."""
    lines = [line("SO-1", "SO-1-L1", 30, REQUESTED, [confirmed(30, EARLY)]),
             line("SO-1", "SO-1-L2", 54, REQUESTED, [confirmed(54, LATE)], cbm_per_unit=0.5, sku="S2")]
    analyses = analyze_orders(lines)
    g = remix_order("SO-1", analyses, RemixConfig(), as_of_date=AS_OF)
    m = remix_order("SO-1", analyses, MILP, as_of_date=AS_OF)
    assert len(g.containers) == len(m.containers) == 2


def test_insufficient_volume_is_decided_before_any_solver_runs() -> None:
    lines = [line("SO-1", "SO-1-L1", 20, REQUESTED, [confirmed(10, EARLY), confirmed(10, LATE)])]
    m = remix_order("SO-1", analyze_orders(lines), MILP, as_of_date=AS_OF)
    assert m.no_proposal_reason == "insufficient_volume" and m.solver_status == {}


def test_time_limit_is_passed_to_cbc(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    real = milp_packer.cbc_solver

    def spy(time_limit_seconds: float) -> object:
        seen.append(time_limit_seconds)
        solver = real(time_limit_seconds)
        assert solver.timeLimit == time_limit_seconds  # reaches the CBC command itself
        return solver

    monkeypatch.setattr(milp_packer, "cbc_solver", spy)
    lines = [line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])]
    remix_order("SO-1", analyze_orders(lines), RemixConfig(packer="milp", milp_time_limit_seconds=3.5), as_of_date=AS_OF)
    assert seen and set(seen) <= {1.0, 3.5} and 3.5 in seen  # 1.0 = availability probe


def test_timeout_fails_visibly_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(milp_packer, "pack_milp", lambda *a, **k: MilpOutcome("timeout", None, 3.5))
    lines = [line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])]
    m = remix_order("SO-1", analyze_orders(lines), RemixConfig(packer="milp", milp_time_limit_seconds=3.5), as_of_date=AS_OF)
    assert m.containers == [] and m.no_proposal_reason == "cannot_balance"
    assert "time limit of 3.5s" in m.group_failures[0].message


def test_solver_answer_is_rechecked_not_trusted(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(units, n, cap, floor, k, t):  # type: ignore[no-untyped-def]
        return MilpOutcome("optimal", [list(units)] + [[] for _ in range(n - 1)], 0.0)  # all in one box

    monkeypatch.setattr(milp_packer, "pack_milp", broken)
    lines = [line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])]
    m = remix_order("SO-1", analyze_orders(lines), MILP, as_of_date=AS_OF)
    assert m.containers == []
    assert "rejected by the plain-Python check" in m.group_failures[0].message


def test_missing_pulp_fails_visibly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(milp_packer, "pulp_available", lambda: False)
    lines = [line("SO-1", "SO-1-L1", 89, REQUESTED, [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])]
    with pytest.raises(StageError) as exc:
        remix_order("SO-1", analyze_orders(lines), MILP, as_of_date=AS_OF)
    assert "pulp[cbc]" in (exc.value.expected or "")
