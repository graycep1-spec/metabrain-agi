"""HTTP API учебных систем crm / stock / pricing (T-18, только чтение)."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.demo_landscape.data import SCHEMAS, SYSTEMS, Landscape


TOKENS_REF = "env:DEMO_LANDSCAPE_TOKENS"


@dataclass(frozen=True)
class Grant:
    user: str
    systems: frozenset[str]


def parse_tokens(raw: str) -> dict[str, Grant]:
    """`{token: {user, systems[]}}` из secret store."""
    try:
        data = json.loads(raw)
        grants = {
            token: Grant(entry["user"], frozenset(entry["systems"]))
            for token, entry in data.items()
        }
    except (ValueError, AttributeError, KeyError, TypeError) as error:
        raise ValueError("DEMO_LANDSCAPE_TOKENS must be JSON {token: {user, systems[]}}") from error
    for grant in grants.values():
        if not isinstance(grant.user, str) or not grant.user or not grant.systems <= set(SYSTEMS):
            raise ValueError("DEMO_LANDSCAPE_TOKENS has an invalid grant")
    return grants


def create_app(landscape: Landscape, tokens: Mapping[str, Grant] | None) -> FastAPI:
    app = FastAPI(title="demo-landscape", docs_url=None, redoc_url=None, openapi_url=None)

    def require(system: str, authorization: str | None) -> Grant:
        if tokens is None:
            raise HTTPException(503, "tokens_not_configured")
        scheme, _, token = (authorization or "").partition(" ")
        grant = tokens.get(token) if scheme.lower() == "bearer" and token else None
        if grant is None:
            raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})
        if system not in grant.systems:
            raise HTTPException(403, "system_not_granted")
        return grant

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "demo-landscape", "tokens": tokens is not None}

    @app.get("/{system}/schema")
    def schema(system: str, authorization: str | None = Header(None)) -> dict[str, Any]:
        if system not in SCHEMAS:
            raise HTTPException(404, "unknown_system")
        require(system, authorization)
        return SCHEMAS[system]

    @app.get("/crm/orders")
    def orders(status: str | None = None, authorization: str | None = Header(None)):
        grant = require("crm", authorization)
        return {"items": landscape.orders_of(grant.user, status)}

    @app.get("/crm/orders/{order_id}")
    def order(order_id: str, authorization: str | None = Header(None)):
        grant = require("crm", authorization)
        found = landscape.orders.get(order_id)
        if found is None or found["manager"] != grant.user:
            raise HTTPException(404, "not_found")  # чужая заявка неотличима от несуществующей
        return found

    @app.get("/stock/items")
    def stock_items(authorization: str | None = Header(None)):
        require("stock", authorization)
        return {"items": list(landscape.stock.values())}

    @app.get("/stock/items/{sku}")
    def stock_item(sku: str, authorization: str | None = Header(None)):
        require("stock", authorization)
        return _get(landscape.stock, sku)

    @app.get("/pricing/prices")
    def prices(authorization: str | None = Header(None)):
        require("pricing", authorization)
        return {"items": list(landscape.prices.values())}

    @app.get("/pricing/prices/{sku}")
    def price(sku: str, authorization: str | None = Header(None)):
        require("pricing", authorization)
        return _get(landscape.prices, sku)

    return app


def _get(items: Mapping[str, Any], key: str) -> Any:
    if key not in items:
        raise HTTPException(404, "not_found")
    return items[key]


def load_tokens(secret_store: EnvSecretStore | None = None) -> dict[str, Grant] | None:
    try:
        raw = (secret_store or EnvSecretStore()).resolve(TOKENS_REF)
    except SecretNotFoundError:
        return None  # сервис жив, данные не отдаёт: 503
    return parse_tokens(raw)


def main() -> None:
    import uvicorn

    try:
        settings = load_settings()
    except ValueError as error:
        raise SystemExit(str(error)) from error

    uvicorn.run(
        create_app(Landscape.load(), load_tokens()),
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
