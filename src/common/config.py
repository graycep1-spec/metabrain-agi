from dataclasses import dataclass
import os
from typing import Mapping
from urllib.parse import urlsplit


class ConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    service_name: str
    port: int
    app_base_url: str
    log_level: str
    llm_provider: str
    llm_api_base: str
    llm_model_main: str
    llm_model_judge: str | None
    llm_api_key_ref: str
    validator_signing_key_ref: str
    consent_signing_key_ref: str
    consent_public_key_ref: str
    consent_token_ttl_seconds: int
    telegram_bot_token_ref: str
    qdrant_url: str
    nats_url: str
    dlp_enabled: bool


def _http_url(name: str, value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ConfigurationError(f"{name} must be an absolute HTTP(S) URL")
    return value


def _service_url(name: str, value: str, schemes: set[str]) -> str:
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in schemes and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        choices = ", ".join(sorted(schemes))
        raise ConfigurationError(f"{name} must be an absolute URL using {choices}")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> Settings:
    source = os.environ if environ is None else environ

    service_name = source.get("SERVICE_NAME", "").strip()
    if not service_name:
        raise ConfigurationError("SERVICE_NAME is required")

    try:
        port = int(source.get("PORT", "8080"))
    except ValueError as error:
        raise ConfigurationError("PORT must be an integer between 1 and 65535") from error
    if not 1 <= port <= 65535:
        raise ConfigurationError("PORT must be an integer between 1 and 65535")

    log_level = source.get("LOG_LEVEL", "info").strip().upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")

    provider = source.get("LLM_PROVIDER", "deepseek").strip().lower()
    if provider == "deepseek":
        llm_api_base = source.get("DEEPSEEK_API_BASE", "https://api.deepseek.com").strip()
        llm_api_key_ref = "env:DEEPSEEK_API_KEY"
    elif provider == "local":
        llm_api_base = source.get("LOCAL_LLM_API_BASE", "http://vllm:8000/v1").strip()
        llm_api_key_ref = "env:LOCAL_LLM_API_KEY"
    else:
        raise ConfigurationError("LLM_PROVIDER must be deepseek or local")

    if not llm_api_base:
        raise ConfigurationError("LLM API base URL is required")
    llm_api_base = _http_url(
        "DEEPSEEK_API_BASE" if provider == "deepseek" else "LOCAL_LLM_API_BASE",
        llm_api_base,
    )

    dlp_value = source.get("DLP_ENABLED", "true").strip().lower()
    if dlp_value not in {"true", "false"}:
        raise ConfigurationError("DLP_ENABLED must be true or false")

    try:
        consent_token_ttl_seconds = int(source.get("CONSENT_TOKEN_TTL_SECONDS", "900"))
    except ValueError as error:
        raise ConfigurationError("CONSENT_TOKEN_TTL_SECONDS must be an integer from 1 to 86400") from error
    if not 1 <= consent_token_ttl_seconds <= 86400:
        raise ConfigurationError("CONSENT_TOKEN_TTL_SECONDS must be an integer from 1 to 86400")

    return Settings(
        service_name=service_name,
        port=port,
        app_base_url=_http_url(
            "APP_BASE_URL", source.get("APP_BASE_URL", "http://localhost:8080").strip()
        ),
        log_level=log_level,
        llm_provider=provider,
        llm_api_base=llm_api_base,
        llm_model_main=source.get("LLM_MODEL_MAIN", "deepseek-flash").strip(),
        llm_model_judge=source.get("LLM_MODEL_JUDGE", "deepseek-v4-pro").strip() or None,
        llm_api_key_ref=llm_api_key_ref,
        validator_signing_key_ref="env:VALIDATOR_SIGNING_KEY",
        consent_signing_key_ref="env:CONSENT_SIGNING_KEY",
        consent_public_key_ref="env:CONSENT_PUBLIC_KEY",
        consent_token_ttl_seconds=consent_token_ttl_seconds,
        telegram_bot_token_ref="env:TELEGRAM_BOT_TOKEN",
        qdrant_url=_http_url(
            "QDRANT_URL", source.get("QDRANT_URL", "http://qdrant:6333").strip()
        ),
        nats_url=_service_url(
            "NATS_URL", source.get("NATS_URL", "nats://nats:4222").strip(), {"nats", "tls"}
        ),
        dlp_enabled=dlp_value == "true",
    )
