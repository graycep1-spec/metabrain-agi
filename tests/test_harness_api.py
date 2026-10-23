from collections.abc import Iterator
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import os
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock
from uuid import uuid4

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from src.common.secrets import EnvSecretStore
from src.harness.api import C1Service, create_app
from src.harness.proposals import ConsentError
from src.harness.store import Registration


API_TOKEN = "test-harness-api-token"
KEY_MATERIAL = base64.b64encode(
    Ed25519PrivateKey.generate().private_bytes(
        Encoding.Raw,
        PrivateFormat.Raw,
        NoEncryption(),
    )
).decode("ascii")
PUBLIC_KEY_MATERIAL = base64.b64encode(
    Ed25519PrivateKey.from_private_bytes(base64.b64decode(KEY_MATERIAL))
    .public_key()
    .public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode("ascii")


def test_system_identifier_must_belong_to_configured_registry():
    service = object.__new__(C1Service)
    service._system_registry = frozenset({"crm"})
    service._researcher = None

    assert service._system({"system": "crm"}) == "crm"
    with pytest.raises(ValueError, match="not in the landscape registry"):
        service._system({"system": "erp"})


def test_non_curator_decision_is_journaled_and_returns_forbidden():
    identity = Registration(uuid4(), uuid4(), uuid4())
    proposal_id = uuid4()
    service = object.__new__(C1Service)
    service._identity = Mock(return_value=identity)
    service._requested_harness = Mock(return_value=identity.harness_id)
    service._required_uuid = Mock(return_value=proposal_id)
    service._optional_text = Mock(return_value=None)
    service._proposals = Mock()
    service._proposals.decide.side_effect = ConsentError("not_curator")
    service._journal = Mock()

    status, result = service.decide_proposal(
        {
            "proposal_id": str(proposal_id),
            "decision": "approve",
            "channel_user_id": "42",
        }
    )

    assert (status, result) == (403, {"error": "not_curator"})
    service._journal.append_entry.assert_called_once_with(
        person_id=identity.person_id,
        company_id=identity.company_id,
        harness_id=identity.harness_id,
        kind="proposal.decision_denied",
        payload={
            "proposal_id": str(proposal_id),
            "decision": "approve",
            "reason": "not_curator",
            "surface": "telegram",
        },
    )


@pytest.fixture
def api_client() -> Iterator[TestClient]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")

    schema = f"test_harness_api_{uuid4().hex}"
    with psycopg.connect(database_url) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    connection = psycopg.connect(database_url, options=f"-c search_path={schema}")
    current_time = [datetime.now(timezone.utc)]
    app = create_app(
        connection,
        api_token=API_TOKEN,
        app_base_url="https://cabinet.example.test",
        secret_store=EnvSecretStore(
            {"CONSENT_SIGNING_KEY": KEY_MATERIAL, "CONSENT_PUBLIC_KEY": PUBLIC_KEY_MATERIAL}
        ),
        clock=lambda: current_time[0],
    )
    client = TestClient(app)
    app.state.test_clock = current_time
    app.state.test_connection = connection
    try:
        yield client
    finally:
        client.close()
        connection.close()
        with psycopg.connect(database_url) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.commit()


def operation(
    client: TestClient,
    name: str,
    payload: dict[str, object],
):
    return client.post(
        f"/v1/operations/{name}",
        json=payload,
        headers={"Authorization": f"Bearer {API_TOKEN}"},
    )


def test_c1_http_api_requires_service_authentication(api_client):
    response = api_client.post(
        "/v1/operations/person.register",
        json={
            "channel": "telegram",
            "channel_user_id": "100",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    )

    assert response.status_code == 401


def test_registration_requires_explicit_external_llm_consent(api_client):
    response = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "100",
            "company_name": "Example",
            "external_llm_consent": False,
        },
    )

    assert response.status_code == 403
    assert response.json()["error"] == "external_llm_consent_required"


def test_registration_creates_company_and_returns_invite_link(api_client):
    response = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "101",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["person_id"]
    assert body["company_id"]
    assert body["harness_id"]
    assert body["invite_link"].startswith("https://t.me/")
    assert body["consent"]["external_llm"] is True


