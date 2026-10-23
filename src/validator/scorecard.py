"""ScoreCard по C-4: формат, подпись Ed25519 по JCS (ADR-008) и проверка потребителем."""

import base64
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import rfc8785


Verdict = Literal["pass", "fail"]
PROFILES = ("answer", "passport", "adapter", "action", "skill")


class ScoreCardRejected(PermissionError):
    """ScoreCard нельзя принять: с точки зрения потребителя он unsigned (C-4)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def artifact_hash(artifact: Mapping[str, Any]) -> str:
    return sha256(rfc8785.dumps(artifact)).hexdigest()


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def public_key_b64(public_key: Ed25519PublicKey) -> str:
    return _b64url(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw))


def key_id_for(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
    return "ed25519:" + sha256(raw).hexdigest()[:16]


def load_private_key(encoded: str) -> Ed25519PrivateKey:
    """Ключ — raw 32 байта в base64, как у ключа согласий (src/harness/proposals.py)."""
    try:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded, validate=True))
    except (ValueError, TypeError) as error:
        raise ValueError("invalid Ed25519 validator signing key") from error


def load_public_key(encoded: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_b64url_decode(encoded))


@dataclass(frozen=True)
class Context:
    company_id: UUID
    harness_id: UUID
    employee_id: str | None = None
    job_id: str | None = None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Context":
        if not isinstance(value, Mapping):
            raise ValueError("context must be an object")
        try:
            company_id = UUID(str(value["company_id"]))
            harness_id = UUID(str(value["harness_id"]))
        except (KeyError, ValueError) as error:
            raise ValueError("context.company_id and context.harness_id must be UUIDs") from error
        employee_id = value.get("employee_id")
        job_id = value.get("job_id")
        for name, item in (("employee_id", employee_id), ("job_id", job_id)):
            if item is not None and (not isinstance(item, str) or not item):
                raise ValueError(f"context.{name} must be a non-empty string")
        return cls(company_id, harness_id, employee_id, job_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_id": str(self.company_id),
            "harness_id": str(self.harness_id),
            "employee_id": self.employee_id,
            "job_id": self.job_id,
        }


@dataclass(frozen=True)
class CheckResult:
    name: str
    verdict: Verdict
    metrics: dict[str, Any] = field(default_factory=dict)
    log_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "metrics": dict(self.metrics),
            "log_ref": self.log_ref,
        }


@dataclass(frozen=True)
class ScoreCard:
    scorecard_id: UUID
    artifact_hash: str
    profile: str
    profile_set: str
    context: Context
    verdict: Verdict
    checks: tuple[CheckResult, ...]
    issued_at: datetime
    key_id: str
    signature: str = ""

    def signed_payload(self) -> dict[str, Any]:
        """Все поля, кроме signature (C-4)."""
        return {
            "scorecard_id": str(self.scorecard_id),
            "artifact_hash": self.artifact_hash,
            "profile": self.profile,
            "profile_set": self.profile_set,
            "context": self.context.to_dict(),
            "verdict": self.verdict,
            "checks": [check.to_dict() for check in self.checks],
            "issued_at": self.issued_at.astimezone(timezone.utc).isoformat(),
            "key_id": self.key_id,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.signed_payload(), "signature": self.signature}


class ScoreCardSigner:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._private_key = private_key
        self.public_key = private_key.public_key()
        self.key_id = key_id_for(self.public_key)

    def sign(self, unsigned: ScoreCard) -> ScoreCard:
        if unsigned.key_id != self.key_id:
            raise ValueError("scorecard key_id does not match the signer")
        signature = self._private_key.sign(rfc8785.dumps(unsigned.signed_payload()))
        return ScoreCard(**{**unsigned.__dict__, "signature": _b64url(signature)})

    def keys(self) -> list[dict[str, str]]:
        return [
            {
                "key_id": self.key_id,
                "alg": "Ed25519",
                "public_key": public_key_b64(self.public_key),
            }
        ]


def verify_scorecard(
    scorecard: Mapping[str, Any],
    keys: Mapping[str, Ed25519PublicKey],
    *,
    artifact: Mapping[str, Any],
    harness_id: UUID | str,
    require_pass: bool = True,
) -> None:
    """Проверка потребителем перед выпуском/применением артефакта.

    Любое несовпадение — отказ: подпись, `artifact_hash`, `context.harness_id` (C-4).
    """
    if not isinstance(scorecard, Mapping):
        raise ScoreCardRejected("malformed")
    signature = scorecard.get("signature")
    if not isinstance(signature, str) or not signature:
        raise ScoreCardRejected("unsigned")
    public_key = keys.get(scorecard.get("key_id"))
    if public_key is None:
        raise ScoreCardRejected("unknown_key")

    payload = {name: value for name, value in scorecard.items() if name != "signature"}
    try:
        public_key.verify(_b64url_decode(signature), rfc8785.dumps(payload))
    except (InvalidSignature, ValueError, TypeError) as error:
        raise ScoreCardRejected("bad_signature") from error

    if scorecard.get("artifact_hash") != artifact_hash(artifact):
        raise ScoreCardRejected("artifact_mismatch")
    context = scorecard.get("context")
    if not isinstance(context, Mapping) or context.get("harness_id") != str(harness_id):
        raise ScoreCardRejected("harness_mismatch")
    if require_pass and scorecard.get("verdict") != "pass":
        raise ScoreCardRejected("verdict_fail")
