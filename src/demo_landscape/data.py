"""Данные и схемы учебных систем demo-landscape (specs/60-demo-landscape.md)."""

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


FIXTURE_PATH = Path(__file__).with_name("fixture.json")

SCHEMAS: dict[str, dict[str, Any]] = {
    "crm": {
        "system": "crm",
        "objects": [
            {
                "name": "order",
                "key": "order_id",
                "path": "/orders",
                "fields": ["order_id", "client", "contact", "items", "deadline", "status", "manager", "quote"],
                "sensitivity": {"client": "pii", "contact": "pii"},
                "links": [{"field": "items[].sku", "to": "stock.item.sku"}],
            }
        ],
    },
    "stock": {
        "system": "stock",
        "objects": [
            {
                "name": "item",
                "key": "sku",
                "path": "/items",
                "fields": ["sku", "title", "qty_available", "lead_time_days", "analogs"],
                "sensitivity": {},
                "links": [
                    {"field": "analogs[]", "to": "stock.item.sku"},
                    {"field": "sku", "to": "pricing.price.sku"},
                ],
            }
        ],
    },
    "pricing": {
        "system": "pricing",
        "objects": [
            {
                "name": "price",
                "key": "sku",
                "path": "/prices",
                "fields": ["sku", "cost", "list_price", "min_margin"],
                "sensitivity": {},
                "links": [{"field": "sku", "to": "stock.item.sku"}],
            }
        ],
    },
}
SYSTEMS = tuple(SCHEMAS)


@dataclass(frozen=True)
class Landscape:
    today: str
    orders: dict[str, dict[str, Any]]
    stock: dict[str, dict[str, Any]]
    prices: dict[str, dict[str, Any]]

    @classmethod
    def load(cls, path: Path = FIXTURE_PATH) -> "Landscape":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            today=raw["today"],
            orders={o["order_id"]: o for o in raw["crm"]["orders"]},
            stock={i["sku"]: i for i in raw["stock"]["items"]},
            prices={p["sku"]: p for p in raw["pricing"]["prices"]},
        )

    def orders_of(self, manager: str, status: str | None = None) -> list[dict[str, Any]]:
        return [
            order
            for order in self.orders.values()
            if order["manager"] == manager and (status is None or order["status"] == status)
        ]
