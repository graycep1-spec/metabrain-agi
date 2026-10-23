"""T-24: MCP-сервер мета-мозга — GT-c5-06, -08..-11 (C-5, «MCP»; ADR-022)."""

import base64
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

from src.metabrain.mcp import (
    HEADER_MISMATCH,
    MODERN,
    SUPPORTED,
    UNSUPPORTED_PROTOCOL_VERSION,
    Client,
    McpServer,
    _digest,
    create_app,
    parse_clients,
)
from src.metabrain.policy import load_policy
from src.metabrain.service import InvalidRequest, Unavailable


COMPANY = str(uuid4())
HARNESS = str(uuid4())
TOKEN_HARNESS = "harness-token-0123456789abcdef"
TOKEN_COMPANY = "company-token-0123456789abcdef"
TOKEN_NARROW = "narrow-token-0123456789abcdefgh"

CLIENTS = {
    _digest(TOKEN_HARNESS): Client("ide-a", COMPANY, HARNESS),
    _digest(TOKEN_COMPANY): Client("project-x", COMPANY, None),
    _digest(TOKEN_NARROW): Client("narrow", COMPANY, HARNESS),
}
ALL_TOOLS = ["skills_resolve", "retrieval_search", "policy_get"]


def policy() -> dict:
    p = load_policy()
    p["mcp_scopes"] = {"ide-a": ALL_TOOLS, "project-x": ALL_TOOLS, "narrow": ["policy_get"]}
    return p


class FakeMetabrain:
    def __init__(self):
        self.calls, self.audits = [], []
        self.fail_with = None

    def _record(self, method, request, via):
        self.calls.append((method, dict(request), dict(via)))
        if self.fail_with:
            raise self.fail_with
        return {"method": method}

    def resolve(self, request, via=None):
        return self._record("resolve", request, via)

    def search(self, request, via=None):
        return self._record("search", request, via)

    def policy(self, request, via=None):
        return self._record("policy", request, via)

    def audit_only(self, **record):
        self.audits.append(record)


@pytest.fixture
def brain():
    return FakeMetabrain()


@pytest.fixture
def client(brain):
    server = McpServer(brain, CLIENTS, policy(), frozenset({"https://cabinet.example"}))
    return TestClient(create_app(server))


def modern(method, params=None, *, token=TOKEN_HARNESS, version=MODERN, headers=None, id_=1, name=None):
    body = {"jsonrpc": "2.0", "method": method, "params": {
        **(params or {}),
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    }}
    if id_ is not None:
        body["id"] = id_
    sent = {
        "Authorization": f"Bearer {token}",
        "MCP-Protocol-Version": version,
        "Mcp-Method": method,
        "Accept": "application/json, text/event-stream",
    }
    if name is not None:
        sent["Mcp-Name"] = name
    sent.update(headers or {})
    return body, {k: v for k, v in sent.items() if v is not None}


def call(client, method, params=None, **kwargs):
    body, headers = modern(method, params, **kwargs)
    return client.post("/mcp", content=json.dumps(body), headers=headers)


def tool_call(client, tool, arguments=None, **kwargs):
    return call(client, "tools/call", {"name": tool, "arguments": arguments or {}}, name=tool, **kwargs)


# --- GT-c5-09: авторизация и Origin ----------------------------------------------

