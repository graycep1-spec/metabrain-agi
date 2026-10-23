"""Внутренний HTTP мета-мозга (C-5, «Транспорт»). Наружу не публикуется."""

import os
import threading
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import psycopg
from starlette.concurrency import run_in_threadpool

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.metabrain.consent import load_public_key
from src.metabrain.policy import load_policy
from src.metabrain.service import InvalidRequest, Metabrain, MetabrainError, Unavailable
from src.metabrain.store import migrate
from src.metabrain.validator_client import HttpValidatorClient


DATABASE_URL_REF = "env:METABRAIN_DATABASE_URL"

ROUTES = {
    "/v1/skills/resolve": "resolve",
    "/v1/skills/propose": "propose",
    "/v1/skills/publish": "publish",
    "/v1/retrieval/index": "index",
    "/v1/retrieval/search": "search",
    "/v1/policy/get": "policy",
}


class Readiness:
    """Схема БД накатывается при первом успешном подключении; до этого — 503."""

    def __init__(self, connect):
        self._connect = connect
        self._ready = False
        self._lock = threading.Lock()

    def ensure(self) -> bool:
        if self._ready:
            return True
        with self._lock:
            if not self._ready:
                try:
                    with self._connect() as connection:
                        migrate(connection)
                    self._ready = True
                except Exception:
                    return False
        return True


def create_app(metabrain: Metabrain, readiness: Readiness | None = None) -> FastAPI:
    app = FastAPI(title="metabrain", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "metabrain", "db": readiness.ensure() if readiness else True}

    def handler(method: str):
        async def endpoint(request: Request):
            try:
                body = await request.json()
            except ValueError:
                return JSONResponse({"error": "invalid_request", "detail": "body must be JSON"}, 400)
            if readiness is not None and not readiness.ensure():
                return JSONResponse({"error": "database_unavailable"}, 503)
            try:
                result = await run_in_threadpool(getattr(metabrain, method), body)
            except MetabrainError as error:
                payload = {"error": error.code}
                if isinstance(error, InvalidRequest) and error.detail:
                    payload["detail"] = error.detail
                return JSONResponse(payload, error.status)
            return result

        return endpoint

    for path, method in ROUTES.items():
        app.add_api_route(path, handler(method), methods=["POST"])
    return app


def build(environ=None) -> tuple[Metabrain, Readiness]:
    source = os.environ if environ is None else environ
    store = EnvSecretStore(source)
    try:
        database_url = store.resolve(DATABASE_URL_REF)
    except SecretNotFoundError:
        database_url = None

    def connect():
        if database_url is None:
            raise Unavailable("database_unavailable")
        return psycopg.connect(database_url, connect_timeout=5)

    consent_key = source.get("CONSENT_PUBLIC_KEY", "").strip()
    metabrain = Metabrain(
        connect,
        HttpValidatorClient(source.get("VALIDATOR_URL", "http://validator:8080")),
        load_policy(),
        load_public_key(consent_key) if consent_key else None,
    )
    return metabrain, Readiness(connect)


def main() -> None:
    import uvicorn

    try:
        settings = load_settings()
        metabrain, readiness = build()
    except ValueError as error:
        raise SystemExit(str(error)) from error
    readiness.ensure()
    uvicorn.run(
        create_app(metabrain, readiness),
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
