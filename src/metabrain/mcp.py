"""MCP-сервер мета-мозга (C-5, «MCP»; ADR-022).

Streamable HTTP, один эндпоинт `POST /mcp`, только JSON-ответы.
Две эпохи протокола: современная 2026-07-28 (без состояния, версия в `_meta` каждого
запроса) и устаревшие 2025-xx через `initialize` без сессий.
"""

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import hmac
import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from src.metabrain.policy import MCP_TOOLS, SUB_ROLES
from src.metabrain.service import InvalidRequest, Metabrain, MetabrainError, Unavailable


MODERN = "2026-07-28"
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
SUPPORTED = (MODERN, *LEGACY)
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
HEADER_MISMATCH, UNSUPPORTED_PROTOCOL_VERSION = -32020, -32022

SERVER_INFO = {"name": "metabrain", "version": "0.1.0"}
INSTRUCTIONS = (
    "Мета-мозг компании: навыки (как делать типовые задачи), поиск по фактам и истории работ "
    "харнесса и политика (модели, лимиты, пороги автономии). Все инструменты только читают."
)
TOOLS_TTL_MS = 60_000

_LIMIT = {"type": "integer", "minimum": 1, "maximum": 50, "description": "Сколько вернуть, 1..50 (по умолчанию 10)"}
TOOLS: dict[str, dict[str, Any]] = {
    "skills_resolve": {
        "name": "skills_resolve",
        "title": "Найти навыки",
        "description": (
            "Навыки, доступные клиенту: личные навыки его харнесса и навыки компании. "
            "Последние версии, отфильтрованные по классу задачи, тегам ролей и тексту; "
            "или ровно одна версия по lock."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "lock": {"type": "string", "description": "skill_id@version — точная версия навыка"},
                "query": {"type": "string", "description": "Слова для полнотекстового поиска"},
                "task_class": {"type": "string", "description": "Класс задачи, например quote"},
                "role_tags": {"type": "array", "items": {"type": "string"}, "description": "Теги ролей, например sales"},
                "limit": _LIMIT,
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "retrieval_search": {
        "name": "retrieval_search",
        "title": "Поиск по данным харнесса",
        "description": "Полнотекстовый поиск по фактам ландшафта, траекториям работ и карточкам знаний харнесса клиента.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Что искать"},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["fact", "trajectory", "card"]},
                    "description": "Какие документы искать",
                },
                "limit": _LIMIT,
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
    "policy_get": {
        "name": "policy_get",
        "title": "Политика",
        "description": "Действующая политика: маршруты моделей, цены, лимиты, DLP, пороги автономии.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "employee_id": {"type": "string", "description": "Сузить L2-классы до этого сотрудника"},
                "sub_role": {"type": "string", "enum": list(SUB_ROLES), "description": "Добавить модель этой роли"},
            },
            "additionalProperties": False,
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    },
}
TOOL_ORDER = MCP_TOOLS  # детерминированный порядок
HARNESS_ONLY = frozenset({"retrieval_search"})


@dataclass(frozen=True)
class Client:
    client_id: str
    company_id: str
    harness_id: str | None = None


@dataclass
class McpResponse:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


def _digest(token: str) -> bytes:
    return sha256(token.encode("utf-8")).digest()


def parse_clients(raw: str) -> dict[bytes, Client]:
    """`MCP_CLIENTS`: `{token: {client_id, company_id, harness_id?}}`; храним только хэши токенов."""
    try:
        data = json.loads(raw)
        clients = {
            _digest(token): Client(entry["client_id"], entry["company_id"], entry.get("harness_id"))
            for token, entry in data.items()
        }
    except (ValueError, AttributeError, KeyError, TypeError) as error:
        raise ValueError("MCP_CLIENTS must be JSON {token: {client_id, company_id, harness_id?}}") from error
    for token, client in zip(data, clients.values()):
        if len(token) < 24:
            raise ValueError("MCP client tokens must be at least 24 characters")
        if not all(isinstance(v, str) and v for v in (client.client_id, client.company_id)):
            raise ValueError("MCP client needs client_id and company_id")
    return clients


def _error(id_: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id_, "error": error}


def _decode_header(value: str | None) -> str | None:
    """Mcp-Name может прийти как `=?base64?...?=` (Value Encoding)."""
    if value is None:
        return None
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?"):-2], validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return "\x00invalid"
    return value


