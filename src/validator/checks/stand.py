"""Тест-стенд для sandbox и eval адаптеров: ответы demo-landscape без сети (ADR-017).

Стенд повторяет API `specs/60-demo-landscape.md` для токена менеджера `manager-a`.
Совпадение с настоящим сервисом проверяет tests/test_validator_adapter.py.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from src.demo_landscape.data import SCHEMAS, Landscape


STAND_USER = "manager-a"

# объект схемы -> (путь списка, ref-префикс)
COLLECTIONS = {
    "crm": ("order", "/orders"),
    "stock": ("item", "/items"),
    "pricing": ("price", "/prices"),
}
SAMPLE_KEYS = {"crm": "1042", "stock": "A-17", "pricing": "A-17"}


@dataclass(frozen=True)
class Stand:
    routes: dict[str, dict[str, Any]]    # system -> {relative path -> JSON}
    records: dict[str, dict[str, Any]]   # system -> {ref -> record}
    samples: dict[str, tuple[str, ...]]  # system -> refs, которые eval пробует всегда

    def schema(self, system: str) -> dict[str, Any]:
        return self.routes[system]["/schema"]


@lru_cache(maxsize=4)
def load_stand(test_data_ref: str) -> Stand | None:
    if test_data_ref != "demo-landscape:fixture-v1":
        return None
    landscape = Landscape.load()
    items_by_system = {
        "crm": landscape.orders_of(STAND_USER),
        "stock": list(landscape.stock.values()),
        "pricing": list(landscape.prices.values()),
    }
    routes: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, Any]] = {}
    samples: dict[str, tuple[str, ...]] = {}
    for system, (obj, path) in COLLECTIONS.items():
        key = SCHEMAS[system]["objects"][0]["key"]
        items = items_by_system[system]
        routes[system] = {"/schema": SCHEMAS[system], path: {"items": items}}
        records[system] = {}
        for item in items:
            routes[system][f"{path}/{item[key]}"] = item
            records[system][f"{obj}/{item[key]}"] = item
        samples[system] = (f"{obj}/{SAMPLE_KEYS[system]}",)
    return Stand(routes, records, samples)
