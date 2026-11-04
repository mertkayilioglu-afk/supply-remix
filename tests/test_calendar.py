"""Working-day calendar: off by default; when on, availability dates on non-working
days move to the next working day before any status is computed."""

from __future__ import annotations

from datetime import date

import pytest

from app.config.analysis_config import AnalysisConfig
from app.config.calendar_config import CalendarConfig
from app.config.remix_config import RemixConfig
from app.core.calendar import next_working_day
from app.core.errors import StageError
from app.core.remix import remix_order
from app.core.splitter import analyze_orders
from tests.test_splitter import confirmed, make_line, unplanned

FRI, SAT, SUN, MON, TUE = (date(2026, 2, d) for d in (6, 7, 8, 9, 10))
ON = CalendarConfig(enabled=True)


class TestNextWorkingDay:
    def test_off_by_default(self) -> None:
        assert CalendarConfig().enabled is False
        assert AnalysisConfig().calendar.enabled is False
        assert next_working_day(SAT, CalendarConfig()) == SAT

    def test_weekend_moves_to_monday(self) -> None:
        assert next_working_day(SAT, ON) == MON
        assert next_working_day(SUN, ON) == MON

    def test_working_day_is_unchanged(self) -> None:
        assert next_working_day(FRI, ON) == FRI

    def test_holiday_after_weekend_moves_further(self) -> None:
        cal = CalendarConfig(enabled=True, holidays=frozenset({MON}))
        assert next_working_day(SAT, cal) == TUE
        assert next_working_day(MON, cal) == TUE

    def test_unplanned_stays_none(self) -> None:
        assert next_working_day(None, ON) is None

    def test_custom_working_week(self) -> None:
        sun_to_thu = CalendarConfig(enabled=True, working_weekdays=frozenset({6, 0, 1, 2, 3}))
        assert next_working_day(FRI, sun_to_thu) == SUN
        assert next_working_day(SUN, sun_to_thu) == SUN

    def test_no_working_day_within_bound_fails_visibly(self) -> None:
        shutdown = CalendarConfig(
            enabled=True, holidays=frozenset(date(2026, 2, d) for d in range(9, 28)), max_shift_days=10
        )
        with pytest.raises(StageError) as exc:
            next_working_day(SAT, shutdown)
        assert exc.value.stage.endswith("working-day calendar")


class TestInvalidCalendarFailsVisibly:
    @pytest.mark.parametrize("weekdays", [frozenset(), frozenset({7})])
    def test_bad_working_weekdays(self, weekdays: frozenset[int]) -> None:
        cfg = AnalysisConfig(calendar=CalendarConfig(enabled=True, working_weekdays=weekdays))
        line = make_line("SO-1", "SO-1-L1", 10, FRI, [confirmed(10, SAT)])
        with pytest.raises(StageError):
            analyze_orders([line], config=cfg)

    def test_bad_calendar_is_ignored_while_disabled(self) -> None:
        cfg = AnalysisConfig(calendar=CalendarConfig(enabled=False, working_weekdays=frozenset()))
        line = make_line("SO-1", "SO-1-L1", 10, FRI, [confirmed(10, SAT)])
        [order] = analyze_orders([line], config=cfg)
        assert order.availability_of_mix == SAT


class TestSplitterUsesShiftedDates:
    def test_shift_is_recorded_and_requested_date_is_not_shifted(self) -> None:
        line = make_line("SO-1", "SO-1-L1", 10, SAT, [confirmed(6, FRI), confirmed(4, SUN)])
        [order] = analyze_orders([line], config=AnalysisConfig(calendar=ON))
        assert order.requested_delivery_date == SAT  # customer's date, untouched
        assert order.availability_of_mix == MON
        fri_seg, sun_seg = order.segments
        assert (fri_seg.available_date, fri_seg.calendar_shifted_from) == (FRI, None)
        assert (sun_seg.available_date, sun_seg.calendar_shifted_from) == (MON, SUN)

    def test_shift_can_turn_an_ok_order_nok(self) -> None:
        """Default 21-day window. Requested Sun 8 Feb; available Sat 28 Feb is a 20-day
        delay (OK). With the calendar on it becomes Mon 2 Mar, a 22-day delay (NOK)."""
        requested = SUN  # 2026-02-08
        saturday = date(2026, 2, 28)
        assert (saturday - requested).days == 20
        line = make_line("SO-1", "SO-1-L1", 10, requested, [confirmed(10, saturday)])
        assert analyze_orders([line])[0].mix_status == "OK"
        [shifted] = analyze_orders([line], config=AnalysisConfig(calendar=ON))
        assert shifted.availability_of_mix == date(2026, 3, 2)
        assert shifted.mix_status == "NOK"

    def test_unplanned_is_untouched(self) -> None:
        line = make_line("SO-1", "SO-1-L1", 10, FRI, [confirmed(5, SAT), unplanned(5)])
        [order] = analyze_orders([line], config=AnalysisConfig(calendar=ON))
        assert order.availability_of_mix is None
        assert order.segments[1].available_date is None
        assert order.segments[1].calendar_shifted_from is None

    def test_off_by_default_changes_nothing(self) -> None:
        line = make_line("SO-1", "SO-1-L1", 10, FRI, [confirmed(10, SAT)])
        [order] = analyze_orders([line])
        assert order.segments[0].available_date == SAT
        assert order.segments[0].calendar_shifted_from is None


def test_container_lines_carry_the_shifted_date_and_the_original() -> None:
    late = date(2026, 5, 2)  # Saturday, far beyond tolerance -> NOK order
    line = make_line("SO-1", "SO-1-L1", 30, FRI, [confirmed(29, FRI), confirmed(1, late)], cbm_per_unit=1.0)
    cfg = AnalysisConfig(calendar=ON)
    result = remix_order("SO-1", analyze_orders([line], config=cfg), RemixConfig(), as_of_date=FRI, analysis_config=cfg)
    [container] = result.containers
    shifted = [l for l in container.lines if l.calendar_shifted_from is not None]
    assert [(l.available_date, l.calendar_shifted_from) for l in shifted] == [(date(2026, 5, 4), late)]
    assert container.availability_date == date(2026, 5, 4)