def _check_arguments(schema: Mapping[str, Any], arguments: Any) -> str | None:
    if not isinstance(arguments, Mapping):
        return "arguments must be an object"
    properties = schema["properties"]
    unknown = sorted(set(arguments) - set(properties))
    if unknown:
        return f"unknown arguments: {unknown}"
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        return f"missing arguments: {missing}"
    for name, value in arguments.items():
        spec = properties[name]
        kind = spec["type"]
        if kind == "string" and not isinstance(value, str):
            return f"{name} must be a string"
        if kind == "integer" and (not isinstance(value, int) or isinstance(value, bool) or not spec["minimum"] <= value <= spec["maximum"]):
            return f"{name} must be an integer within {spec['minimum']}..{spec['maximum']}"
        if kind == "array" and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            return f"{name} must be a list of strings"
        allowed = spec.get("enum") or spec.get("items", {}).get("enum")
        if allowed and any(v not in allowed for v in (value if isinstance(value, list) else [value])):
            return f"{name} must be within {allowed}"
    return None


class McpServer:
    def __init__(
        self,
        metabrain: Metabrain,
        clients: Mapping[bytes, Client],
        policy: Mapping[str, Any],
        allowed_origins: frozenset[str] = frozenset(),
        readiness: Any = None,
    ):
        self._metabrain = metabrain
        self._clients = dict(clients)
        self._policy = policy
        self._allowed_origins = allowed_origins
        self._readiness = readiness

    # --- вход ------------------------------------------------------------------

    def handle(self, headers: Mapping[str, str], raw_body: bytes) -> McpResponse:
        headers = {k.lower(): v for k, v in headers.items()}
        origin = headers.get("origin")
        if origin is not None and origin not in self._allowed_origins:
            return McpResponse(403, _error(None, INVALID_REQUEST, "Origin is not allowed"))
        client = self._authenticate(headers.get("authorization"))
        if client is None:
            return McpResponse(401, _error(None, INVALID_REQUEST, "Unauthorized"), {"WWW-Authenticate": 'Bearer realm="metabrain-mcp"'})

        try:
            message = json.loads(raw_body)
        except ValueError:
            return McpResponse(400, _error(None, PARSE_ERROR, "Parse error"))
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            return McpResponse(400, _error(None, INVALID_REQUEST, "Invalid Request: a single JSON-RPC 2.0 message is expected"))
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        if META_VERSION in meta:
            return self._modern(client, headers, message, params, meta)
        return self._legacy(client, headers, message, params)

    def _authenticate(self, authorization: str | None) -> Client | None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            self._audit_safely(operation="mcp.auth", outcome="unauthorized", detail={"reason": "missing_token"})
            return None
        digest = _digest(token.strip())
        for known, client in self._clients.items():
            if hmac.compare_digest(known, digest):
                return client
        self._audit_safely(operation="mcp.auth", outcome="unauthorized", detail={"reason": "unknown_token"})
        return None

    # --- современная эпоха (2026-07-28) ------------------------------------------

    def _modern(self, client: Client, headers, message, params, meta) -> McpResponse:
        id_ = message.get("id")
        method = message["method"]
        version = meta.get(META_VERSION)
        if headers.get("mcp-protocol-version") != version:
            return McpResponse(400, _error(id_, HEADER_MISMATCH, "Header mismatch: MCP-Protocol-Version does not match _meta"))
        if version != MODERN:
            return McpResponse(400, _error(id_, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                                           {"supported": list(SUPPORTED), "requested": version}))
        if headers.get("mcp-method") != method:
            return McpResponse(400, _error(id_, HEADER_MISMATCH, "Header mismatch: Mcp-Method does not match body method"))
        if method == "tools/call" and _decode_header(headers.get("mcp-name")) != params.get("name"):
            return McpResponse(400, _error(id_, HEADER_MISMATCH, "Header mismatch: Mcp-Name does not match params.name"))
        if not isinstance(meta.get(META_CAPABILITIES), dict):
            return McpResponse(400, _error(id_, INVALID_PARAMS, f"_meta.{META_CAPABILITIES} is required"))
        if "id" not in message:
            return McpResponse(202)

        def complete(result: dict[str, Any]) -> McpResponse:
            result = {**result, "resultType": "complete", "_meta": {META_SERVER_INFO: SERVER_INFO}}
            return McpResponse(200, {"jsonrpc": "2.0", "id": id_, "result": result})

        if method == "server/discover":
            return complete({
                "supportedVersions": list(SUPPORTED),
                "capabilities": {"tools": {}},
                "instructions": INSTRUCTIONS,
                "ttlMs": 3_600_000,
                "cacheScope": "public",
            })
        if method == "tools/list":
            return complete({"tools": self._visible_tools(client), "ttlMs": TOOLS_TTL_MS, "cacheScope": "private"})
        if method == "tools/call":
            outcome = self._call_tool(client, params)
            if isinstance(outcome, McpResponse):
                outcome.body["id"] = id_
                return outcome
            return complete(outcome)
        return McpResponse(404, _error(id_, METHOD_NOT_FOUND, f"Method not found: {method}"))

    # --- устаревшая эпоха (2025-03-26 … 2025-11-25) ---------------------------------

    def _legacy(self, client: Client, headers, message, params) -> McpResponse:
        id_ = message.get("id")
        method = message["method"]
        if method == "initialize":
            requested = params.get("protocolVersion")
            return McpResponse(200, {"jsonrpc": "2.0", "id": id_, "result": {
                "protocolVersion": requested if requested in LEGACY else LEGACY[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
                "instructions": INSTRUCTIONS,
            }})
        header_version = headers.get("mcp-protocol-version", "2025-03-26")
        if header_version not in LEGACY:
            return McpResponse(400, _error(id_, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                                           {"supported": list(SUPPORTED), "requested": header_version}))
        if "id" not in message:
            return McpResponse(202)
        if method == "ping":
            return McpResponse(200, {"jsonrpc": "2.0", "id": id_, "result": {}})
        if method == "tools/list":
            return McpResponse(200, {"jsonrpc": "2.0", "id": id_, "result": {"tools": self._visible_tools(client)}})
        if method == "tools/call":
            outcome = self._call_tool(client, params)
            if isinstance(outcome, McpResponse):
                outcome.body["id"] = id_
                return outcome
            return McpResponse(200, {"jsonrpc": "2.0", "id": id_, "result": outcome})
        return McpResponse(200, _error(id_, METHOD_NOT_FOUND, f"Method not found: {method}"))

    # --- инструменты -------------------------------------------------------------

    def _allowed(self, client: Client) -> list[str]:
        scopes = set((self._policy.get("mcp_scopes") or {}).get(client.client_id, []))
        return [
            name for name in TOOL_ORDER
            if name in scopes and (name not in HARNESS_ONLY or client.harness_id is not None)
        ]

    def _visible_tools(self, client: Client) -> list[dict[str, Any]]:
        return [TOOLS[name] for name in self._allowed(client)]

    def _call_tool(self, client: Client, params: Mapping[str, Any]) -> dict[str, Any] | McpResponse:
        name = params.get("name")
        if name not in self._allowed(client):
            if name in TOOLS:
                self._audit_safely(
                    operation="mcp.tools/call", outcome="forbidden", company_id=client.company_id,
                    harness_id=client.harness_id, detail={"client_id": client.client_id, "tool": name},
                )
            return McpResponse(200, _error(None, INVALID_PARAMS, f"Unknown tool: {name}"))

        arguments = params.get("arguments") or {}
        problem = _check_arguments(TOOLS[name]["inputSchema"], arguments)
        if problem:
            return self._tool_error(problem)
        if self._readiness is not None and not self._readiness.ensure():
            return McpResponse(503, _error(None, INTERNAL_ERROR, "Service temporarily unavailable"))

        request = {"company_id": client.company_id, "harness_id": client.harness_id, **arguments}
        via = {"channel": "mcp", "client_id": client.client_id}
        method = {"skills_resolve": "resolve", "retrieval_search": "search", "policy_get": "policy"}[name]
        try:
            result = getattr(self._metabrain, method)(request, via)
        except Unavailable:
            return McpResponse(503, _error(None, INTERNAL_ERROR, "Service temporarily unavailable"))
        except InvalidRequest as error:
            return self._tool_error(error.detail or error.code)
        except MetabrainError as error:
            return self._tool_error(error.code)
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
            "isError": False,
        }

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}

    def _audit_safely(self, **record: Any) -> None:
        try:
            self._metabrain.audit_only(**record)
        except MetabrainError:
            pass  # журнал недоступен — отказ всё равно отдаём


