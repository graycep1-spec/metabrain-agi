"""Генератор `.env` для демо: ключи совместимы с загрузчиками сервисов, заполняются только заглушки."""

import base64
from pathlib import Path

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from src.common.demo_env import fill, generated, main
from src.demo_landscape.app import parse_tokens
from src.harness.vault import load_key
from src.metabrain.consent import load_public_key
from src.validator.scorecard import load_private_key


def parsed(text: str) -> dict[str, str]:
    """Как читает Compose: KEY=value, одинарные кавычки снимаются."""
    result = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.startswith("#"):
            result[name] = value[1:-1] if value.startswith("'") and value.endswith("'") else value
    return result


def test_generated_keys_load_in_every_service_and_consent_pair_matches():
    values, managers = generated()
    load_private_key(values["VALIDATOR_SIGNING_KEY"])
    consent = load_private_key(values["CONSENT_SIGNING_KEY"]).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert base64.b64encode(consent).decode() == values["CONSENT_PUBLIC_KEY"]
    load_public_key(values["CONSENT_PUBLIC_KEY"])
    assert len(load_key(values["SECRET_STORE_KEY"])) == 32
    grants = parse_tokens(values["DEMO_LANDSCAPE_TOKENS"])
    assert {g.user for g in grants.values()} == {"manager-a", "manager-b"} and set(grants) == set(managers.values())


def test_only_placeholders_are_filled_and_existing_env_is_kept(tmp_path):
    template = "\n".join([
        "# комментарий=не трогать", "POSTGRES_PASSWORD=changeme", "HARNESS_API_TOKEN=replace-with-a-random-service-token",
        "CONSENT_SIGNING_KEY=", "DEEPSEEK_API_KEY=", "VALIDATOR_SIGNING_KEY=already-set", "WEB_BASE_URL=http://localhost:8081",
    ])
    values, _ = generated()
    env = parsed(fill(template, values))
    assert env["POSTGRES_PASSWORD"] == values["POSTGRES_PASSWORD"] and env["CONSENT_SIGNING_KEY"] == values["CONSENT_SIGNING_KEY"]
    assert env["HARNESS_API_TOKEN"] == values["HARNESS_API_TOKEN"]
    assert env["DEEPSEEK_API_KEY"] == "" and env["VALIDATOR_SIGNING_KEY"] == "already-set"
    assert env["WEB_BASE_URL"] == "http://localhost:8081"

    source, output = tmp_path / "template", tmp_path / ".env"
    source.write_text(template, encoding="utf-8")
    assert main(["--template", str(source), "--output", str(output)]) == 0
    first = output.read_text(encoding="utf-8")
    assert main(["--template", str(source), "--output", str(output)]) == 1          # без --force не перезаписывает
    assert output.read_text(encoding="utf-8") == first


def test_real_env_example_gets_every_generated_value(tmp_path):
    values, _ = generated()
    env = parsed(fill(Path(".env.example").read_text(encoding="utf-8"), values))
    assert {name: env[name] for name in values} == values