def test_company_invite_registers_person_into_the_same_company(api_client):
    owner = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "102",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    invite = operation(
        api_client,
        "company.invite",
        {"channel_user_id": "102", "harness_id": owner["harness_id"]},
    )

    assert invite.status_code == 200
    invite_token = parse_qs(urlsplit(invite.json()["invite_link"]).query)["start"][0]
    joined = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "103",
            "company_invite": invite_token,
            "external_llm_consent": True,
        },
    )

    assert joined.status_code == 201
    assert joined.json()["company_id"] == owner["company_id"]
    assert joined.json()["harness_id"] != owner["harness_id"]


@pytest.mark.parametrize("invite", ["does-not-exist", ""])
def test_invalid_invite_does_not_create_a_person_or_company(api_client, invite):
    response = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "104",
            "company_invite": invite,
            "external_llm_consent": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_invite"
    with api_client.app.state.test_connection.transaction():
        people = api_client.app.state.test_connection.execute(
            "SELECT count(*) FROM people"
        ).fetchone()[0]
        companies = api_client.app.state.test_connection.execute(
            "SELECT count(*) FROM companies"
        ).fetchone()[0]
    assert people == 0
    assert companies == 0


def test_expired_invite_does_not_register_person(api_client):
    owner = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "117",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    invite = operation(
        api_client,
        "company.invite",
        {
            "channel_user_id": "117",
            "harness_id": owner["harness_id"],
        },
    ).json()
    token = parse_qs(urlsplit(invite["invite_link"]).query)["start"][0]
    api_client.app.state.test_clock[0] += timedelta(days=8)

    response = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "118",
            "company_invite": token,
            "external_llm_consent": True,
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_invite"
    with api_client.app.state.test_connection.transaction():
        people = api_client.app.state.test_connection.execute(
            "SELECT count(*) FROM people"
        ).fetchone()[0]
        companies = api_client.app.state.test_connection.execute(
            "SELECT count(*) FROM companies"
        ).fetchone()[0]
    assert people == 1
    assert companies == 1


