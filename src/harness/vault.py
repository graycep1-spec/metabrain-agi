"""Хранилище секретов харнесса — vault-lite (ADR-028): доступы к системам, введённые в кабинете.

Секрет шифруется AES-256-GCM ключом `SECRET_STORE_KEY`; в AAD — `harness_id`, `secret_id` и система, поэтому
шифротекст нельзя переставить в другой харнесс или систему. Наружу — только ссылка `vault:<secret_id>`.
"""

import base64
import os
from pathlib import Path
import re
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from psycopg import Connection

from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.harness.store import Registration


VAULT_REF = re.compile(r"vault:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\Z")
MAX_SECRET = 4096
# Секреты самого харнесса: на них нельзя сослаться из подключения (иначе их унесёт адаптер в чужую систему).
SERVICE_SECRET = re.compile(
    r"(CONSENT_SIGNING_KEY|SECRET_STORE_KEY|VALIDATOR_SIGNING_KEY|HARNESS_API_TOKEN|.*_API_KEY|.*_DATABASE_URL"
    r"|.*_PASSWORD|TELEGRAM_.*|METABRAIN_.*|MCP_.*)"
)


class VaultUnavailable(RuntimeError):
    code = "secret_store_unavailable"


def load_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("SECRET_STORE_KEY must be 32 bytes in base64") from error
    if len(key) != 32:
        raise ValueError("SECRET_STORE_KEY must be 32 bytes in base64")
    return key


def _aad(harness_id: UUID, secret_id: UUID, system: str) -> bytes:
    return f"metabrain-vault:v1:{harness_id}:{secret_id}:{system}".encode()


class HarnessVault:
    def __init__(self, connection: Connection, key: bytes | None):
        self._c = connection
        self._aead = AESGCM(key) if key else None
        with connection.transaction():
            connection.execute(Path(__file__).with_name("vault.sql").read_text(encoding="utf-8"), prepare=False)

    @property
    def available(self) -> bool:
        return self._aead is not None

    def put(self, identity: Registration, *, system: str, secret: str) -> str:
        if self._aead is None:
            raise VaultUnavailable("SECRET_STORE_KEY is not configured")
        value = secret.strip() if isinstance(secret, str) else ""
        # В сообщениях об ошибке — никогда не само значение.
        if not value or len(value) > MAX_SECRET or any(ch in value for ch in "\r\n\x00"):
            raise ValueError(f"secret must be a single line of 1..{MAX_SECRET} characters")
        secret_id, nonce = uuid4(), os.urandom(12)
        ciphertext = self._aead.encrypt(nonce, value.encode("utf-8"), _aad(identity.harness_id, secret_id, system))
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO harness_secrets (secret_id, harness_id, company_id, system, nonce, ciphertext, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (secret_id, identity.harness_id, identity.company_id, system, nonce, ciphertext, identity.person_id),
            )
        return f"vault:{secret_id}"

    def owner(self, secret_ref: str) -> tuple[UUID, str] | None:
        """(harness_id, system) ссылки или None, если такой нет."""
        match = VAULT_REF.fullmatch(secret_ref) if isinstance(secret_ref, str) else None
        if match is None:
            return None
        row = self._c.execute(
            "SELECT harness_id, system FROM harness_secrets WHERE secret_id = %s", (UUID(match.group(1)),)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def resolve(self, secret_ref: str) -> str:
        match = VAULT_REF.fullmatch(secret_ref) if isinstance(secret_ref, str) else None
        if match is None:
            raise SecretNotFoundError("secret reference must use the vault:<id> format")
        if self._aead is None:
            raise SecretNotFoundError("secret store is not configured")
        secret_id = UUID(match.group(1))
        row = self._c.execute(
            "SELECT harness_id, system, nonce, ciphertext FROM harness_secrets WHERE secret_id = %s", (secret_id,)
        ).fetchone()
        if row is None:
            raise SecretNotFoundError("secret is not in the store")
        harness_id, system, nonce, ciphertext = row
        try:
            return self._aead.decrypt(bytes(nonce), bytes(ciphertext), _aad(harness_id, secret_id, system)).decode("utf-8")
        except InvalidTag as error:
            raise SecretNotFoundError("secret cannot be decrypted") from error


class SecretStore:
    """Ссылки `env:` — переменные окружения (ADR-014), `vault:` — хранилище харнесса (ADR-028)."""

    def __init__(self, env: EnvSecretStore, vault: HarnessVault | None):
        self._env = env
        self._vault = vault

    def resolve(self, secret_ref: str) -> str:
        if isinstance(secret_ref, str) and secret_ref.startswith("vault:"):
            if self._vault is None:
                raise SecretNotFoundError("secret store is not configured")
            return self._vault.resolve(secret_ref)
        return self._env.resolve(secret_ref)


def build_vault(connection: Connection, secret_store: EnvSecretStore) -> HarnessVault:
    """Без SECRET_STORE_KEY хранилище есть, но не принимает секреты (503 при вводе доступа)."""
    try:
        key = load_key(secret_store.resolve("env:SECRET_STORE_KEY"))
    except SecretNotFoundError:
        key = None
    return HarnessVault(connection, key)
