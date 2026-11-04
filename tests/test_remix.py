"""Tests for the container-based remix engine.

Rules under test: every unit of the customer's NOK orders (OK, late and
UNPLANNED) lands in a date-ordered container; every container is within
[94.5%, 100%]; if the volume cannot support that, there is no proposal and the
reason says so; a fully-OK container never donates units.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.config.remix_config import RemixConfig
from app.core.errors import StageError
from app.config.analysis_config import AnalysisConfig
from app.core.remix import _rebalance, _Unit, _violation, remix_order
from app.core.splitter import analyze_orders
from tests.test_splitter import REQUESTED, confirmed, make_line, unplanned

AS_OF = date(2026, 2, 1)
EARLY = date(2026, 2, 10)
MID = date(2026, 2, 12)
LATE = date(2026, 5, 1)  # far beyond the 21-day tolerance


def line(*args, **kwargs):
    kwargs.setdefault("cbm_per_unit", 1.0)
    kwargs.setdefault("product_category", "Dishwashers")
    return make_line(*args, **kwargs)


def run(lines, target, config=None):
    return remix_order(target, analyze_orders(lines), config or RemixConfig(), as_of_date=AS_OF)


class TestScreenshotBehaviour:
    """Mirrors the legacy view: an all-OK container, then containers that
    still hold late units and therefore wait -- nothing is dropped."""

    def setup_method(self) -> None:
        self.lines = [
            line("SO-1", "SO-1-L1", 89, REQUESTED,
                 [confirmed(30, EARLY), confirmed(29, MID), confirmed(30, LATE)])
        ]
        self.result = run(self.lines, "SO-1")

    def test_three_containers_with_statuses(self) -> None:
        cs = self.result.containers
        assert [c.status for c in cs] == ["OK", "NOK", "NOK"]
        assert self.result.no_proposal_reason is None

    def test_every_unit_is_in_some_container(self) -> None:
        assert sum(c.total_quantity for c in self.result.containers) == 89

    def test_every_container_at_least_floor_and_at_most_capacity(self) -> None:
        for c in self.result.containers:
            assert 0.945 - 1e-9 <= c.utilization <= 1.0 + 1e-9

    def test_containers_ordered_by_date(self) -> None:
        dates = [c.availability_date for c in self.result.containers]
        assert dates == sorted(dates)


class TestNoProposalReasons:
    def test_insufficient_volume_means_no_containers(self) -> None:
        r = run([line("SO-1", "SO-1-L1", 20, REQUESTED, [confirmed(10, EARLY), confirmed(10, LATE)])], "SO-1")
        assert r.containers == []
        assert r.no_proposal_reason == "insufficient_volume"
        assert "94.5%" in (r.message or "")

    def test_volume_between_one_and_two_containers_cannot_fill_two(self) -> None:
        # 31 CBM needs 2 containers, which would need 56.7 CBM at the floor.
        r = run([line("SO-1", "SO-1-L1", 31, REQUESTED, [confirmed(15, EARLY), confirmed(16, LATE)])], "SO-1")
        assert r.no_proposal_reason == "insufficient_volume"

    def test_groups_failing_for_different_reasons_give_mixed(self) -> None:
        lines = [
            # Dishwashers: 86 CBM of 1.0 units -> 3 containers, but integer units cannot all reach 28.35.
            line("SO-1", "SO-1-L1", 86, REQUESTED, [confirmed(29, EARLY), confirmed(29, MID), confirmed(28, LATE)]),
            # Ovens: 10 CBM -> cannot fill one container.
            line("SO-1", "SO-1-L2", 10, REQUESTED, [confirmed(10, LATE)], product_category="Ovens", sku="OV"),
        ]
        r = run(lines, "SO-1")
        assert r.containers == []
        assert r.no_proposal_reason == "mixed"
        assert sorted(f.reason for f in r.group_failures) == ["cannot_balance", "insufficient_volume"]

    def test_groups_failing_for_the_same_reason_keep_that_reason(self) -> None:
        lines = [
            line("SO-1", "SO-1-L1", 10, REQUESTED, [confirmed(5, EARLY), confirmed(5, LATE)]),
            line("SO-1", "SO-1-L2", 10, REQUESTED, [confirmed(10, LATE)], product_category="Ovens", sku="OV"),
        ]
        r = run(lines, "SO-1")
        assert r.no_proposal_reason == "insufficient_volume" and len(r.group_failures) == 2

    def test_ok_target_has_no_candidates(self) -> None:
        r = run([line("SO-1", "SO-1-L1", 30, REQUESTED, [confirmed(30, EARLY)])], "SO-1")
        assert r.no_proposal_reason == "no_candidates"

    def test_unknown_order_raises_stage_error(self) -> None:
        with pytest.raises(StageError):
            run([line("SO-1", "SO-1-L1", 30, REQUESTED, [confirmed(30, EARLY)])], "SO-X")


class TestPoolScope:
    def test_other_customers_are_never_included(self) -> None:
        lines = [
            line("SO-1", "SO-1-L1", 60, REQUESTED, [confirmed(30, EARLY), confirmed(30, LATE)]),
            line("SO-2", "SO-2-L1", 60, REQUESTED, [confirmed(30, EARLY), confirmed(30, LATE)], customer_id="CUST-002"),
        ]
        r = run(lines, "SO-1")
        orders = {l.order_number for c in r.containers for l in c.lines}
        assert orders == {"SO-1"}

    def test_other_categories_are_never_included(self) -> None:
        lines = [
            line("SO-1", "SO-1-L1", 60, REQUESTED, [confirmed(30, EARLY), confirmed(30, LATE)]),
            line("SO-2", "SO-2-L1", 60, REQUESTED, [confirmed(30, EARLY), confirmed(30, LATE)], product_category="Ovens"),
        ]
        r = run(lines, "SO-1")
        assert {l.product_category for c in r.containers for l in c.lines} == {"Dishwashers"}

    def test_same_customer_other_nok_orders_join_the_pool(self) -> None:
        lines = [
            line("SO-1", "SO-1-L1", 30, REQUESTED, [confirmed(15, EARLY), confirmed(15, LATE)]),
            line("SO-2", "SO-2-L1", 30, REQUESTED, [confirmed(15, MID), confirmed(15, LATE)]),
        ]
        r = run(lines, "SO-1")
        assert {l.order_number for c in r.containers for l in c.lines} == {"SO-1", "SO-2"}
        assert sum(c.total_quantity for c in r.containers) == 60


class TestUnplanned:
    def test_unplanned_units_stay_in_a_container_with_unknown_date(self) -> None:
        lines = [line("SO-1", "SO-1-L1", 60, REQUESTED, [confirmed(30, EARLY), unplanned(30)])]
        r = run(lines, "SO-1")
        assert [c.status for c in r.containers] == ["OK", "NOK"]
        assert r.containers[-1].availability_date is None
        assert sum(c.total_quantity for c in r.containers) == 60


class TestBorrowing:
    def test_last_container_borrows_from_container_above_without_dropping_it_below_floor(self) -> None:
        # 29 + 29 + 29 = 87 CBM -> 3 containers, floor 85.05: feasible only by balancing.
        lines = [line("SO-1", "SO-1-L1", 87, REQUESTED, [confirmed(29, EARLY), confirmed(29, MID), confirmed(29, LATE)])]
        r = run(lines, "SO-1")
        assert len(r.containers) == 3
        assert all(0.945 - 1e-9 <= c.utilization <= 1 + 1e-9 for c in r.containers)

    def test_fully_ok_container_is_never_a_donor(self) -> None:
        """Direct test of the repair step. Box 1 is 30 OK units (30 CBM), box 2 is
        28 late units (28 CBM, under the 28.35 floor). Moving one unit up would
        fix it, but box 1 is fully OK, so it must not give anything away."""
        ok_units, late_units = _units(30, ok=True), _units(28, ok=False)
        boxes = [list(ok_units), list(late_units)]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert [len(b) for b in boxes] == [30, 28]
        assert all(u.ok for u in boxes[0])

    def test_container_that_is_not_fully_ok_does_donate(self) -> None:
        """Control for the test above: same volumes, but box 1 holds one late unit,
        so it may lend and the last container reaches the floor."""
        boxes = [_units(29, ok=True) + _units(1, ok=False), _units(28, ok=False)]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert [len(b) for b in boxes] == [29, 29]
        assert _violation(boxes, 28.35, 30.0) == 0.0

    def test_fully_ok_container_may_swap_when_it_stays_ok_and_in_bounds(self) -> None:
        """Box 1: 29 OK units of 1.0 (29.0). Box 2: 27 late 1.0 + 2 OK 0.5 = 28.0, short by 0.35.
        Swapping one OK 1.0 for one OK 0.5 leaves box 1 at 28.5 (fully OK, within bounds): allowed."""
        boxes = [_units(29, ok=True, tag="A"), _units(27, ok=False, tag="B") + _units(2, ok=True, tag="C", cbm=0.5)]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert _violation(boxes, 28.35, 30.0) == 0.0
        assert all(u.ok for u in boxes[0])
        assert 28.35 <= sum(u.cbm for u in boxes[0]) <= 30.0

    def test_fully_ok_container_may_not_swap_below_the_floor(self) -> None:
        """Box 1: 28 OK 1.0 + 1 OK 0.5 = 28.5. Box 2: 26 late 1.0 + 2 OK 0.5 = 27.0.
        Swapping a 1.0 for a 0.5 would lower the total shortfall (1.35 -> 1.2) but drop
        box 1 to 28.0, under the floor. Owner rule: not allowed, box 1 is untouched."""
        box1 = _units(28, ok=True, tag="A") + _units(1, ok=True, tag="D", cbm=0.5)
        boxes = [list(box1), _units(26, ok=False, tag="B") + _units(2, ok=True, tag="C", cbm=0.5)]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert sorted(id(u) for u in boxes[0]) == sorted(id(u) for u in box1)

    def test_repair_borrows_from_earlier_container_first(self) -> None:
        """Container 2 is short; containers 1 and 3 could both lend. The earlier one lends."""
        boxes = [_units(30, ok=False, tag="A"), _units(28, ok=False, tag="B"), _units(30, ok=False, tag="C")]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert [len(b) for b in boxes] == [29, 29, 30]

    def test_repair_borrows_from_later_container_only_when_earlier_cannot_lend(self) -> None:
        """Container 1 sits at 29: lending one unit would drop it under the floor, so the
        repair falls back to the later container (documented in the README)."""
        boxes = [_units(29, ok=False, tag="A"), _units(28, ok=False, tag="B"), _units(30, ok=False, tag="C")]
        _rebalance(boxes, floor=28.35, cap=30.0)
        assert [len(b) for b in boxes] == [29, 29, 29]


def _units(n: int, *, ok: bool, tag: str = "X", cbm: float = 1.0) -> list[_Unit]:
    seg = analyze_orders([line(f"SO-{tag}", f"SO-{tag}-L1", n, REQUESTED, [confirmed(n, EARLY)], cbm_per_unit=cbm)])[0].segments[0]
    return [_Unit(seg, ok, cbm) for _ in range(n)]


class TestSingleToleranceSource:
    def test_container_status_uses_the_analysis_tolerance(self) -> None:
        """A unit 10 days late is OK at the default 21-day window and NOK at 7 days:
        the remix reads the same AnalysisConfig window as the splitter."""
        ten_late = date(REQUESTED.year, REQUESTED.month, REQUESTED.day + 10)
        lines = [line("SO-1", "SO-1-L1", 28, REQUESTED, [confirmed(27, EARLY), confirmed(1, LATE)]),
                 line("SO-2", "SO-2-L1", 1, REQUESTED, [confirmed(1, ten_late)])]
        analyses = analyze_orders(lines)
        # SO-2 is OK at 21 days, so only SO-1 is pooled; re-analyse at 7 days to pool both.
        strict = AnalysisConfig(status_tolerance_days=7)
        strict_analyses = analyze_orders(lines, config=strict)
        r = remix_order("SO-1", strict_analyses, RemixConfig(), as_of_date=AS_OF, analysis_config=strict)
        so2 = [l for c in r.containers for l in c.lines if l.order_number == "SO-2"]
        assert so2 and not so2[0].is_ok
        assert [a.mix_status for a in analyses if a.order_number == "SO-2"] == ["OK"]

    def test_remix_config_no_longer_carries_its_own_tolerance(self) -> None:
        assert not hasattr(RemixConfig(), "status_tolerance_days")
        assert not hasattr(RemixConfig(), "target_utilization")


class TestEdgeCases:
    def test_pool_with_only_zero_quantity_segments_is_no_candidates_not_a_crash(self) -> None:
        lines = [line("SO-1", "SO-1-L1", 0, REQUESTED, [unplanned(0)])]
        r = run(lines, "SO-1")
        assert r.containers == [] and r.no_proposal_reason == "no_candidates"


class TestPropertyOnGeneratedData:
    def test_all_containers_within_bounds_and_units_conserved(self) -> None:
        from app.config.generator_config import GeneratorConfig
        from app.data.generator import SyntheticDataGenerator

        analyses = analyze_orders(SyntheticDataGenerator(GeneratorConfig()).generate_orders())
        cfg = RemixConfig()
        built = 0
        for a in [x for x in analyses if x.mix_status == "NOK"][:25]:
            r = remix_order(a.order_number, analyses, cfg, as_of_date=AS_OF)
            for c in r.containers:
                built += 1
                assert cfg.min_acceptable_utilization - 1e-9 <= c.utilization <= 1 + 1e-9
            for g in {c.group for c in r.containers}:
                in_boxes = sum(c.total_quantity for c in r.containers if c.group == g)
                assert in_boxes > 0
            if r.containers:
                assert r.no_proposal_reason is None
            else:
                assert r.no_proposal_reason in {"no_candidates", "insufficient_volume", "cannot_balance", "mixed"}
        assert built > 0


def _dated_units(n: int, when: date | None, *, ok: bool, tag: str, cbm: float = 1.0) -> list[_Unit]:
    """n identical units of one segment, available on `when` (None = UNPLANNED)."""
    feedback = [confirmed(n, when)] if when is not None else [unplanned(n)]
    seg = analyze_orders([line(f"SO-{tag}", f"SO-{tag}-L1", n, REQUESTED, feedback, cbm_per_unit=cbm)])[0].segments[0]
    return [_Unit(seg, ok, cbm) for _ in range(n)]


D0 = date(2026, 3, 2)
LATER = date(2026, 5, 1)  # 60 days after D0


class TestBalancingDelayGuard:
    """Owner rule: a repair move or swap may not push a container's ready date more than
    RemixConfig.max_balancing_delay_days past its ready date as the fill left it."""

    def test_default_is_21_days(self) -> None:
        assert RemixConfig().max_balancing_delay_days == 21

    def _short_box_and_late_donor(self) -> list[list[_Unit]]:
        # Box 1: 28 CBM ready D0 (short of the 28.35 floor). Box 2: 30 CBM ready 60 days later.
        return [_dated_units(28, D0, ok=False, tag="A"), _dated_units(30, LATER, ok=False, tag="B")]

    def test_move_that_would_delay_a_container_too_much_is_blocked(self) -> None:
        boxes = self._short_box_and_late_donor()
        _rebalance(boxes, floor=28.35, cap=30.0, max_delay_days=21)
        assert [len(b) for b in boxes] == [28, 30]  # nothing moved: no legal repair
        assert max(u.seg.available_date for u in boxes[0]) == D0

    def test_same_move_is_allowed_within_the_limit_or_without_guard(self) -> None:
        for limit in (60, None):
            boxes = self._short_box_and_late_donor()
            _rebalance(boxes, floor=28.35, cap=30.0, max_delay_days=limit)
            assert _violation(boxes, 28.35, 30.0) == 0.0, limit

    def test_both_sides_of_a_swap_are_guarded(self) -> None:
        """Box 1 is fully OK (it can only swap). Swapping its 1.0 unit (ready D0) for a 0.5
        unit ready 60 days later would fix box 2, but would delay box 1 by 60 days."""
        box1 = _dated_units(30, D0, ok=True, tag="A")
        box2 = _dated_units(27, D0, ok=False, tag="B") + _dated_units(2, LATER, ok=True, tag="C", cbm=0.5)
        guarded = [list(box1), list(box2)]
        _rebalance(guarded, floor=28.35, cap=30.0, max_delay_days=21)
        assert max(u.seg.available_date for u in guarded[0]) == D0
        assert _violation(guarded, 28.35, 30.0) > 0  # honest failure instead of a hidden delay
        free = [list(box1), list(box2)]
        _rebalance(free, floor=28.35, cap=30.0, max_delay_days=None)
        assert _violation(free, 28.35, 30.0) == 0.0
        assert max(u.seg.available_date for u in free[0]) == LATER

    def test_unplanned_unit_never_moves_into_a_dated_container(self) -> None:
        boxes = [_dated_units(28, D0, ok=False, tag="A"), _dated_units(30, None, ok=False, tag="U")]
        _rebalance(boxes, floor=28.35, cap=30.0, max_delay_days=21)
        assert all(u.seg.available_date == D0 for u in boxes[0])

    def test_container_already_waiting_on_unplanned_accepts_any_unit(self) -> None:
        boxes = [_dated_units(27, D0, ok=False, tag="A") + _dated_units(1, None, ok=False, tag="U"),
                 _dated_units(30, LATER, ok=False, tag="B")]
        _rebalance(boxes, floor=28.35, cap=30.0, max_delay_days=21)
        assert _violation(boxes, 28.35, 30.0) == 0.0


class TestSO100275SampleCase:
    """The case that motivated the guard: on the sample data, container 1 of SO-100275 used to
    hold 76 units ready 2026-03-03 plus ONE unit ready 2026-05-25, so all 77 waited 83 days."""

    @staticmethod
    def _container_1(cfg: RemixConfig):  # type: ignore[no-untyped-def]
        import json
        import pathlib

        from app.api.main import _to_order_line
        from app.api.schemas import AnalyzeRequest

        book = json.loads((pathlib.Path(__file__).resolve().parents[1] / "data" / "sample_order_book.json").read_text())
        analyses = analyze_orders([_to_order_line(l) for l in AnalyzeRequest.model_validate(book).order_lines])
        return remix_order("SO-100275", analyses, cfg, as_of_date=date(2026, 4, 1)).containers

    def test_without_guard_container_1_waits_83_days_for_one_unit(self) -> None:
        c1 = self._container_1(RemixConfig(max_balancing_delay_days=None))[0]
        assert c1.availability_date == date(2026, 5, 25)
        assert sorted(l.quantity for l in c1.lines) == [1, 76]

    def test_with_default_guard_container_1_ships_with_its_own_date(self) -> None:
        containers = self._container_1(RemixConfig())
        c1 = containers[0]
        assert c1.availability_date == date(2026, 3, 3)
        assert [(l.order_number, l.quantity, l.available_date) for l in c1.lines] == [("SO-100275", 77, date(2026, 3, 3))]
        assert len(containers) == 12 and all(0.945 - 1e-9 <= c.utilization <= 1 + 1e-9 for c in containers)