def create_app(server: McpServer) -> FastAPI:
    app = FastAPI(title="metabrain-mcp", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "service": "metabrain-mcp"}

    @app.post("/mcp")
    async def mcp(request: Request):
        outcome = await run_in_threadpool(server.handle, dict(request.headers), await request.body())
        if outcome.body is None:
            return Response(status_code=outcome.status, headers=outcome.headers)
        return JSONResponse(outcome.body, outcome.status, headers=outcome.headers)

    @app.api_route("/mcp", methods=["GET", "DELETE"])
    def not_allowed():
        return Response(status_code=405, headers={"Allow": "POST"})

    return app


def main() -> None:
    import uvicorn

    from src.common.config import load_settings
    from src.common.secrets import EnvSecretStore, SecretNotFoundError
    from src.metabrain.app import build
    from src.metabrain.policy import load_policy

    try:
        settings = load_settings()
        metabrain, readiness = build()
        try:
            clients = parse_clients(EnvSecretStore().resolve("env:MCP_CLIENTS"))
        except SecretNotFoundError:
            clients = {}  # клиентов нет — все запросы 401
    except ValueError as error:
        raise SystemExit(str(error)) from error
    origins = frozenset(o.strip() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip())
    server = McpServer(metabrain, clients, load_policy(), origins, readiness)
    uvicorn.run(create_app(server), host="0.0.0.0", port=settings.port,
                log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
