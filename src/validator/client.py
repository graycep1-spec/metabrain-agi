"""HTTP-клиент валидатора (C-4, «Транспорт») для мета-мозга и харнесса."""

from collections.abc import Mapping
import threading
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import httpx

from src.validator.scorecard import load_public_key


class ValidatorUnavailable(RuntimeError):
    code = "validator_unavailable"


class ValidatorClient(Protocol):
    def validate(self, artifact: Mapping[str, Any], profile: str, context: Mapping[str, Any]) -> dict[str, Any]: ...

    def keys(self, *, refresh: bool = False) -> Mapping[str, Ed25519PublicKey]: ...


class HttpValidatorClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)
        self._keys: dict[str, Ed25519PublicKey] = {}
        self._lock = threading.Lock()

    def validate(self, artifact: Mapping[str, Any], profile: str, context: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(
                "/v1/validate", json={"artifact": artifact, "profile": profile, "context": context}
            )
        except httpx.HTTPError as error:
            raise ValidatorUnavailable(str(error)) from error
        if response.status_code != 200:
            raise ValidatorUnavailable(f"validator answered {response.status_code}")
        return response.json()

    def keys(self, *, refresh: bool = False) -> Mapping[str, Ed25519PublicKey]:
        with self._lock:
            if refresh or not self._keys:
                try:
                    response = self._client.get("/v1/keys")
                    response.raise_for_status()
                except httpx.HTTPError as error:
                    raise ValidatorUnavailable(str(error)) from error
                self._keys = {k["key_id"]: load_public_key(k["public_key"]) for k in response.json()["keys"]}
            return dict(self._keys)
