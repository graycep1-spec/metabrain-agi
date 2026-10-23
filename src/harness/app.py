"""Harness service entry point: PostgreSQL-backed C-1 API and Telegram adapter."""

from datetime import timedelta
import os

import psycopg

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.harness.api import create_app
from src.harness.wiring import build_extensions, build_runtime_thread, build_web_auth
from src.landscape.registry import parse_registry


def main() -> None:
    import uvicorn

    try:
        settings = load_settings()
        secret_store = EnvSecretStore()
        database_url = secret_store.resolve("env:HARNESS_DATABASE_URL")
        api_token = secret_store.resolve("env:HARNESS_API_TOKEN")
        connection = psycopg.connect(database_url)
    except (ValueError, KeyError, SecretNotFoundError) as error:
        raise SystemExit(str(error)) from error

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() or None
    app = create_app(
        connection,
        api_token=api_token,
        # Ссылки в кабинет из Telegram (`/connect`) ведут на веб-кабинет, а не на API харнесса.
        app_base_url=os.environ.get("WEB_BASE_URL", "").strip() or settings.app_base_url,
        telegram_bot_username=os.environ.get(
            "TELEGRAM_BOT_USERNAME", "metabrain_bot"
        ),
        llm_provider=settings.llm_provider,
        secret_store=secret_store,
        consent_signing_key_ref=settings.consent_signing_key_ref,
        consent_public_key_ref=settings.consent_public_key_ref,
        consent_token_ttl=timedelta(seconds=settings.consent_token_ttl_seconds),
        telegram_bot_token=bot_token,
        telegram_api_base_url=os.environ.get(
            "TELEGRAM_API_BASE_URL", "http://127.0.0.1:8080"
        ),
        # T-13/T-19: цифровые сотрудники и Исследователь (без VALIDATOR_URL — поведение T-35).
        **build_extensions(connection, settings, secret_store),
        system_registry=parse_registry(os.environ.get("LANDSCAPE_SYSTEMS")),
        # Настройки LLM в кабинете (ADR-033): показываем всегда, правим — только при флаге установки.
        settings=settings,
        llm_settings_editable=os.environ.get("LLM_SETTINGS_EDITABLE", "").strip().lower() in {"true", "1", "on", "yes"},
        # T-31: вход в веб-кабинет по Telegram OIDC (ADR-025).
        **build_web_auth(connection, secret_store),
    )
    runtime: dict = {}

    @app.on_event("startup")
    async def start_runtime() -> None:
        # Работы по расписанию (T-15 + рой T-14); отчёты — в журнал и куратору в Telegram, если бот есть.
        import asyncio

        from src.workforce.reports import telegram_sender

        bot = app.state.telegram_bot
        send = telegram_sender(bot, asyncio.get_running_loop()) if bot is not None else None
        thread = build_runtime_thread(database_url, settings, secret_store, send=send)
        if thread is not None:
            thread.start()
            runtime["thread"] = thread

    @app.on_event("shutdown")
    async def stop_runtime() -> None:
        if "thread" in runtime:
            runtime["thread"].stop()

    try:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=settings.port,
            log_level=settings.log_level.lower(),
            access_log=False,
        )
    finally:
        connection.close()


if __name__ == "__main__":
    main()
