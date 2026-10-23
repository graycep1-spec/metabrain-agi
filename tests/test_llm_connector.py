from dataclasses import asdict
from decimal import Decimal
import json
from uuid import uuid4

import httpx
import pytest

from src.common.secrets import EnvSecretStore
from src.llm.connector import (
    BudgetExceededError,
    ChatRequest,
    LLMContext,
    LLMConnector,
    ProviderConfig,
    RoutingError,
)


CHAT_RESPONSE = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Ответ с источником."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 8, "completion_tokens": 5, "total_tokens": 13},
}


class RecordingBudget:
    def __init__(self):
        self.calls = []

    def check(self, context, provider, request):
        self.calls.append((context, provider, request))
        return uuid4()

    def settle(self, reservation, event):
        pass

    def cancel(self, reservation):
        pass


class RejectingBudget:
    def check(self, context, provider, request):
        raise BudgetExceededError("budget_exceeded")

    def settle(self, reservation, event):
        pass

    def cancel(self, reservation):
        pass


def providers():
    return {
        "deepseek-flash": ProviderConfig(
            name="deepseek-flash",
            base_url="https://api.deepseek.com",
            api_key_ref="env:DEEPSEEK_API_KEY",
            model="deepseek-flash",
            family="deepseek-v4.1-flash",
            max_tokens=8192,
            temperature=0.2,
        ),
        "local": ProviderConfig(
            name="local",
            base_url="http://vllm:8000/v1",
            api_key_ref=None,
            model="local-chat",
            family="main",
            max_tokens=512,
            temperature=0.2,
            external=False,
        ),
        "deepseek-v4-pro": ProviderConfig(
            name="deepseek-v4-pro",
            base_url="https://api.deepseek.com",
            api_key_ref="env:DEEPSEEK_API_KEY",
            model="deepseek-v4-pro",
            family="deepseek-v4-pro",
            max_tokens=8192,
            temperature=0.0,
        ),
    }


def routes(
    analyst="deepseek-flash",
    developer="deepseek-flash",
    critic="deepseek-v4-pro",
    researcher=None,
):
    return {
        "analyst": analyst,
        "developer": developer,
        "critic": critic,
        "researcher": researcher or analyst,
    }


def context(sub_role="analyst"):
    return LLMContext(
        company_id="company-1",
        harness_id="harness-1",
        employee_id="employee-1",
        job_id="job-1",
        sub_role=sub_role,
    )


def request(**kwargs):
    return ChatRequest(
        messages=[{"role": "user", "content": "Summarize the order."}],
        **kwargs,
    )


@pytest.fixture
def connector_factory():
    clients = []

    def create(
        handler=None,
        configured_routes=None,
        configured_providers=None,
        budget=None,
        dlp_rules=None,
        dlp_enabled=True,
    ):
        client = httpx.Client(
            transport=httpx.MockTransport(handler or default_handler)
        )
        clients.append(client)
        recorder = []
        budget_guard = budget or RecordingBudget()
        provider_configs = configured_providers or providers()
        connector = LLMConnector(
            providers=provider_configs,
            routes=configured_routes or routes(),
            secret_store=EnvSecretStore(
                {
                    "DEEPSEEK_API_KEY": "deepseek-test-secret",
                }
            ),
            http_client=client,
            budget_guard=budget_guard,
            cost_calculator=lambda provider, usage: Decimal("0.003"),
            usage_recorder=recorder.append,
            dlp_rules=dlp_rules or {"pii": "mask", "internal": "drop"},
            dlp_enabled=dlp_enabled,
        )
        return connector, client, recorder, budget_guard

    yield create
    for client in clients:
        client.close()


def default_handler(http_request):
    return httpx.Response(200, json=CHAT_RESPONSE)


def test_gt_c2_01_openai_compatible_chat_uses_deepseek_base_url(connector_factory):
    observed = []

    def handler(http_request):
        observed.append(http_request)
        return httpx.Response(200, json=CHAT_RESPONSE)

    connector, _, _, _ = connector_factory(handler=handler)
    result = connector.chat_completions(context(), request())

    assert result.response == CHAT_RESPONSE
    assert str(observed[0].url) == "https://api.deepseek.com/chat/completions"
    assert observed[0].headers["authorization"] == "Bearer deepseek-test-secret"
    assert "deepseek-test-secret" not in observed[0].content.decode()


