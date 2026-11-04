"""Customer contact directory — a simple, standalone lookup mapping
customer number to the contact for that delivery address.

Deliberately separate from the core order/analysis/scoring/remix
pipeline: contact information isn't an analysis input, it's a
communication-layer concern only used when drafting a customer-facing
email. Keeping it out of app/core and app/data avoids threading an
unrelated concern through every dataclass in the actual engine.

This mirrors the original tool's contact lookup sheet (customer ->
contact name and email), which was used to resolve who to address before
a customer email was drafted.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from app.core.errors import StageError
from app.data.models import Customer


@dataclass(frozen=True)
class Contact:
    customer_id: str
    contact_name: str
    email: str


ContactDirectory = dict[str, Contact]


def resolve_contact(directory: ContactDirectory, customer_id: str) -> Contact:
    """Look up the contact for a customer, failing loudly (not with a
    generic KeyError, and never by silently drafting an email to
    nobody) when none is on file — the same StageError treatment as
    every other stage in this pipeline.
    """
    contact = directory.get(customer_id)
    if contact is None:
        raise StageError(
            stage="Presentation — customer contact lookup",
            message=f"No contact on file for customer {customer_id!r}.",
            expected="a registered Contact for this customer_id in the contact directory",
            received="no entry",
        )
    return contact


def generate_synthetic_contact_directory(
    customers: list[Customer], seed: int = 4242, coverage: float = 0.9
) -> ContactDirectory:
    """Build a synthetic contacts directory for the generated customer
    book. `coverage` deliberately leaves some customers WITHOUT a
    contact on file (a realistic gap in real master data), so the
    "fails visibly when missing" path has something real to exercise
    rather than only being reachable in a hand-built test.

    Uses its own seeded RNG, independent of the order-generation RNG,
    so contact assignment doesn't shift order-book generation (or vice
    versa) if either is changed independently.
    """
    rng = random.Random(seed)
    directory: ContactDirectory = {}
    first_names = ("Alex", "Jordan", "Morgan", "Sam", "Taylor", "Casey", "Riley", "Jamie")
    last_names = ("Bennett", "Okafor", "Novak", "Suzuki", "Dubois", "Andersson", "Rossi", "Kim")

    for customer in customers:
        if rng.random() > coverage:
            continue  # deliberately no contact on file for this customer
        contact_name = f"{rng.choice(first_names)} {rng.choice(last_names)}"
        domain = customer.customer_name.lower().replace(" ", "").replace(".", "").replace(",", "")
        email = f"{contact_name.split()[0].lower()}.{contact_name.split()[1].lower()}@{domain}.example.com"
        directory[customer.customer_id] = Contact(
            customer_id=customer.customer_id, contact_name=contact_name, email=email
        )

    return directory
