"""ADR-033: настройка LLM в кабинете — валидация, хранилище, наложение на конфиг, операции и экран."""

import os
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from src.common.config import Settings
from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.llm_settings import LLMSettingsStore, overlay_settings, validate
from src.harness.vault import SecretStore
from src.harness.wiring import build_connector
from src.metabrain.policy import load_policy
from src.web.app import create_app as create_web, http_harness
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, harness_env
from tests.test_web_auth import API_TOKEN, FakeTelegram, claims, make_auth, telegram_id


KEY = os.urandom(32)
FORM = {"content-type": "application/x-www-form-urlencoded"}


def settings_obj(**over) -> Settings:
    base = dict(
        service_name="harness", port=8080, app_base_url="http://localhost:8080", log_level="INFO",
        llm_provider="deepseek", llm_api_base="https://api.deepseek.com",
        llm_model_main="deepseek-flash", llm_model_judge="deepseek-v4-pro",
        llm_api_key_ref="env:DEEPSEEK_API_KEY", validator_signing_key_ref="env:VALIDATOR_SIGNING_KEY",
        consent_signing_key_ref="env:CONSENT_SIGNING_KEY", consent_public_key_ref="env:CONSENT_PUBLIC_KEY",
        consent_token_ttl_seconds=900, telegram_bot_token_ref="env:TELEGRAM_BOT_TOKEN",
        qdrant_url="http://qdrant:6333", nats_url="nats://nats:4222", dlp_enabled=True,
    )
    base.update(over)
    return Settings(**base)


