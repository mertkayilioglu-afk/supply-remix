"""Write the synthetic order book as an API-ready JSON payload (data/sample_order_book.json)."""
from __future__ import annotations

import dataclasses
import json
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.config.generator_config import GeneratorConfig
from app.data.generator import SyntheticDataGenerator
from app.data.models import OrderLine


def ser(line: OrderLine) -> dict[str, Any]:
    d = dataclasses.asdict(line)
    d["order_date"] = str(d["order_date"])
    d["requested_delivery_date"] = str(d["requested_delivery_date"])
    d["production_feedback"] = [
        {**f, "available_date": str(f["available_date"]) if f["available_date"] else None, "status": f["status"].value}
        for f in d["production_feedback"]
    ]
    return d


if __name__ == "__main__":
    lines = SyntheticDataGenerator(GeneratorConfig()).generate_orders()
    out = pathlib.Path(__file__).resolve().parents[1] / "data" / "sample_order_book.json"
    out.write_text(json.dumps({"order_lines": [ser(l) for l in lines]}))
    print(f"{len(lines)} lines -> {out}")
