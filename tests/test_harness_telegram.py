import asyncio
from collections.abc import Mapping
from uuid import uuid4

import httpx
import pytest

from src.harness.telegram import C1APIError, HTTPC1API, TelegramAdapter, create_router


class FakeC1API:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.responses: dict[str, Mapping[str, object]] = {}
        self.errors: dict[str, Exception] = {}

    async def call(
        self,
        operation: str,
        payload: Mapping[str, object],
    ) -> Mapping[str, object]:
        self.calls.append((operation, dict(payload)))
        if operation in self.errors:
            raise self.errors[operation]
        return self.responses.get(operation, {})


def test_start_registers_telegram_identity_with_optional_invite():
    api = FakeC1API()
    adapter = TelegramAdapter(api)

    reply = asyncio.run(adapter.handle_message(telegram_user_id=42, text="/start invite-abc"))

    assert not api.calls
    assert "согласие" in reply.text.lower()
    assert any(
        button.callback_data == "register:invite-abc" for button in reply.buttons
    )


def test_registration_is_created_only_after_explicit_external_llm_consent():
    api = FakeC1API()
    api.responses["person.register"] = {
        "person_id": str(uuid4()),
        "company_id": str(uuid4()),
        "harness_id": str(uuid4()),
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_callback(
            telegram_user_id=42,
            callback_data="register:-",
        )
    )

    assert api.calls == [
        (
            "person.register",
            {
                "channel": "telegram",
                "channel_user_id": "42",
                "external_llm_consent": True,
                "company_name": "Компания Telegram 42",
            },
        )
    ]
    assert reply.text.startswith("Регистрация завершена.")


def test_role_command_creates_twin_draft_and_decision_buttons():
    api = FakeC1API()
    proposal_id = str(uuid4())
    api.responses["employee.draft"] = {
        "employee_id": str(uuid4()),
        "passport_draft": {"title": "Менеджер по продажам"},
        "proposal_id": proposal_id,
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(
            telegram_user_id=42,
            text="/role Менеджер по продажам",
        )
    )

    assert api.calls == [
        (
            "employee.draft",
            {
                "channel_user_id": "42",
                "kind": "twin",
                "role_text": "Менеджер по продажам",
            },
        )
    ]
    assert {button.callback_data for button in reply.buttons} == {
        f"proposal:{proposal_id}:approve",
        f"proposal:{proposal_id}:return",
        f"proposal:{proposal_id}:incident",
    }


def test_connect_never_accepts_or_sends_a_chat_secret():
    api = FakeC1API()
    api.responses["landscape.connect.start"] = {
        "cabinet_url": "https://cabinet.example.test/connections?request=abc"
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(
            telegram_user_id=42,
            text="/connect demo-landscape",
        )
    )

    assert api.calls == [
        (
            "landscape.connect.start",
            {"channel_user_id": "42", "system": "demo-landscape"},
        )
    ]
    assert "secret_ref" not in repr(api.calls)
    assert "кабинет" in reply.text.lower()
    assert "https://cabinet.example.test/connections?request=abc" in reply.text
    assert "парол" not in reply.text.lower()


def test_invite_command_returns_api_invite_link():
    api = FakeC1API()
    api.responses["company.invite"] = {
        "invite_link": "https://t.me/metabrain_bot?start=invite-token",
        "exp": "2030-01-01T00:00:00+00:00",
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(telegram_user_id=42, text="/invite")
    )

    assert api.calls == [
        ("company.invite", {"channel_user_id": "42"}),
    ]
    assert "https://t.me/metabrain_bot?start=invite-token" in reply.text


