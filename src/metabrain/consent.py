"""Проверка consent_token в мета-мозге по публичному ключу куратор-гейта (C-1, C-5, ADR-008)."""

import base64
from collections.abc import Mapping
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import rfc8785


# Подписываются ровно эти поля — как в src/harness/proposals.py `_token_payload`.
SIGNED_FIELDS = (
    "token_id", "harness_id", "proposal_id", "subject_hash", "scope",
    "issued_by", "surface", "issued_at", "exp", "signing_public_key", "key_certificate",
)


class ConsentRejected(PermissionError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_public_key(encoded: str) -> Ed25519PublicKey:
    """Публичный ключ — raw 32 байта в base64 (стандартном или url-safe)."""
    value = encoded.strip()
    padding = "=" * (-len(value) % 4)
    try:
        raw = base64.b64decode(value + padding, altchars=b"-_" if ("-" in value or "_" in value) else None, validate=True)
        return Ed25519PublicKey.from_public_bytes(raw)
    except (ValueError, TypeError) as error:
        raise ValueError("invalid Ed25519 consent public key") from error


def subject_hash(subject: Mapping[str, Any]) -> str:
    return sha256(rfc8785.dumps(subject)).hexdigest()


def verify_consent(
    token: Any,
    public_key: Ed25519PublicKey | None,
    *,
    harness_id: str,
    scope: str,
    subject: Mapping[str, Any],
    now: datetime | None = None,
) -> str:
    """Возвращает token_id. Одноразовость проверяет вызывающий (по token_id)."""
    if public_key is None:
        raise ConsentRejected("consent_key_not_configured")
    if not isinstance(token, Mapping):
        raise ConsentRejected("consent_required")
    if any(not isinstance(token.get(name), str) or not token.get(name) for name in SIGNED_FIELDS):
        raise ConsentRejected("malformed_consent")
    signature = token.get("signature")
    if not isinstance(signature, str) or not signature:
        raise ConsentRejected("unsigned_consent")

    payload = {name: token[name] for name in SIGNED_FIELDS}
    try:
        signing_key = load_public_key(token["signing_public_key"])
        certificate = {
            "harness_id": token["harness_id"],
            "signing_public_key": token["signing_public_key"],
        }
        public_key.verify(
            _decode_signature(token["key_certificate"]),
            rfc8785.dumps(certificate),
        )
        signing_key.verify(
            _decode_signature(signature),
            rfc8785.dumps(payload),
        )
    except (InvalidSignature, ValueError, TypeError) as error:
        raise ConsentRejected("bad_consent_signature") from error

    if token["harness_id"] != harness_id:
        raise ConsentRejected("consent_other_harness")
    if token["scope"] != scope:
        raise ConsentRejected("consent_wrong_scope")
    if token["subject_hash"] != subject_hash(subject):
        raise ConsentRejected("consent_other_subject")
    try:
        expires = datetime.fromisoformat(token["exp"])
    except ValueError as error:
        raise ConsentRejected("malformed_consent") from error
    if expires.tzinfo is None:
        raise ConsentRejected("malformed_consent")
    if (now or datetime.now(timezone.utc)) >= expires:
        raise ConsentRejected("consent_expired")
    return token["token_id"]


def _decode_signature(signature: str) -> bytes:
    padding = "=" * (-len(signature) % 4)
    return base64.b64decode(signature + padding, altchars=b"-_", validate=True)