def test_gt_c2_02_switching_to_local_uses_configured_endpoint_without_api_key(
    connector_factory,
):
    observed = []

    def handler(http_request):
        observed.append(http_request)
        return httpx.Response(200, json=CHAT_RESPONSE)

    connector, _, _, _ = connector_factory(
        handler=handler,
        configured_routes=routes(analyst="local"),
    )

    result = connector.chat_completions(context(), request())

    assert result.response["choices"][0]["message"]["content"] == "Ответ с источником."
    assert str(observed[0].url) == "http://vllm:8000/v1/chat/completions"
    assert "authorization" not in observed[0].headers


def test_gt_c2_03_sensitive_fields_are_masked_or_dropped_before_external_egress(
    connector_factory,
):
    observed = []

    def handler(http_request):
        observed.append(json.loads(http_request.content))
        return httpx.Response(200, json=CHAT_RESPONSE)

    connector, _, _, _ = connector_factory(handler=handler)
    outgoing = ChatRequest(
        messages=[
            {
                "role": "user",
                "content": {
                    "client": "Ada Lovelace",
                    "order_id": "private-order-42",
                    "status": "new",
                },
            }
        ],
        field_sensitivity={"client": "pii", "order_id": "internal"},
    )

    connector.chat_completions(context(), outgoing)

    content = observed[0]["messages"][0]["content"]
    assert content == {"client": "[REDACTED:pii]", "status": "new"}
    assert "Ada Lovelace" not in json.dumps(observed[0])
    assert "private-order-42" not in json.dumps(observed[0])


def test_gt_c2_07_fact_placeholders_are_resolved_only_after_dlp(connector_factory):
    observed = []

    def handler(http_request):
        observed.append(http_request.content)
        return httpx.Response(200, json=CHAT_RESPONSE)

    connector, _, _, _ = connector_factory(handler=handler)
    connector.chat_completions(
        context(),
        ChatRequest(
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Customer: {{fact:order/1042.customer}}; "
                        "status: {{fact:order/1042.status}}"
                    ),
                }
            ],
            facts=[
                {
                    "ref": "order/1042",
                    "data": {
                        "customer": "Ada Lovelace",
                        "status": "open",
                    },
                    "source": {
                        "system": "crm",
                        "object_ref": "order/1042",
                        "read_at": "2026-10-21T00:00:00Z",
                    },
                    "sensitivity": {"customer": "pii"},
                }
            ],
        ),
    )

    body = json.loads(observed[0])
    assert body["messages"][0]["content"] == (
        "Customer: [REDACTED:pii]; status: open"
    )
    assert "Ada Lovelace" not in observed[0].decode()
    assert "facts" not in body
    assert "field_sensitivity" not in body


def test_dropped_fact_fields_disappear_and_missing_fact_fails_before_egress(
    connector_factory,
):
    observed = []

    def handler(http_request):
        observed.append(http_request.content)
        return httpx.Response(200, json=CHAT_RESPONSE)

    connector, _, _, _ = connector_factory(handler=handler)
    connector.chat_completions(
        context(),
        ChatRequest(
            messages=[
                {
                    "role": "user",
                    "content": "Order {{fact:order/1042.internal_id}}",
                }
            ],
            facts=[
                {
                    "ref": "order/1042",
                    "data": {"internal_id": "private-42"},
                    "sensitivity": {"internal_id": "internal"},
                }
            ],
        ),
    )

    dropped_body = json.loads(observed[0])
    assert dropped_body["messages"][0]["content"] == "Order "
    assert "private-42" not in observed[0].decode()
    with pytest.raises(ValueError, match="fact"):
        connector.chat_completions(
            context(),
            ChatRequest(
                messages=[
                    {
                        "role": "user",
                        "content": "Order {{fact:missing/1.id}}",
                    }
                ],
            ),
        )
    assert len(observed) == 1


def test_external_provider_rejects_disabled_dlp_at_startup(connector_factory):
    with pytest.raises(ValueError, match="external"):
        connector_factory(dlp_enabled=False)


