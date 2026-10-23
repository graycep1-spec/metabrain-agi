from datetime import datetime, timezone
import base64
import json
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat
from fastapi.testclient import TestClient
import pytest

from src.common.secrets import EnvSecretStore
from src.validator.app import build_validator, create_app
from src.validator.scorecard import (
    ScoreCardSigner,
    artifact_hash,
    load_public_key,
    verify_scorecard,
)
from src.validator.service import (
    NOT_IMPLEMENTED,
    PROFILE_CHECKS,
    CheckOutcome,
    InvalidRequest,
    SigningUnavailable,
    Validator,
)


HARNESS_ID = str(uuid4())
CONTEXT = {"company_id": str(uuid4()), "harness_id": HARNESS_ID}
ARTIFACT = {"text": "ok", "claims": [], "sources": []}
FIXED_NOW = datetime(2026, 10, 20, 9, 30, tzinfo=timezone.utc)


def passing(profile):
    return {(profile, name): (lambda artifact, ctx: CheckOutcome("pass", {"n": 1})) for name in PROFILE_CHECKS[profile]}


@pytest.fixture
def signer():
    return ScoreCardSigner(Ed25519PrivateKey.generate())


def test_all_checks_pass_gives_signed_pass_with_profile_checks_in_order(signer):
    card = Validator(signer, passing("answer"), clock=lambda: FIXED_NOW).validate(ARTIFACT, "answer", CONTEXT)

    assert card.verdict == "pass"
    assert [c.name for c in card.checks] == ["grounding", "pii_dlp", "judge"]
    assert card.artifact_hash == artifact_hash(ARTIFACT)
    assert card.issued_at == FIXED_NOW
    assert card.context.employee_id is None
    verify_scorecard(card.to_dict(), {signer.key_id: signer.public_key}, artifact=ARTIFACT, harness_id=HARNESS_ID)


def test_one_failed_check_fails_the_scorecard(signer):
    checkers = passing("answer")
    checkers[("answer", "pii_dlp")] = lambda artifact, ctx: CheckOutcome("fail", {"pii_fields": 1})

    card = Validator(signer, checkers).validate(ARTIFACT, "answer", CONTEXT)

    assert card.verdict == "fail"
    assert next(c for c in card.checks if c.name == "pii_dlp").metrics == {"pii_fields": 1}


def test_missing_checker_fails_closed(signer):
    card = Validator(signer, {}).validate(ARTIFACT, "adapter", CONTEXT)

    assert card.verdict == "fail"
    assert all(c.metrics == {"reason": NOT_IMPLEMENTED} for c in card.checks)


def test_crashing_checker_fails_closed_and_keeps_log_inside(signer):
    def broken(artifact, ctx):
        raise RuntimeError("boom")

    checkers = passing("answer")
    checkers[("answer", "judge")] = broken
    validator = Validator(signer, checkers)

    card = validator.validate(ARTIFACT, "answer", CONTEXT)
    judge = next(c for c in card.checks if c.name == "judge")

    assert card.verdict == "fail"
    assert judge.metrics == {"reason": "checker_error"}
    assert "boom" in validator.log(judge.log_ref)
    assert "boom" not in json.dumps(card.to_dict(), ensure_ascii=False)


@pytest.mark.parametrize(
    "artifact, profile, context",
    [
        (ARTIFACT, "unknown", CONTEXT),
        (["not", "object"], "answer", CONTEXT),
        (ARTIFACT, "answer", {"company_id": "x", "harness_id": HARNESS_ID}),
        (ARTIFACT, "answer", {**CONTEXT, "employee_id": ""}),
    ],
)
def test_invalid_requests_are_rejected(signer, artifact, profile, context):
    with pytest.raises(InvalidRequest):
        Validator(signer, {}).validate(artifact, profile, context)


def test_validator_without_key_signs_nothing():
    with pytest.raises(SigningUnavailable):
        Validator(None, passing("answer")).validate(ARTIFACT, "answer", CONTEXT)


# --- HTTP ----------------------------------------------------------------------

def test_http_validate_returns_verifiable_scorecard_via_published_keys(signer):
    client = TestClient(create_app(Validator(signer, passing("skill"))))

    keys = client.get("/v1/keys").json()["keys"]
    response = client.post("/v1/validate", json={"artifact": ARTIFACT, "profile": "skill", "context": CONTEXT})

    assert response.status_code == 200
    card = response.json()
    assert card["verdict"] == "pass"
    published = {k["key_id"]: load_public_key(k["public_key"]) for k in keys}
    verify_scorecard(card, published, artifact=ARTIFACT, harness_id=HARNESS_ID)


def test_http_failed_verdict_is_200_and_bad_request_is_400(signer):
    client = TestClient(create_app(Validator(signer, {})))

    failed = client.post("/v1/validate", json={"artifact": ARTIFACT, "profile": "answer", "context": CONTEXT})
    invalid = client.post("/v1/validate", json={"artifact": ARTIFACT, "profile": "nope", "context": CONTEXT})

    assert failed.status_code == 200 and failed.json()["verdict"] == "fail"
    assert invalid.status_code == 400 and invalid.json()["error"] == "invalid_request"


def test_http_without_signing_key_is_healthy_but_refuses_to_sign():
    client = TestClient(create_app(Validator(None, passing("answer"))))

    assert client.get("/healthz").json() == {
        "status": "ok", "service": "validator", "signing": False, "profile_set": "mvp-v1"
    }
    assert client.get("/v1/keys").json() == {"keys": []}
    response = client.post("/v1/validate", json={"artifact": ARTIFACT, "profile": "answer", "context": CONTEXT})
    assert response.status_code == 503


def test_build_validator_reads_key_from_secret_store():
    raw = Ed25519PrivateKey.generate().private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    store = EnvSecretStore({"VALIDATOR_SIGNING_KEY": base64.b64encode(raw).decode("ascii")})

    assert build_validator("env:VALIDATOR_SIGNING_KEY", store).signer is not None
    assert build_validator("env:VALIDATOR_SIGNING_KEY", EnvSecretStore({})).signer is None