def logged_in(env, *, editable: bool, settings: Settings | None = None, test_http: httpx.Client | None = None):
    telegram = FakeTelegram()
    auth = make_auth(env, telegram)
    harness = TestClient(create_harness(
        env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
        secret_store=EnvSecretStore({"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                                     "DEEPSEEK_API_KEY": "sk-env-key"}),
        web_auth=auth, workforce=env.workforce, researcher=env.researcher, vault=env.vault,
        settings=settings or settings_obj(), llm_settings_editable=editable, llm_http_client=test_http,
    ))
    web = TestClient(create_web(http_harness("http://harness", API_TOKEN, client=harness),
                                web_base_url="https://cabinet.example.test"), base_url="https://cabinet.example.test")
    start = web.get("/login/start", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))
    assert web.get(f"/auth/callback?code=code&state={query['state']}").status_code == 200
    return web


def csrf(web) -> str:
    return web.cookies.get("mb_session").split(".", 1)[1]


def save(web, **fields):
    return web.post("/settings", content=urlencode({"csrf_token": csrf(web), **fields}), headers=FORM,
                    follow_redirects=False)


# --- unit: валидация, хранилище, наложение, коннектор ----------------------------------------

@pytest.mark.parametrize("args", [
    ("sky", "https://api.x", "m", None, True),                   # неизвестный провайдер
    ("deepseek", "ftp://x", "m", None, True),                    # не http(s)
    ("deepseek", "https://api.x", "", None, True),               # пустая основная модель
    ("deepseek", "https://api.deepseek.com", "m", None, False),  # внешний провайдер без DLP
])
def test_validate_rejects_bad_settings(args):
    with pytest.raises(ValueError):
        validate(*args)


def test_store_round_trip_keeps_a_single_row():
    with harness_env(vault_key=KEY) as env:
        store = LLMSettingsStore(env.connection)
        assert store.get() is None
        store.put(provider="local", api_base="http://vllm:8000/v1", model_main="qwen", model_judge=None,
                  api_key_ref=None, dlp_enabled=False, updated_by=None)
        store.put(provider="deepseek", api_base="https://api.deepseek.com", model_main="deepseek-flash",
                  model_judge="deepseek-v4-pro", api_key_ref="vault:x", dlp_enabled=True, updated_by=None)
        row = store.get()
        assert row["provider"] == "deepseek" and row["api_key_ref"] == "vault:x"
        assert env.connection.execute("SELECT count(*) FROM llm_settings").fetchone()[0] == 1


def test_overlay_applies_cabinet_row_over_env_config():
    with harness_env(vault_key=KEY) as env:
        base = settings_obj()
        assert overlay_settings(env.connection, base) == base         # нет строки — как было
        LLMSettingsStore(env.connection).put(
            provider="local", api_base="http://vllm:8000/v1", model_main="qwen-main", model_judge="qwen-judge",
            api_key_ref=None, dlp_enabled=False, updated_by=None)
        merged = overlay_settings(env.connection, base)
        assert merged.llm_provider == "local" and merged.llm_api_base == "http://vllm:8000/v1"
        assert merged.llm_model_main == "qwen-main" and merged.llm_model_judge == "qwen-judge"
        assert merged.dlp_enabled is False and merged.llm_api_key_ref == "env:LOCAL_LLM_API_KEY"


def test_build_connector_uses_model_names_and_the_vault_key():
    with harness_env(vault_key=KEY) as env:
        me = env.register("a")
        ref = env.vault.put(me, system="llm", secret="sk-local-123")
        settings = settings_obj(llm_provider="local", llm_api_base="http://vllm:8000/v1",
                                llm_model_main="qwen-main", llm_model_judge="qwen-judge", llm_api_key_ref=ref)
        store = SecretStore(EnvSecretStore(), env.vault)
        connector = build_connector(load_policy(), settings, env.connection, store)
        assert connector.provider_for("developer").model == "qwen-main"   # основная работа
        assert connector.provider_for("critic").model == "qwen-judge"     # судья
        assert store.resolve(connector.provider_for("developer").api_key_ref) == "sk-local-123"


# --- операции и экран кабинета ---------------------------------------------------------------

def test_settings_screen_shows_config_and_hides_form_when_locked():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        web = logged_in(env, editable=False)
        page = web.get("/settings")
        assert page.status_code == 200
        assert "api.deepseek.com" in page.text and "deepseek-flash" in page.text
        assert 'action="/settings"' not in page.text            # формы сохранения нет
        assert "Редактирование из кабинета выключено" in page.text


def test_save_is_locked_without_the_flag():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        web = logged_in(env, editable=False)
        resp = save(web, provider="local", api_base="http://vllm:8000/v1", model_main="qwen", dlp_enabled="")
        assert resp.status_code == 403 and "выключен" in resp.text.lower()
        assert env.connection.execute("SELECT count(*) FROM llm_settings").fetchone()[0] == 0


def test_editable_save_stores_config_and_key_in_vault():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        web = logged_in(env, editable=True)
        resp = save(web, provider="local", api_base="http://vllm:8000/v1", model_main="qwen-main",
                    model_judge="qwen-judge", api_key="sk-secret-xyz", dlp_enabled="")
        assert resp.status_code == 303 and resp.headers["location"] == "/settings?notice=llm_saved"

        row = env.connection.execute(
            "SELECT provider, api_base, model_main, model_judge, api_key_ref, dlp_enabled FROM llm_settings"
        ).fetchone()
        assert row[:4] == ("local", "http://vllm:8000/v1", "qwen-main", "qwen-judge")
        assert row[4].startswith("vault:") and row[5] is False
        # ключ зашифрован в хранилище; в журнале — только 'set', самого ключа нигде нет
        assert env.connection.execute("SELECT count(*) FROM harness_secrets WHERE system='llm'").fetchone()[0] == 1
        journal = env.connection.execute(
            "SELECT payload::text FROM journal_entries WHERE kind='llm_settings.updated'"
        ).fetchone()[0]
        assert '"key": "set"' in journal and "sk-secret-xyz" not in journal
        # экран теперь показывает источник «кабинет» и что ключ задан (хранилище)
        page = web.get("/settings")
        assert "qwen-main" in page.text and "хранилище" in page.text


def test_dlp_off_for_external_provider_is_rejected():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        web = logged_in(env, editable=True)
        resp = save(web, provider="deepseek", api_base="https://api.deepseek.com", model_main="deepseek-flash",
                    model_judge="deepseek-v4-pro", dlp_enabled="")
        assert resp.status_code == 400 and "Проверьте поля" in resp.text
        assert env.connection.execute("SELECT count(*) FROM llm_settings").fetchone()[0] == 0


def test_connection_test_reports_ok_and_missing_models():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        ok_http = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]})))
        web = logged_in(env, editable=True, test_http=ok_http)
        page = web.post("/settings/test", content=urlencode({"csrf_token": csrf(web)}), headers=FORM,
                        follow_redirects=False)
        assert page.status_code == 200 and "подключение ок" in page.text

        bad_http = httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"data": [{"id": "some-other-model"}]})))
        web2 = logged_in(env, editable=True, test_http=bad_http)
        page2 = web2.post("/settings/test", content=urlencode({"csrf_token": csrf(web2)}), headers=FORM,
                          follow_redirects=False)
        assert page2.status_code == 200 and "нет подключения" in page2.text and "Модели не найдены" in page2.text