def test_map_displays_landscape_map():
    api = FakeC1API()
    api.responses["landscape.map"] = {
        "map": {"crm": {"status": "connected"}, "stock": {"status": "connected"}},
        "sources": [{"ref": "demo-landscape"}],
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(adapter.handle_message(telegram_user_id=42, text="/map"))

    assert api.calls == [("landscape.map", {"channel_user_id": "42"})]
    assert "crm" in reply.text
    assert "stock" in reply.text


def test_free_text_answer_includes_sources_and_score_ref():
    api = FakeC1API()
    api.responses["ask"] = {
        "answer": "В наличии 4 единицы.",
        "sources": [{"ref": "stock/item-1"}],
        "score_ref": "score-123",
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(telegram_user_id=42, text="Сколько товара в наличии?")
    )

    assert api.calls == [
        (
            "ask",
            {
                "channel_user_id": "42",
                "text": "Сколько товара в наличии?",
            },
        )
    ]
    assert "stock/item-1" in reply.text
    assert "score-123" in reply.text


def test_employee_mention_resolves_title_and_targets_that_employee():
    api = FakeC1API()
    employee_id = str(uuid4())
    api.responses["employee.list"] = {
        "employees": [
            {"employee_id": employee_id, "title": "Менеджер по продажам", "status": "active"},
            {"employee_id": str(uuid4()), "title": "Аналитик", "status": "active"},
        ]
    }
    api.responses["ask"] = {
        "answer": "Ответ.",
        "sources": [{"ref": "crm/customer-1"}],
        "score_ref": "score-123",
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(
            telegram_user_id=42,
            text="@Менеджер по продажам сколько активных клиентов?",
        )
    )

    assert api.calls == [
        ("employee.list", {"channel_user_id": "42"}),
        (
            "ask",
            {
                "channel_user_id": "42",
                "text": "сколько активных клиентов?",
                "employee_id": employee_id,
            },
        ),
    ]
    assert "Источники" in reply.text


def test_unknown_employee_mention_does_not_fall_back_to_default_employee():
    api = FakeC1API()
    api.responses["employee.list"] = {"employees": []}
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_message(
            telegram_user_id=42,
            text="@Неизвестный сотрудник ответь",
        )
    )

    assert api.calls == [("employee.list", {"channel_user_id": "42"})]
    assert "не наш" in reply.text.lower()


@pytest.mark.parametrize(
    "result",
    [
        {"answer": "Ответ без provenance"},
        {"answer": "Ответ", "sources": [], "score_ref": "score-123"},
        {"answer": "Ответ", "sources": [{"ref": "source"}], "score_ref": ["invalid"]},
    ],
)
def test_answer_without_sources_or_score_is_blocked(result):
    api = FakeC1API()
    api.responses["ask"] = result
    adapter = TelegramAdapter(api)

    reply = asyncio.run(adapter.handle_message(telegram_user_id=42, text="Вопрос"))

    assert "заблокирован" in reply.text.lower()


def test_http_api_client_reports_transport_errors():
    async def exercise():
        def fail_request(request):
            raise httpx.ConnectError("connection failed", request=request)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail_request),
        ) as http_client:
            api = HTTPC1API(
                "http://harness.test",
                api_token="service-token",
                client=http_client,
            )
            with pytest.raises(C1APIError) as error:
                await api.call("ask", {"channel_user_id": "42", "text": "Hi"})

        assert error.value.status_code == 502
        assert error.value.code == "api_unavailable"

    asyncio.run(exercise())


def test_proposal_button_passes_telegram_identity_and_decision():
    api = FakeC1API()
    proposal_id = str(uuid4())
    api.responses["proposal.decide"] = {"status": "approved"}
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_callback(
            telegram_user_id=42,
            callback_data=f"proposal:{proposal_id}:approve",
        )
    )

    assert api.calls == [
        (
            "proposal.decide",
            {
                "channel_user_id": "42",
                "proposal_id": proposal_id,
                "decision": "approve",
                "surface": "telegram",
            },
        )
    ]
    assert "подтвержден" in reply.text.lower()


def test_proposal_button_from_non_curator_is_rejected():
    api = FakeC1API()
    api.errors["proposal.decide"] = C1APIError(
        status_code=403,
        code="not_curator",
    )
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_callback(
            telegram_user_id=99,
            callback_data=f"proposal:{uuid4()}:approve",
        )
    )

    assert "нет прав" in reply.text.lower()


def test_already_decided_proposal_returns_current_status_without_buttons():
    api = FakeC1API()
    proposal_id = str(uuid4())
    api.errors["proposal.decide"] = C1APIError(
        status_code=409,
        code="already_decided",
    )
    api.responses["proposal.list"] = {
        "proposals": [
            {
                "proposal_id": proposal_id,
                "status": "approved",
            }
        ]
    }
    adapter = TelegramAdapter(api)

    reply = asyncio.run(
        adapter.handle_callback(
            telegram_user_id=42,
            callback_data=f"proposal:{proposal_id}:approve",
        )
    )

    assert len(api.calls) == 2
    assert api.calls[1] == (
        "proposal.list",
        {"channel_user_id": "42"},
    )
    assert not reply.buttons
    assert "approved" in reply.text.lower()


def test_aiogram_router_registers_message_and_callback_handlers():
    router = create_router(TelegramAdapter(FakeC1API()))

    assert router.name == "metabrain-telegram"
    assert len(router.message.handlers) == 1
    assert len(router.callback_query.handlers) == 1


@pytest.mark.parametrize(
    "text",
    ["/role", "/connect", "/unknown"],
)
def test_invalid_or_unsupported_command_does_not_call_api(text):
    api = FakeC1API()
    adapter = TelegramAdapter(api)

    reply = asyncio.run(adapter.handle_message(telegram_user_id=42, text=text))

    assert not api.calls
    assert reply.text