def test_local_provider_may_disable_dlp_without_sending_internal_metadata(
    connector_factory,
):
    configured_providers = {
        "local": providers()["local"],
        "local-critic": ProviderConfig(
            name="local-critic",
            base_url="http://vllm:8000/v1",
            api_key_ref=None,
            model="local-critic",
            family="local-critic",
            max_tokens=512,
            temperature=0,
            external=False,
        ),
    }
    configured_routes = routes(
        analyst="local",
        developer="local",
        critic="local-critic",
        researcher="local",
    )
    observed = []
    connector, _, _, _ = connector_factory(
        handler=lambda http_request: observed.append(
            json.loads(http_request.content)
        )
        or httpx.Response(200, json=CHAT_RESPONSE),
        configured_routes=configured_routes,
        configured_providers=configured_providers,
        dlp_enabled=False,
    )

    connector.chat_completions(
        context(),
        ChatRequest(
            messages=[{"role": "user", "content": "Ada Lovelace"}],
            field_sensitivity={"content": "pii"},
        ),
    )

    assert observed[0]["messages"][0]["content"] == "Ada Lovelace"
    assert "field_sensitivity" not in observed[0]


def test_gt_c2_04_usage_provenance_and_cost_are_recorded_for_harness_and_employee(
    connector_factory,
):
    connector, _, recorder, _ = connector_factory()

    result = connector.chat_completions(context(), request())
    event = recorder[0]

    assert event.context.harness_id == "harness-1"
    assert event.context.employee_id == "employee-1"
    assert event.provider == "deepseek-flash"
    assert event.model == "deepseek-flash"
    assert event.usage.prompt_tokens == 8
    assert event.usage.completion_tokens == 5
    assert event.cost == Decimal("0.003")
    assert event.latency_ms >= 0
    assert result.event == event


def test_gt_c2_05_api_key_is_not_in_logs_or_traceable_request_data(
    connector_factory, caplog
):
    connector, _, recorder, _ = connector_factory()
    outgoing = request()
    result = connector.chat_completions(context(), outgoing)
    serialized_trace = json.dumps(asdict(result.event), default=str)
    serialized_request = json.dumps(asdict(outgoing), default=str)

    assert "deepseek-test-secret" not in caplog.text
    assert "deepseek-test-secret" not in serialized_trace
    assert "deepseek-test-secret" not in serialized_request
    assert "deepseek-test-secret" not in repr(recorder)


def test_gt_c2_06_critic_and_developer_use_different_families_and_budget_is_preflighted(
    connector_factory,
):
    connector, _, _, budget = connector_factory()
    developer = connector.provider_for("developer")
    critic = connector.provider_for("critic")

    assert developer.family != critic.family
    connector.chat_completions(context("developer"), request())
    assert budget.calls[0][1].name == "deepseek-flash"

    calls = []
    rejecting_connector, _, _, _ = connector_factory(
        handler=lambda http_request: calls.append(http_request),
        budget=RejectingBudget(),
    )
    with pytest.raises(BudgetExceededError) as error:
        rejecting_connector.chat_completions(context(), request())

    assert error.value.code == "budget_exceeded"
    assert calls == []


def test_connector_rejects_critic_and_developer_routes_with_same_family(
    connector_factory,
):
    with pytest.raises(RoutingError, match="different model families"):
        connector_factory(configured_routes=routes(critic="deepseek-flash"))


def test_embeddings_uses_openai_compatible_endpoint(connector_factory):
    observed = []

    def handler(http_request):
        observed.append(http_request)
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "model": "deepseek-flash",
                "usage": {"prompt_tokens": 3, "total_tokens": 3},
            },
        )

    connector, _, _, _ = connector_factory(handler=handler)
    result = connector.embeddings(
        context(),
        {
            "input": "Ada Lovelace",
            "field_sensitivity": {"input": "pii"},
        },
    )

    assert result.response["data"][0]["embedding"] == [0.1, 0.2]
    assert str(observed[0].url) == "https://api.deepseek.com/embeddings"
    payload = json.loads(observed[0].content)
    assert payload["input"] == "[REDACTED:pii]"
    assert "field_sensitivity" not in payload
