"""Настройки LLM из кабинета (ADR-033): провайдер, адрес, модели, DLP и ссылка на ключ.

Хранятся одной строкой на установку и накладываются на env-конфиг при старте харнесса
(`overlay_settings`). Сам ключ лежит в vault (`harness_secrets`), здесь — только `api_key_ref`.
Политика (маршруты ролей, цены, лимиты, семейства моделей) остаётся в файле — кабинет её не трогает.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from psycopg import Connection

from src.common.config import Settings


PROVIDERS = ("deepseek", "local")
DEFAULT_MODEL_MAIN = "deepseek-flash"
DEFAULT_MODEL_JUDGE = "deepseek-v4-pro"


def default_api_base(provider: str) -> str:
    return "https://api.deepseek.com" if provider == "deepseek" else "http://vllm:8000/v1"


def env_key_ref(provider: str) -> str:
    return "env:DEEPSEEK_API_KEY" if provider == "deepseek" else "env:LOCAL_LLM_API_KEY"


def validate(provider: str, api_base: str, model_main: str, model_judge: str | None,
             dlp_enabled: bool) -> None:
    """Те же инварианты, что у коннектора: известный провайдер, http(s)-адрес, DLP для внешнего."""
    if provider not in PROVIDERS:
        raise ValueError("provider must be deepseek or local")
    try:
        parsed = urlsplit(api_base)
        ok = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("api_base must be an absolute HTTP(S) URL")
    if not (model_main or "").strip():
        raise ValueError("model_main is required")
    if model_judge is not None and not model_judge.strip():
        raise ValueError("model_judge must be non-empty or omitted")
    # Внешний провайдер без DLP коннектор не соберёт (egress без фильтра) — запрещаем здесь же.
    if provider != "local" and not dlp_enabled:
        raise ValueError("DLP must stay enabled for an external provider")


class LLMSettingsStore:
    def __init__(self, connection: Connection):
        self._c = connection
        with connection.transaction():
            connection.execute(
                Path(__file__).with_name("llm_settings.sql").read_text(encoding="utf-8"),
                prepare=False,
            )

    def get(self) -> dict[str, Any] | None:
        # В транзакции: на non-autocommit соединении голый SELECT оставил бы idle-in-transaction
        # с блокировкой llm_settings, и CREATE TABLE в другом соединении (runtime) завис бы навсегда.
        with self._c.transaction():
            row = self._c.execute(
                """
                SELECT provider, api_base, model_main, model_judge, api_key_ref, dlp_enabled, updated_at
                FROM llm_settings WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "provider": row[0], "api_base": row[1], "model_main": row[2], "model_judge": row[3],
            "api_key_ref": row[4], "dlp_enabled": row[5], "updated_at": row[6],
        }

    def put(self, *, provider: str, api_base: str, model_main: str, model_judge: str | None,
            api_key_ref: str | None, dlp_enabled: bool, updated_by: UUID | None) -> None:
        validate(provider, api_base, model_main, model_judge, dlp_enabled)
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO llm_settings
                    (id, provider, api_base, model_main, model_judge, api_key_ref, dlp_enabled, updated_at, updated_by)
                VALUES (1, %s, %s, %s, %s, %s, %s, now(), %s)
                ON CONFLICT (id) DO UPDATE SET
                    provider = EXCLUDED.provider, api_base = EXCLUDED.api_base,
                    model_main = EXCLUDED.model_main, model_judge = EXCLUDED.model_judge,
                    api_key_ref = EXCLUDED.api_key_ref, dlp_enabled = EXCLUDED.dlp_enabled,
                    updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by
                """,
                (provider, api_base, model_main, model_judge, api_key_ref, dlp_enabled, updated_by),
            )


def overlay_settings(connection: Connection, settings: Settings) -> Settings:
    """Накладываем строку из кабинета на env-конфиг. Нет строки — возвращаем как было."""
    row = LLMSettingsStore(connection).get()
    if row is None:
        return settings
    provider = row["provider"]
    api_key_ref = row["api_key_ref"] or env_key_ref(provider)
    return replace(
        settings,
        llm_provider=provider,
        llm_api_base=row["api_base"],
        llm_model_main=row["model_main"],
        llm_model_judge=row["model_judge"],
        llm_api_key_ref=api_key_ref,
        dlp_enabled=row["dlp_enabled"],
    )
