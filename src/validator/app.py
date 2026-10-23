"""HTTP-транспорт валидатора (C-4, раздел «Транспорт»): внутренняя сеть compose."""

import os
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.validator.checkers import default_checkers
from src.validator.scorecard import ScoreCardSigner, load_private_key
from src.validator.service import InvalidRequest, SigningUnavailable, Validator


class ValidateRequest(BaseModel):
    artifact: Any
    profile: Any
    context: Any


def create_app(validator: Validator) -> FastAPI:
    app = FastAPI(title="metabrain-validator", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "validator",
            "signing": validator.signer is not None,
            "profile_set": validator.profile_set,
        }

    @app.get("/v1/keys")
    def keys() -> dict[str, Any]:
        signer = validator.signer
        return {"keys": signer.keys() if signer else []}

    @app.post("/v1/validate")
    def validate(request: ValidateRequest):
        try:
            card = validator.validate(request.artifact, request.profile, request.context)
        except InvalidRequest as error:
            return JSONResponse({"error": "invalid_request", "detail": str(error)}, 400)
        except SigningUnavailable:
            return JSONResponse({"error": "signing_key_not_configured"}, 503)
        return card.to_dict()

    return app


def build_validator(
    secret_ref: str,
    secret_store: EnvSecretStore | None = None,
    profile_set: str = "mvp-v1",
) -> Validator:
    store = secret_store or EnvSecretStore()
    try:
        signer = ScoreCardSigner(load_private_key(store.resolve(secret_ref)))
    except SecretNotFoundError:
        # Без ключа сервис жив, но ничего не подписывает: /v1/validate → 503.
        signer = None
    return Validator(signer, default_checkers(), profile_set=profile_set)


def main() -> None:
    import uvicorn

    try:
        settings = load_settings()
    except ValueError as error:
        raise SystemExit(str(error)) from error

    profile_set = os.environ.get("VALIDATOR_PROFILE_SET", "mvp-v1").strip()
    try:
        validator = build_validator(settings.validator_signing_key_ref, profile_set=profile_set)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    app = create_app(validator)
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    main()
