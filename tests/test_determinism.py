"""Float totals must not depend on the Python version.

Python 3.12 changed the built-in sum() for floats to compensated (Neumaier) summation, so the
same float additions gave different last digits on 3.11 and 3.12+. That made the committed static
dashboard differ from a fresh 3.11 build (CI failed on 3.11 only) and could, in a borderline case,
change a packing decision or the ranking order. Float totals now use math.fsum, which returns
the correctly rounded exact sum on every version.

Ten amounts of 0.1 are the classic case: left-to-right addition (3.11's sum()) gives
0.9999999999999999; the exact sum rounds to 1.0.
"""

from __future__ import annotations

import math

from app.core.remix import _cbm
from app.core.splitter import analyze_orders
from tests.test_remix import EARLY, _dated_units
from tests.test_splitter import REQUESTED, confirmed, make_line

TENTHS = [0.1] * 10


def test_the_example_really_differs_between_naive_and_exact_summation() -> None:
    naive = 0.0
    for x in TENTHS:  # what sum() did on Python 3.11
        naive += x
    assert naive != 1.0 and math.fsum(TENTHS) == 1.0


def test_order_net_amount_is_the_exact_sum_of_its_lines() -> None:
    lines = [
        make_line("SO-1", f"SO-1-L{i}", 1, REQUESTED, [confirmed(1, EARLY)], unit_price=0.1, sku=f"SKU-{i}")
        for i in range(10)
    ]
    [order] = analyze_orders(lines)
    assert order.total_amount == 1.0


def test_container_volume_is_the_exact_sum_of_its_units() -> None:
    units = _dated_units(10, EARLY, ok=True, tag="A", cbm=0.1)
    assert _cbm(units) == 1.0
