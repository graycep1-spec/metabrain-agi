import pytest

from src.common.config import ConfigurationError, load_settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError


def test_config_uses_provider_specific_defaults_and_secret_references():
    settings = load_settings({"SERVICE_NAME": "harness"})

    assert settings.service_name == "harness"
    assert settings.llm_provider == "deepseek"
    assert settings.llm_api_base == "https://api.deepseek.com"
    assert settings.llm_model_main == "deepseek-flash"
    assert settings.llm_model_judge == "deepseek-v4-pro"
    assert settings.llm_api_key_ref == "env:DEEPSEEK_API_KEY"
    assert settings.validator_signing_key_ref == "env:VALIDATOR_SIGNING_KEY"
    assert settings.consent_signing_key_ref == "env:CONSENT_SIGNING_KEY"
    assert settings.consent_public_key_ref == "env:CONSENT_PUBLIC_KEY"
    assert settings.consent_token_ttl_seconds == 900
    assert settings.telegram_bot_token_ref == "env:TELEGRAM_BOT_TOKEN"


def test_config_selects_local_provider_from_environment():
    settings = load_settings(
        {
            "SERVICE_NAME": "harness",
            "LLM_PROVIDER": "local",
            "LOCAL_LLM_API_BASE": "http://vllm:8000/v1",
        }
    )

    assert settings.llm_provider == "local"
    assert settings.llm_api_base == "http://vllm:8000/v1"
    assert settings.llm_api_key_ref == "env:LOCAL_LLM_API_KEY"


def test_config_reads_consent_token_ttl_from_environment():
    settings = load_settings({"SERVICE_NAME": "harness", "CONSENT_TOKEN_TTL_SECONDS": "3600"})

    assert settings.consent_token_ttl_seconds == 3600


def test_config_does_not_copy_secret_values_into_settings():
    secret = "do-not-log-this-value"
    settings = load_settings(
        {"SERVICE_NAME": "harness", "DEEPSEEK_API_KEY": secret}
    )

    assert secret not in repr(settings)


@pytest.mark.parametrize(
    ("environment", "message"),
    [
        ({}, "SERVICE_NAME"),
        ({"SERVICE_NAME": "harness", "LLM_PROVIDER": "unknown"}, "LLM_PROVIDER"),
        ({"SERVICE_NAME": "harness", "PORT": "invalid"}, "PORT"),
        ({"SERVICE_NAME": "harness", "DLP_ENABLED": "sometimes"}, "DLP_ENABLED"),
        ({"SERVICE_NAME": "harness", "CONSENT_TOKEN_TTL_SECONDS": "0"}, "CONSENT_TOKEN_TTL_SECONDS"),
        ({"SERVICE_NAME": "harness", "CONSENT_TOKEN_TTL_SECONDS": "86401"}, "CONSENT_TOKEN_TTL_SECONDS"),
        (
            {"SERVICE_NAME": "harness", "APP_BASE_URL": "file:///tmp/app"},
            "APP_BASE_URL",
        ),
    ],
)
def test_config_rejects_invalid_environment(environment, message):
    with pytest.raises(ConfigurationError, match=message):
        load_settings(environment)


def test_secret_store_resolves_environment_reference_without_changing_value():
    secret = "secret-value-with-spaces "
    store = EnvSecretStore({"DEEPSEEK_API_KEY": secret})

    assert store.resolve("env:DEEPSEEK_API_KEY") == secret


@pytest.mark.parametrize(
    "secret_ref",
    [
        "",
        "DEEPSEEK_API_KEY",
        "file:secret",
        "env:",
        "env:lowercase",
        "env:A/B",
        None,
    ],
)
def test_secret_store_rejects_invalid_reference(secret_ref):
    store = EnvSecretStore({})

    with pytest.raises(SecretNotFoundError):
        store.resolve(secret_ref)


def test_secret_store_fails_closed_for_missing_or_empty_secret():
    for value in ("", "   "):
        store = EnvSecretStore({"VALIDATOR_SIGNING_KEY": value})
        with pytest.raises(SecretNotFoundError, match="env:VALIDATOR_SIGNING_KEY"):
            store.resolve("env:VALIDATOR_SIGNING_KEY")


def test_missing_secret_error_never_contains_secret_values():
    store = EnvSecretStore({"VALIDATOR_SIGNING_KEY": ""})
    secret = "do-not-leak"

    with pytest.raises(SecretNotFoundError) as exc_info:
        store.resolve(f"env:VALIDATOR_SIGNING_KEY{secret}")

    assert secret not in str(exc_info.value)