@pytest.mark.parametrize("authorization", [None, "Bearer nope-nope-nope-nope-nope-nope", "Basic abc"])
def test_gt_c5_09_requests_without_a_known_token_are_401(client, brain, authorization):
    body, headers = modern("tools/list")
    headers.pop("Authorization")
    if authorization:
        headers["Authorization"] = authorization
    response = client.post("/mcp", content=json.dumps(body), headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert brain.audits[-1]["outcome"] == "unauthorized"


def test_gt_c5_09_foreign_origin_is_403(client):
    assert call(client, "tools/list", headers={"Origin": "https://evil.example"}).status_code == 403
    assert call(client, "tools/list", headers={"Origin": "https://cabinet.example"}).status_code == 200


def test_get_and_delete_on_the_endpoint_are_405(client):
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


# --- GT-c5-10: современная эпоха --------------------------------------------------

def test_gt_c5_10_server_discover(client):
    result = call(client, "server/discover").json()["result"]
    assert result["supportedVersions"] == list(SUPPORTED)
    assert result["capabilities"] == {"tools": {}}
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "metabrain"
    assert {"ttlMs", "cacheScope"} <= set(result)


@pytest.mark.parametrize("headers, name", [
    ({"MCP-Protocol-Version": "2025-11-25"}, "policy_get"),
    ({"MCP-Protocol-Version": None}, "policy_get"),
    ({"Mcp-Method": "tools/list"}, "policy_get"),
    ({}, "skills_resolve"),
    ({}, None),
])
def test_gt_c5_10_headers_must_match_the_body(client, headers, name):
    body, sent = modern("tools/call", {"name": "policy_get", "arguments": {}}, headers=headers, name=name)
    response = client.post("/mcp", content=json.dumps(body), headers=sent)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == HEADER_MISMATCH


def test_gt_c5_10_base64_encoded_mcp_name_is_decoded(client):
    encoded = "=?base64?" + base64.b64encode(b"policy_get").decode() + "?="
    body, headers = modern("tools/call", {"name": "policy_get", "arguments": {}}, name=encoded)
    assert client.post("/mcp", content=json.dumps(body), headers=headers).status_code == 200


def test_gt_c5_10_unsupported_version_lists_supported_versions(client):
    response = call(client, "tools/list", version="2030-01-01")
    error = response.json()["error"]
    assert response.status_code == 400
    assert error["code"] == UNSUPPORTED_PROTOCOL_VERSION
    assert error["data"] == {"supported": list(SUPPORTED), "requested": "2030-01-01"}


def test_modern_unknown_method_is_404_and_missing_capabilities_is_400(client):
    response = call(client, "resources/list")
    assert response.status_code == 404 and response.json()["error"]["code"] == -32601

    body, headers = modern("tools/list")
    del body["params"]["_meta"]["io.modelcontextprotocol/clientCapabilities"]
    assert client.post("/mcp", content=json.dumps(body), headers=headers).status_code == 400


def test_modern_tools_list_is_private_cacheable_and_deterministic(client):
    result = call(client, "tools/list").json()["result"]
    assert [t["name"] for t in result["tools"]] == ALL_TOOLS
    assert result["cacheScope"] == "private" and result["ttlMs"] > 0
    assert all(t["annotations"]["readOnlyHint"] for t in result["tools"])


# --- GT-c5-06, -08: scope и проекция ----------------------------------------------

def test_gt_c5_06_tool_outside_scope_is_hidden_refused_and_audited(client, brain):
    listed = call(client, "tools/list", token=TOKEN_NARROW).json()["result"]["tools"]
    refused = tool_call(client, "skills_resolve", token=TOKEN_NARROW)

    assert [t["name"] for t in listed] == ["policy_get"]
    assert refused.json()["error"]["code"] == -32602
    assert brain.calls == []
    assert brain.audits[-1] == {
        "operation": "mcp.tools/call", "outcome": "forbidden", "company_id": COMPANY,
        "harness_id": HARNESS, "detail": {"client_id": "narrow", "tool": "skills_resolve"},
    }


def test_company_level_client_has_no_harness_retrieval(client):
    listed = call(client, "tools/list", token=TOKEN_COMPANY).json()["result"]["tools"]
    assert [t["name"] for t in listed] == ["skills_resolve", "policy_get"]
    assert tool_call(client, "retrieval_search", {"query": "x"}, token=TOKEN_COMPANY).json()["error"]["code"] == -32602


def test_gt_c5_08_projection_comes_from_the_token_not_from_arguments(client, brain):
    ok = tool_call(client, "skills_resolve", {"task_class": "quote"}).json()["result"]
    injected = tool_call(client, "skills_resolve", {"harness_id": str(uuid4())}).json()["result"]

    method, request, via = brain.calls[0]
    assert (method, request["company_id"], request["harness_id"], request["task_class"]) == ("resolve", COMPANY, HARNESS, "quote")
    assert via == {"channel": "mcp", "client_id": "ide-a"}
    assert ok["structuredContent"] == {"method": "resolve"} and ok["isError"] is False
    assert injected["isError"] is True and "unknown arguments" in injected["content"][0]["text"]
    assert len(brain.calls) == 1


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": "5"}, {"role_tags": "sales"}, {"query": 5},
])
def test_invalid_arguments_are_tool_errors(client, arguments):
    result = tool_call(client, "skills_resolve", arguments).json()["result"]
    assert result["isError"] is True


def test_service_errors_map_to_tool_errors_or_503(client, brain):
    brain.fail_with = InvalidRequest(detail="lock must be skill_id@version")
    assert tool_call(client, "skills_resolve", {"lock": "x"}).json()["result"]["content"][0]["text"] == "lock must be skill_id@version"
    brain.fail_with = Unavailable("database_unavailable")
    assert tool_call(client, "policy_get").status_code == 503


# --- GT-c5-11: устаревшая эпоха ------------------------------------------------------

def legacy(client, method, params=None, *, id_=1, version="2025-11-25"):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if id_ is not None:
        body["id"] = id_
    headers = {"Authorization": f"Bearer {TOKEN_HARNESS}", "Accept": "application/json, text/event-stream"}
    if version is not None:
        headers["MCP-Protocol-Version"] = version
    return client.post("/mcp", content=json.dumps(body), headers=headers)