def test_employee_draft_creates_pending_passport_proposal(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "105",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()

    response = operation(
        api_client,
        "employee.draft",
        {
            "channel_user_id": "105",
            "harness_id": identity["harness_id"],
            "kind": "twin",
            "role_text": "Менеджер по продажам",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["employee_id"]
    assert body["proposal_id"]
    assert body["passport_draft"]["role_text"] == "Менеджер по продажам"
    assert body["status"] == "pending"


def test_foreign_harness_operations_are_forbidden(api_client):
    owner = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "106",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    invite = operation(
        api_client,
        "company.invite",
        {
            "channel_user_id": "106",
            "harness_id": owner["harness_id"],
        },
    ).json()
    invite_token = parse_qs(urlsplit(invite["invite_link"]).query)["start"][0]
    other = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "107",
            "company_invite": invite_token,
            "external_llm_consent": True,
        },
    ).json()

    assert other["company_id"] == owner["company_id"]
    assert other["harness_id"] != owner["harness_id"]
    for name, extra in (
        ("landscape.map", {}),
        ("employee.list", {}),
        ("ask", {"text": "Question"}),
        ("journal.list", {}),
    ):
        response = operation(
            api_client,
            name,
            {
                "channel_user_id": "107",
                "harness_id": owner["harness_id"],
                **extra,
            },
        )
        assert response.status_code == 403
        assert response.json()["error"] == "forbidden_harness"


def test_ask_returns_explicit_not_implemented_until_t14_runtime(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "115",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()

    response = operation(
        api_client,
        "ask",
        {
            "channel_user_id": "115",
            "harness_id": identity["harness_id"],
            "text": "Question",
        },
    )

    assert response.status_code == 501
    assert response.json()["error"] == "not_implemented"


def test_proposal_list_can_filter_by_employee(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "116",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    drafts = []
    for role_text in ("Роль 1", "Роль 2"):
        drafts.append(
            operation(
                api_client,
                "employee.draft",
                {
                    "channel_user_id": "116",
                    "harness_id": identity["harness_id"],
                    "kind": "new",
                    "role_text": role_text,
                },
            ).json()
        )

    response = operation(
        api_client,
        "proposal.list",
        {
            "channel_user_id": "116",
            "harness_id": identity["harness_id"],
            "employee_id": drafts[0]["employee_id"],
        },
    )

    assert response.status_code == 200
    proposals = response.json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["employee_id"] == drafts[0]["employee_id"]


def test_connect_start_returns_short_lived_cabinet_url(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "108",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()

    response = operation(
        api_client,
        "landscape.connect.start",
        {
            "channel_user_id": "108",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["connect_request_id"]
    assert body["cabinet_url"].startswith("https://cabinet.example.test/")
    assert "secret" not in response.text.lower()
    assert body["exp"]


def test_connect_request_is_one_time_and_secret_ref_is_required(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "110",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    started = operation(
        api_client,
        "landscape.connect.start",
        {
            "channel_user_id": "110",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
        },
    ).json()
    request_id = started["connect_request_id"]

    missing_secret = operation(
        api_client,
        "landscape.connect",
        {
            "channel_user_id": "110",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
            "connect_request_id": request_id,
        },
    )
    assert missing_secret.status_code == 400

    connect = operation(
        api_client,
        "landscape.connect",
        {
            "channel_user_id": "110",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
            "secret_ref": "env:DEMO_LANDSCAPE_TOKENS",
            "connect_request_id": request_id,
        },
    )
    assert connect.status_code == 201
    assert "secret_ref" not in connect.text

    replay = operation(
        api_client,
        "landscape.connect",
        {
            "channel_user_id": "110",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
            "secret_ref": "env:DEMO_LANDSCAPE_TOKENS",
            "connect_request_id": request_id,
        },
    )
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_connect_request"


def test_connect_request_cannot_be_used_from_another_harness(api_client):
    owner = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "111",
            "company_name": "Owner",
            "external_llm_consent": True,
        },
    ).json()
    other = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "112",
            "company_name": "Other",
            "external_llm_consent": True,
        },
    ).json()
    started = operation(
        api_client,
        "landscape.connect.start",
        {
            "channel_user_id": "111",
            "harness_id": owner["harness_id"],
            "system": "demo-landscape",
        },
    ).json()

    response = operation(
        api_client,
        "landscape.connect",
        {
            "channel_user_id": "112",
            "harness_id": other["harness_id"],
            "system": "demo-landscape",
            "secret_ref": "env:DEMO_LANDSCAPE_TOKENS",
            "connect_request_id": started["connect_request_id"],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_connect_request"


def test_expired_connect_request_is_rejected(api_client):
    identity = operation(
        api_client,
        "person.register",
        {
            "channel": "telegram",
            "channel_user_id": "119",
            "company_name": "Example",
            "external_llm_consent": True,
        },
    ).json()
    started = operation(
        api_client,
        "landscape.connect.start",
        {
            "channel_user_id": "119",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
        },
    ).json()
    api_client.app.state.test_clock[0] += timedelta(minutes=31)

    response = operation(
        api_client,
        "landscape.connect",
        {
            "channel_user_id": "119",
            "harness_id": identity["harness_id"],
            "system": "demo-landscape",
            "secret_ref": "env:DEMO_LANDSCAPE_TOKENS",
            "connect_request_id": started["connect_request_id"],
        },
    )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_connect_request"
    proposals = operation(
        api_client,
        "proposal.list",
        {
            "channel_user_id": "119",
            "harness_id": identity["harness_id"],
            "kind": "connect",
        },
    )
    assert proposals.json()["proposals"] == []


def test_telegram_http_api_client_calls_c1_operation(api_client):
    from src.harness.telegram import HTTPC1API

    async def call_api():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_client.app),
            base_url="http://testserver",
        ) as http_client:
            client = HTTPC1API(
                "http://testserver",
                api_token=API_TOKEN,
                client=http_client,
            )
            return await client.call(
                "person.register",
                {
                    "channel": "telegram",
                    "channel_user_id": "109",
                    "company_name": "Example",
                    "external_llm_consent": True,
                },
            )

    result = asyncio.run(call_api())

    assert result["person_id"]
