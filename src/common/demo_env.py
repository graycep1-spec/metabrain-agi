"""Генератор `.env` для демо: заполняет ключи и пароли, которые иначе собирают вручную (T-28).

    python -m src.common.demo_env            # .env.example → .env (существующий .env не трогает)
    python -m src.common.demo_env --force    # перезаписать .env

Заполняет только пустые значения и заглушки `changeme*`/`replace-*`; DeepSeek, Telegram и адрес кабинета остаются вам.
Ключ согласий и его публичная половина генерируются парой — мета-мозг проверяет подписи харнесса (ADR-008).
Токены менеджеров демо-ландшафта печатаются в консоль: их вводят в кабинете при подключении систем.
"""

import argparse
import base64
import json
from pathlib import Path
import secrets
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def ed25519_pair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(private).decode(), base64.b64encode(public).decode()


def generated() -> tuple[dict[str, str], dict[str, str]]:
    """(значения для .env, токены менеджеров для подключения в кабинете)."""
    consent_private, consent_public = ed25519_pair()
    managers = {"manager-a": f"demo-a-{secrets.token_urlsafe(18)}", "manager-b": f"demo-b-{secrets.token_urlsafe(18)}"}
    systems = ["crm", "stock", "pricing"]
    values = {
        "HARNESS_API_TOKEN": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "HARNESS_DB_PASSWORD": secrets.token_urlsafe(24),
        "METABRAIN_DB_PASSWORD": secrets.token_urlsafe(24),
        "VALIDATOR_SIGNING_KEY": ed25519_pair()[0],
        "CONSENT_SIGNING_KEY": consent_private,
        "CONSENT_PUBLIC_KEY": consent_public,
        "SECRET_STORE_KEY": base64.b64encode(secrets.token_bytes(32)).decode(),
        "DEMO_LANDSCAPE_TOKENS": json.dumps({token: {"user": user, "systems": systems} for user, token in managers.items()}),
    }
    return values, managers


def is_placeholder(value: str) -> bool:
    return not value or value.startswith("changeme") or value.startswith("replace-")


def fill(template: str, values: dict[str, str]) -> str:
    lines = []
    for line in template.splitlines():
        name, sep, current = line.partition("=")
        if sep and not line.lstrip().startswith("#") and name in values and is_placeholder(current.strip()):
            # Одинарные кавычки: значение берётся как есть (JSON с кавычками, base64 с «=» и «/»).
            line = f"{name}='{values[name]}'"
        lines.append(line)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Заполнить .env для демо")
    parser.add_argument("--template", default=".env.example")
    parser.add_argument("--output", default=".env")
    parser.add_argument("--force", action="store_true", help="перезаписать существующий файл")
    args = parser.parse_args(argv)
    output = Path(args.output)
    if output.exists() and not args.force:
        print(f"{output} уже есть — не трогаю. Перезаписать: --force", file=sys.stderr)
        return 1
    values, managers = generated()
    output.write_text(fill(Path(args.template).read_text(encoding="utf-8"), values), encoding="utf-8")
    print(f"Записано: {output}. Осталось вписать: DEEPSEEK_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_USERNAME, "
          "TELEGRAM_OIDC_CLIENT_ID, TELEGRAM_OIDC_CLIENT_SECRET, WEB_BASE_URL (https).")
    print("Токены менеджеров — вводить в кабинете при подключении crm, stock, pricing:")
    for user, token in managers.items():
        print(f"  {user}: {token}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
