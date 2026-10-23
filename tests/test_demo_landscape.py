"""T-18, GT-dl-00: фикстура и API demo-landscape (specs/60-demo-landscape.md)."""

from datetime import date, timedelta
import json

from fastapi.testclient import TestClient
import pytest

from src.common.secrets import EnvSecretStore
from src.demo_landscape.app import Grant, create_app, load_tokens, parse_tokens
from src.demo_landscape.data import SCHEMAS, Landscape


ALL = frozenset({"crm", "stock", "pricing"})
TOKENS = {
    "tok-a": Grant("manager-a", ALL),
    "tok-b": Grant("manager-b", ALL),
    "tok-stock-only": Grant("manager-a", frozenset({"stock"})),
}


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def landscape() -> Landscape:
    return Landscape.load()


@pytest.fixture
def client(landscape) -> TestClient:
    return TestClient(create_app(landscape, TOKENS))


# --- инварианты данных ------------------------------------------------------

def test_each_new_order_has_exactly_one_shortage_with_a_viable_analog(landscape):
    today = date.fromisoformat(landscape.today)
    new_orders = [o for o in landscape.orders.values() if o["status"] == "new"]
    assert {o["order_id"] for o in new_orders} == {"1042", "1043"}

    for order in new_orders:
        deadline = date.fromisoformat(order["deadline"])
        short = [i for i in order["items"] if landscape.stock[i["sku"]]["qty_available"] < i["qty"]]
        assert len(short) == 1, order["order_id"]

        item = short[0]
        viable = []
        for sku in landscape.stock[item["sku"]]["analogs"]:
            analog, price = landscape.stock[sku], landscape.prices[sku]
            margin = (price["list_price"] - price["cost"]) / price["list_price"]
            if (
                analog["qty_available"] >= item["qty"]
                and today + timedelta(days=analog["lead_time_days"]) <= deadline
                and margin >= price["min_margin"]
            ):
                viable.append(sku)
        assert viable, order["order_id"]

        for rest in order["items"]:
            if rest is not item:
                stock = landscape.stock[rest["sku"]]
                assert stock["qty_available"] >= rest["qty"]
                assert today + timedelta(days=stock["lead_time_days"]) <= deadline


def test_scenario_orders_belong_to_different_managers(landscape):
    assert landscape.orders["1042"]["manager"] == "manager-a"
    assert landscape.orders["1043"]["manager"] == "manager-b"


def test_every_sku_is_priced_and_every_analog_exists(landscape):
    assert set(landscape.prices) == set(landscape.stock)
    for item in landscape.stock.values():
        assert set(item["analogs"]) <= set(landscape.stock)
    for order in landscape.orders.values():
        assert {i["sku"] for i in order["items"]} <= set(landscape.stock)


# --- schema -------------------------------------------------------------------

def test_schemas_describe_three_systems_with_links_and_pii(client):
    schemas = {s: client.get(f"/{s}/schema", headers=auth("tok-a")).json() for s in ("crm", "stock", "pricing")}

    assert schemas == SCHEMAS
    order = schemas["crm"]["objects"][0]
    assert order["sensitivity"] == {"client": "pii", "contact": "pii"}
    links = {link["to"] for s in schemas.values() for o in s["objects"] for link in o["links"]}
    assert {"stock.item.sku", "pricing.price.sku"} <= links


def test_schema_fields_match_the_data(landscape):
    samples = {"crm": landscape.orders["1042"], "stock": landscape.stock["A-17"], "pricing": landscape.prices["A-17"]}
    for system, sample in samples.items():
        assert set(SCHEMAS[system]["objects"][0]["fields"]) == set(sample)


# --- доступ -------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, auth("unknown"), {"Authorization": "Basic tok-a"}, {"Authorization": "Bearer"}])
def test_missing_or_unknown_token_is_401(client, headers):
    assert client.get("/crm/orders", headers=headers).status_code == 401


def test_system_outside_grant_is_403(client):
    assert client.get("/crm/orders", headers=auth("tok-stock-only")).status_code == 403
    assert client.get("/pricing/schema", headers=auth("tok-stock-only")).status_code == 403
    assert client.get("/stock/items", headers=auth("tok-stock-only")).status_code == 200


def test_crm_shows_only_the_managers_own_orders(client):
    a = client.get("/crm/orders", headers=auth("tok-a")).json()["items"]
    new_b = client.get("/crm/orders?status=new", headers=auth("tok-b")).json()["items"]

    assert {o["order_id"] for o in a} == {"1042", "1037"}
    assert [o["order_id"] for o in new_b] == ["1043"]
    assert client.get("/crm/orders/1042", headers=auth("tok-a")).status_code == 200
    assert client.get("/crm/orders/1043", headers=auth("tok-a")).status_code == 404


def test_stock_and_pricing_are_shared(client):
    for token in ("tok-a", "tok-b"):
        assert client.get("/stock/items/A-17M", headers=auth(token)).json()["qty_available"] == 40
        assert client.get("/pricing/prices/D-09S", headers=auth(token)).json()["list_price"] == 170
    assert client.get("/stock/items/NOPE", headers=auth("tok-a")).status_code == 404


@pytest.mark.parametrize("method, path", [
    ("post", "/crm/orders"),
    ("put", "/crm/orders/1042"),
    ("patch", "/crm/orders/1042"),
    ("delete", "/stock/items/A-17"),
    ("post", "/pricing/prices"),
])
def test_landscape_is_read_only(client, method, path):
    assert getattr(client, method)(path, headers=auth("tok-a")).status_code == 405


def test_without_token_table_service_is_healthy_but_serves_no_data(landscape):
    client = TestClient(create_app(landscape, None))

    assert client.get("/healthz").json() == {"status": "ok", "service": "demo-landscape", "tokens": False}
    assert client.get("/crm/orders", headers=auth("tok-a")).status_code == 503


def test_tokens_come_from_secret_store_and_never_appear_in_responses(client):
    raw = json.dumps({"tok-x": {"user": "manager-a", "systems": ["crm"]}})
    tokens = load_tokens(EnvSecretStore({"DEMO_LANDSCAPE_TOKENS": raw}))

    assert tokens == {"tok-x": Grant("manager-a", frozenset({"crm"}))}
    assert load_tokens(EnvSecretStore({})) is None
    body = client.get("/crm/orders", headers=auth("tok-a")).text
    assert "tok-a" not in body


@pytest.mark.parametrize("raw", [
    "not json",
    "[]",
    json.dumps({"t": {"user": "u"}}),
    json.dumps({"t": {"user": "", "systems": ["crm"]}}),
    json.dumps({"t": {"user": "u", "systems": ["erp"]}}),
])
def test_invalid_token_table_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_tokens(raw)
