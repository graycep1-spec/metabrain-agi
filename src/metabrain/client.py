"""HTTP-клиент мета-мозга для харнесса (C-5, «Транспорт»): навыки — resolve, propose, publish.

Ошибки — те же классы, что у `Metabrain` в процессе: код и статус из ответа, сеть и 5xx — `Unavailable`.
"""

from collections.abc import Mapping
from typing import Any, Protocol

import httpx

from src.metabrain.service import Conflict, InvalidRequest, MetabrainError, NotFound, Rejected, Unavailable


ERRORS: dict[int, type[MetabrainError]] = {400: InvalidRequest, 403: Rejected, 404: NotFound, 409: Conflict}


class MetabrainClient(Protocol):
    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def propose(self, request: Mapping[str, Any]) -> dict[str, Any]: ...

    def publish(self, request: Mapping[str, Any]) -> dict[str, Any]: ...


class HttpMetabrainClient:
    def __init__(self, base_url: str, *, timeout: float = 60.0, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    def resolve(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/v1/skills/resolve", request)

    def propose(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/v1/skills/propose", request)

    def publish(self, request: Mapping[str, Any]) -> dict[str, Any]:
        return self._post("/v1/skills/publish", request)

    def _post(self, path: str, request: Mapping[str, Any]) -> dict[str, Any]:
        try:
            response = self._client.post(path, json=dict(request))
        except httpx.HTTPError as error:
            raise Unavailable("metabrain_unavailable", str(error)) from error
        if response.status_code == 200:
            return response.json()
        try:
            code = response.json().get("error")
        except ValueError:
            code = None
        raise ERRORS.get(response.status_code, Unavailable)(code or None, f"metabrain answered {response.status_code}")
