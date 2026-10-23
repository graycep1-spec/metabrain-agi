"""Харнесс не перехватывает чужого бота: токен с вебхуком — опрос не запускается (деплой рядом с другими ботами)."""

import asyncio
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from fastapi.testclient import TestClient

from src.common.secrets import EnvSecretStore
from src.harness.api import create_app
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, harness_env


def started(monkeypatch, env, webhook_url: str):
    polled = []

    async def get_me(self):
        return SimpleNamespace(id=1, username="metabrain_test_bot")

    async def get_webhook_info(self):
        return SimpleNamespace(url=webhook_url)

    async def start_polling(self, *bots, **kwargs):
        polled.append(bots)
        await asyncio.sleep(3600)

    monkeypatch.setattr(Bot, "get_me", get_me)
    monkeypatch.setattr(Bot, "get_webhook_info", get_webhook_info)
    monkeypatch.setattr(Dispatcher, "start_polling", start_polling)
    app = create_app(env.connection, api_token="guard-token", app_base_url="https://cabinet.example.test",
                     secret_store=EnvSecretStore({
                         "CONSENT_SIGNING_KEY": CONSENT_KEY,
                         "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                     }),
                     telegram_bot_token="123456:TEST-not-a-real-token")
    with TestClient(app):
        state = app.state.telegram_polling
        task = app.state.telegram_task
    return state, task, polled


def test_token_with_a_webhook_is_left_alone(monkeypatch):
    with harness_env() as env:
        state, task, polled = started(monkeypatch, env, "https://tsn.example/webhook")
        assert state == "webhook_conflict" and task is None and polled == []


def test_token_without_a_webhook_is_polled(monkeypatch):
    with harness_env() as env:
        state, task, polled = started(monkeypatch, env, "")
        assert state == "polling" and task is not None and len(polled) == 1
