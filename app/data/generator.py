"""Synthetic order-book generator.

Produces a reproducible, realistic-looking set of customer orders with
product master data and production feedback (quantity split across
availability dates). This stands in for what would, in a real supply
planning role, come from an ERP order extract joined with a factory
production feedback report.

Nothing here is company data — all values are randomly generated from
a seeded RNG per `GeneratorConfig`.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import pandas as pd

from app.config.generator_config import GeneratorConfig
from app.data.models import (
    AvailabilityStatus,
    Customer,
    OrderLine,
    Product,
    ProductionFeedbackEntry,
)

_CUSTOMER_NAME_PARTS = (
    ("Nord", "Baltic", "Alpine", "Meridian", "Harbor", "Summit", "Cedar", "River",
     "Atlas", "Delta", "Union", "Pioneer", "Coastal", "Highland", "Vista"),
    ("Retail", "Home Goods", "Living", "Trading Co.", "Distribution", "Mercantile",
     "Supply", "Group", "Partners", "Direct"),
)


class SyntheticDataGenerator:
    """Generates a product master, customer master, and order book.

    Usage:
        gen = SyntheticDataGenerator(GeneratorConfig())
        orders = gen.generate_orders()
        df = orders_to_dataframe(orders)
    """

    def __init__(self, config: GeneratorConfig | None = None) -> None:
        self.config = config or GeneratorConfig()
        self._rng = random.Random(self.config.seed)
        self.products: list[Product] = self._generate_products()
        self.customers: list[Customer] = self._generate_customers()

    # ------------------------------------------------------------------
    # Master data
    # ------------------------------------------------------------------
    def _generate_products(self) -> list[Product]:
        cfg = self.config
        abc_choices = list(cfg.abc_class_weights.keys())
        abc_weights = list(cfg.abc_class_weights.values())

        products: list[Product] = []
        for i in range(cfg.n_products):
            category = self._rng.choice(cfg.categories)
            baseline = cfg.category_seasonality_baseline[category]
            # +/-15% jitter around the category baseline so products in
            # the same category aren't all identical.
            jitter = self._rng.uniform(-0.15, 0.15)
            seasonality = round(max(0.1, baseline + jitter), 2)
            bi_fs_choices = list(cfg.bi_fs_weights.keys())
            bi_fs_weights = list(cfg.bi_fs_weights.values())
            country = self._rng.choice(cfg.supplier_countries)
            plant_number = self._rng.randint(1, cfg.plants_per_country)
            plant = cfg.plant_code_format.format(country=country, number=plant_number)

            cbm_min, cbm_max = cfg.category_cbm_range[category]
            price_min, price_max = cfg.category_unit_price_range[category]
            unit_price = round(self._rng.uniform(price_min, price_max), 2)

            products.append(
                Product(
                    sku=f"SKU-{i + 1:04d}",
                    product_category=category,
                    abc_class=self._rng.choices(abc_choices, weights=abc_weights, k=1)[0],
                    cbm_per_unit=round(self._rng.uniform(cbm_min, cbm_max), 4),
                    seasonality_factor=seasonality,
                    supplier_country=country,
                    bi_fs=self._rng.choices(bi_fs_choices, weights=bi_fs_weights, k=1)[0],
                    plant=plant,
                    unit_price=unit_price,
                )
            )
        return products

    def _generate_customers(self) -> list[Customer]:
        regions = ("Region-North", "Region-South", "Region-East", "Region-West", "Region-Central")
        names_used: set[str] = set()
        customers: list[Customer] = []
        for i in range(self.config.n_customers):
            while True:
                name = (
                    f"{self._rng.choice(_CUSTOMER_NAME_PARTS[0])} "
                    f"{self._rng.choice(_CUSTOMER_NAME_PARTS[1])}"
                )
                if name not in names_used:
                    names_used.add(name)
                    break
            customers.append(
                Customer(
                    customer_id=f"CUST-{i + 1:03d}",
                    customer_name=name,
                    region=self._rng.choice(regions),
                    preferred_categories=tuple(
                        self._rng.sample(
                            list(self.config.categories),
                            k=self.config.n_preferred_categories_per_customer,
                        )
                    ),
                )
            )
        return customers

    # ------------------------------------------------------------------
    # Order book
    # ------------------------------------------------------------------
    def generate_orders(self) -> list[OrderLine]:
        cfg = self.config
        order_lines: list[OrderLine] = []

        for order_idx in range(cfg.n_orders):
            order_number = f"SO-{100000 + order_idx}"
            customer = self._rng.choice(self.customers)

            requested_delivery_date = cfg.simulated_today + timedelta(
                days=self._rng.randint(
                    cfg.delivery_offset_min_days, cfg.delivery_offset_max_days
                )
            )
            # Order was placed some weeks before its requested delivery date.
            order_date = requested_delivery_date - timedelta(
                days=self._rng.randint(20, 90)
            )

            n_lines = self._rng.randint(cfg.min_lines_per_order, cfg.max_lines_per_order)
            # Sample distinct products for this order (an order shouldn't
            # repeat the same SKU across lines).
            # Real customers order repeatedly within a narrow range, which is
            # what makes same-customer, same-category remix pools form.
            line_products = []
            tries = 0
            while len(line_products) < n_lines and tries < 200:
                tries += 1
                if self._rng.random() < cfg.preferred_category_bias:
                    cands = [p for p in self.products
                             if p.product_category in customer.preferred_categories]
                else:
                    cands = self.products
                pick = self._rng.choice(cands or self.products)
                if pick not in line_products:
                    line_products.append(pick)

            for line_no, product in enumerate(line_products, start=1):
                quantity = self._rng.randint(cfg.quantity_min, cfg.quantity_max)
                order_lines.append(
                    OrderLine(
                        order_number=order_number,
                        order_line_id=f"{order_number}-L{line_no}",
                        customer_id=customer.customer_id,
                        customer_name=customer.customer_name,
                        sku=product.sku,
                        product_category=product.product_category,
                        abc_class=product.abc_class,
                        seasonality_factor=product.seasonality_factor,
                        supplier_country=product.supplier_country,
                        bi_fs=product.bi_fs,
                        plant=product.plant,
                        unit_price=product.unit_price,
                        quantity=quantity,
                        cbm_per_unit=product.cbm_per_unit,
                        order_date=order_date,
                        requested_delivery_date=requested_delivery_date,
                        production_feedback=self._generate_production_feedback(
                            quantity, requested_delivery_date
                        ),
                    )
                )

        return order_lines

    def _generate_production_feedback(
        self, quantity: int, requested_delivery_date: date
    ) -> list[ProductionFeedbackEntry]:
        """Split a line's total quantity across 1-3 availability dates.

        This simulates a factory production feedback report: the full
        quantity may come complete on one date, or arrive in staggered
        batches, some of which may land after the requested delivery
        date (the delay that blocks the rest of the order).
        """
        cfg = self.config
        n_chunks = self._rng.choices(
            (1, 2, 3),
            weights=(cfg.prob_single_date, cfg.prob_two_dates, cfg.prob_three_dates),
            k=1,
        )[0]
        n_chunks = min(n_chunks, quantity)  # can't split below 1 unit per chunk

        # Split quantity into n_chunks positive integer parts.
        chunk_qtys = self._split_quantity(quantity, n_chunks)

        entries: list[ProductionFeedbackEntry] = []
        for i, qty in enumerate(chunk_qtys):
            status = self._rng.choices(
                (
                    AvailabilityStatus.CONFIRMED,
                    AvailabilityStatus.POTENTIAL,
                    AvailabilityStatus.UNPLANNED,
                ),
                weights=(
                    cfg.prob_status_confirmed,
                    cfg.prob_status_potential,
                    cfg.prob_status_unplanned,
                ),
                k=1,
            )[0]

            if status is AvailabilityStatus.UNPLANNED:
                entries.append(
                    ProductionFeedbackEntry(
                        available_qty=qty, available_date=None, status=status
                    )
                )
                continue

            # Every line has exactly one "risk" chunk that can land late:
            # the only chunk on a single-date line, or the last chunk on
            # a split line. All other chunks are always on-time.
            is_risk_chunk = i == n_chunks - 1
            is_late = is_risk_chunk and self._rng.random() < cfg.prob_delay_chunk

            if is_late:
                offset = self._rng.randint(cfg.late_offset_min_days, cfg.late_offset_max_days)
            else:
                offset = self._rng.randint(
                    cfg.on_time_offset_min_days, cfg.on_time_offset_max_days
                )

            available_date = requested_delivery_date + timedelta(days=offset)
            entries.append(
                ProductionFeedbackEntry(
                    available_qty=qty, available_date=available_date, status=status
                )
            )

        # Keep feedback entries chronologically ordered for readability.
        # UNPLANNED entries (no date) sort last, since "no committed date"
        # should never be treated as "earliest available".
        entries.sort(
            key=lambda e: (e.available_date is None, e.available_date or date.max)
        )
        return entries

    def _split_quantity(self, quantity: int, n_chunks: int) -> list[int]:
        """Split `quantity` into `n_chunks` positive integers summing to it."""
        if n_chunks == 1:
            return [quantity]
        # Choose n_chunks-1 cut points in [1, quantity-1] without replacement.
        cut_points = sorted(
            self._rng.sample(range(1, quantity), n_chunks - 1)
        )
        bounds = [0, *cut_points, quantity]
        return [bounds[i + 1] - bounds[i] for i in range(n_chunks)]


def orders_to_dataframe(order_lines: list[OrderLine]) -> pd.DataFrame:
    """Flatten order lines (with nested production feedback) into a
    single pandas DataFrame — one row per order line, production
    feedback kept as a list-of-dicts column.

    This mirrors how the data would realistically arrive (one row per
    order line in an export, with a feedback sub-table per line) and is
    the direct input to the Step 2 availability splitter.
    """
    rows = []
    for line in order_lines:
        rows.append(
            {
                "order_number": line.order_number,
                "order_line_id": line.order_line_id,
                "customer_id": line.customer_id,
                "customer_name": line.customer_name,
                "sku": line.sku,
                "product_category": line.product_category,
                "abc_class": line.abc_class,
                "bi_fs": line.bi_fs,
                "plant": line.plant,
                "unit_price": line.unit_price,
                "seasonality_factor": line.seasonality_factor,
                "supplier_country": line.supplier_country,
                "quantity": line.quantity,
                "cbm_per_unit": line.cbm_per_unit,
                "order_date": line.order_date,
                "requested_delivery_date": line.requested_delivery_date,
                "production_feedback": [
                    {
                        "available_qty": e.available_qty,
                        "available_date": e.available_date,
                        "status": e.status.value,
                    }
                    for e in line.production_feedback
                ],
            }
        )
    return pd.DataFrame(rows)


if __name__ == "__main__":
    generator = SyntheticDataGenerator(GeneratorConfig())
    lines = generator.generate_orders()
    df = orders_to_dataframe(lines)

    print(f"Generated {len(lines)} order lines across {df['order_number'].nunique()} orders "
          f"and {df['customer_id'].nunique()} customers.\n")
    print(df.head(10).to_string(index=False))

    df.to_json("data/synthetic_orders.json", orient="records", date_format="iso", indent=2)
    print("\nSaved full dataset to data/synthetic_orders.json")
