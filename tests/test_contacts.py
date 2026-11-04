"""Tests for the customer contact directory."""

from __future__ import annotations

import pytest

from app.data.models import Customer
from app.presentation.contacts import (
    Contact,
    generate_synthetic_contact_directory,
    resolve_contact,
)


class TestResolveContact:
    def test_resolves_known_customer(self) -> None:
        directory = {"CUST-001": Contact("CUST-001", "Jamie Rossi", "jamie@example.com")}
        contact = resolve_contact(directory, "CUST-001")
        assert contact.email == "jamie@example.com"

    def test_fails_visibly_for_unknown_customer(self) -> None:
        directory = {"CUST-001": Contact("CUST-001", "Jamie Rossi", "jamie@example.com")}
        with pytest.raises(ValueError, match="No contact on file"):
            resolve_contact(directory, "CUST-999")

    def test_error_names_the_missing_customer(self) -> None:
        directory: dict = {}
        try:
            resolve_contact(directory, "CUST-404")
            assert False, "expected StageError"
        except ValueError as exc:
            assert "CUST-404" in str(exc)
            assert exc.stage == "Presentation — customer contact lookup"  # type: ignore[attr-defined]


class TestSyntheticDirectory:
    def test_full_coverage_gives_every_customer_a_contact(self) -> None:
        customers = [
            Customer("CUST-001", "Alpha Co", "Region-West", preferred_categories=("Lighting",)),
            Customer("CUST-002", "Beta Co", "Region-North", preferred_categories=("Cookware",)),
        ]
        directory = generate_synthetic_contact_directory(customers, coverage=1.0)
        assert set(directory.keys()) == {"CUST-001", "CUST-002"}
        for contact in directory.values():
            assert "@" in contact.email

    def test_partial_coverage_deliberately_leaves_gaps(self) -> None:
        customers = [
            Customer(f"CUST-{i:03d}", f"Customer {i}", "Region-West", preferred_categories=("Lighting",))
            for i in range(50)
        ]
        directory = generate_synthetic_contact_directory(customers, coverage=0.5, seed=1)
        assert 0 < len(directory) < len(customers)

    def test_deterministic_for_a_given_seed(self) -> None:
        customers = [
            Customer("CUST-001", "Alpha Co", "Region-West", preferred_categories=("Lighting",))
        ]
        d1 = generate_synthetic_contact_directory(customers, seed=7)
        d2 = generate_synthetic_contact_directory(customers, seed=7)
        assert d1 == d2