def test_gt_c5_11_legacy_client_works_without_a_session(client):
    init = legacy(client, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "old", "version": "1"}}, version=None)
    initialized = legacy(client, "notifications/initialized", id_=None)
    tools = legacy(client, "tools/list").json()["result"]["tools"]
    result = legacy(client, "tools/call", {"name": "policy_get", "arguments": {}}).json()["result"]

    assert init.json()["result"]["protocolVersion"] == "2025-11-25"
    assert "mcp-session-id" not in {k.lower() for k in init.headers}
    assert initialized.status_code == 202
    assert [t["name"] for t in tools] == ALL_TOOLS
    assert result["structuredContent"] == {"method": "policy"}
    assert legacy(client, "ping").json()["result"] == {}


def test_legacy_initialize_with_unknown_version_gets_our_latest_legacy(client):
    init = legacy(client, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}, version=None)
    assert init.json()["result"]["protocolVersion"] == "2025-11-25"


def test_legacy_request_with_unsupported_header_version_is_400(client):
    assert legacy(client, "tools/list", version="2024-01-01").status_code == 400
    assert legacy(client, "tools/list", version=None).status_code == 200  # без заголовка = 2025-03-26


def test_batches_and_garbage_are_rejected(client):
    headers = {"Authorization": f"Bearer {TOKEN_HARNESS}"}
    assert client.post("/mcp", content="[]", headers=headers).status_code == 400
    assert client.post("/mcp", content="not json", headers=headers).json()["error"]["code"] == -32700


# --- реестр клиентов -------------------------------------------------------------

def test_client_registry_stores_only_token_hashes_and_rejects_weak_tokens():
    clients = parse_clients(json.dumps({TOKEN_HARNESS: {"client_id": "ide-a", "company_id": COMPANY, "harness_id": HARNESS}}))
    assert TOKEN_HARNESS.encode() not in b"".join(clients)
    with pytest.raises(ValueError):
        parse_clients(json.dumps({"short": {"client_id": "a", "company_id": COMPANY}}))
    with pytest.raises(ValueError):
        parse_clients(json.dumps({TOKEN_HARNESS: {"client_id": "a"}}))


def test_policy_rejects_unknown_mcp_tools():
    from src.metabrain.policy import PolicyError, validate_policy

    p = policy()
    p["mcp_scopes"]["ide-a"] = ["skill_publish"]
    with pytest.raises(PolicyError):
        validate_policy(p)


# --- сквозной прогон на PostgreSQL --------------------------------------------------

def test_mcp_end_to_end_on_postgres(database, connect):
    from src.metabrain.service import Metabrain
    from tests.test_metabrain import CONSENT_PUBLIC, NOW, InProcessValidator, candidate, consent_for

    company, harness_a, harness_b = str(uuid4()), str(uuid4()), str(uuid4())
    brain = Metabrain(connect, InProcessValidator(), load_policy(), CONSENT_PUBLIC, clock=lambda: NOW)
    personal = brain.propose(candidate("personal", harness_a, company, title="Личный приём"))
    brain.publish({"company_id": company, "harness_id": harness_a, "candidate_id": personal["candidate_id"]})
    shared = brain.propose(candidate("company", harness_a, company))
    brain.publish({"company_id": company, "harness_id": harness_a, "candidate_id": shared["candidate_id"],
                   "consent_token": consent_for(shared, harness=harness_a)})
    brain.index({"company_id": company, "harness_id": harness_a, "docs": [{"doc_id": "f1", "kind": "fact", "text": "кронштейн A-17 в дефиците"}]})

    clients = {
        _digest(TOKEN_HARNESS): Client("ide-b", company, harness_b),
        _digest(TOKEN_COMPANY): Client("project-x", company, None),
    }
    p = load_policy()
    p["mcp_scopes"] = {"ide-b": ALL_TOOLS, "project-x": ALL_TOOLS}
    client = TestClient(create_app(McpServer(brain, clients, p)))

    as_b = tool_call(client, "skills_resolve", {"task_class": "quote"}).json()["result"]["structuredContent"]["skills"]
    as_project = tool_call(client, "skills_resolve", {"task_class": "quote"}, token=TOKEN_COMPANY).json()["result"]["structuredContent"]["skills"]
    b_search = tool_call(client, "retrieval_search", {"query": "кронштейн"}).json()["result"]["structuredContent"]["hits"]

    assert [s["scope"] for s in as_b] == ["company"]          # личный навык A не виден B
    assert [s["scope"] for s in as_project] == ["company"]
    assert b_search == []                                     # ретривал A не виден B

    with connect() as connection:
        rows = connection.execute(
            "SELECT operation, outcome, detail->>'client_id' FROM mb_audit WHERE operation LIKE 'mcp.%' ORDER BY id"
        ).fetchall()
    assert rows == [
        ("mcp.skills.resolve", "ok", "ide-b"),
        ("mcp.skills.resolve", "ok", "project-x"),
        ("mcp.retrieval.search", "ok", "ide-b"),
    ]


@pytest.fixture
def database():
    import os

    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")
    schema = f"test_mcp_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield schema
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def connect(database):
    from src.metabrain.store import migrate
    from tests.test_metabrain import connector

    factory = connector(database)
    with factory() as connection:
        migrate(connection)
    return factory
