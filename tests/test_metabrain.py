"""T-21, T-22: мета-мозг по C-5 — GT-c5-01..05, -07, политика, журнал, HTTP.

Тесты хранилища требуют PostgreSQL (`TEST_DATABASE_URL`), как и тесты харнесса.
"""

import base64
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import os
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest
import rfc8785
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from src.common.secrets import EnvSecretStore
from src.metabrain.app import Readiness, create_app
from src.metabrain.consent import ConsentRejected, load_public_key, subject_hash, verify_consent
from src.metabrain.policy import PolicyError, load_policy, resolve_policy, validate_policy
from src.metabrain.service import Conflict, InvalidRequest, Metabrain, NotFound, Rejected, Unavailable
from src.metabrain.store import ConsentReused, MetabrainStore, migrate
from src.metabrain.validator_client import HttpValidatorClient, ValidatorUnavailable
from src.validator.checkers import default_checkers
from src.validator.scorecard import ScoreCardSigner
from src.validator.service import Validator


NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
CONSENT_KEY = Ed25519PrivateKey.generate()
CONSENT_PUBLIC = CONSENT_KEY.public_key()


class InProcessValidator:
    """ValidatorClient поверх настоящего Validator (набор demo-v1)."""

    def __init__(self, signer: ScoreCardSigner | None = None, keys_signer: ScoreCardSigner | None = None):
        self.signer = signer or ScoreCardSigner(Ed25519PrivateKey.generate())
        self.keys_signer = keys_signer or self.signer
        self.validator = Validator(self.signer, default_checkers(), profile_set="demo-v1")
        self.key_requests = 0

    def validate(self, artifact, profile, context):
        return self.validator.validate(artifact, profile, context).to_dict()

    def keys(self, *, refresh: bool = False):
        self.key_requests += 1
        return {self.keys_signer.key_id: self.keys_signer.public_key}


# --- фикстуры -------------------------------------------------------------------

@pytest.fixture
def database() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")
    schema = f"test_metabrain_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield schema
    finally:
        with psycopg.connect(url, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def connector(schema: str):
    url = os.environ["TEST_DATABASE_URL"]
    return lambda: psycopg.connect(url, options=f"-c search_path={schema}")


@pytest.fixture
def connect(database):
    factory = connector(database)
    with factory() as connection:
        migrate(connection)
    return factory


@pytest.fixture
def validator() -> InProcessValidator:
    return InProcessValidator()


@pytest.fixture
def brain(connect, validator) -> Metabrain:
    return Metabrain(connect, validator, load_policy(), CONSENT_PUBLIC, clock=lambda: NOW)


COMPANY = str(uuid4())
OTHER_COMPANY = str(uuid4())
HARNESS_A = str(uuid4())
HARNESS_B = str(uuid4())
HARNESS_C = str(uuid4())  # другая компания


def caller(harness: str, company: str = COMPANY) -> dict:
    return {"company_id": company, "harness_id": harness}


def candidate(scope: str, harness: str = HARNESS_A, company: str = COMPANY, **changes) -> dict:
    data = {
        "scope": scope,
        "company_id": company,
        "title": "Аналог при нехватке позиции",
        "applies_to": {"role_tags": ["sales"], "task_class": "quote"},
        "body": "Если позиции не хватает на складе, предложить аналог из analogs[] при марже не ниже min_margin",
        "examples": [],
        "provenance": {"source_jobs": ["job-1"], "scorecard_ref": None, "consent_ref": None, "created_at": "2026-10-20T09:00:00+00:00"},
    }
    if scope == "personal":
        data["owner_harness_id"] = harness
    else:
        data["author_person_id"] = "person-a"
    data.update(changes)
    return {
        **caller(harness, company),
        "candidate": data,
        "source_job_summaries": [{"job_id": "job-1", "identifiers": ["ООО Ромашка", "order/1042"]}],
    }


def consent_for(proposed: dict, *, harness: str = HARNESS_A, scope: str = "skill_publish",
                exp: datetime | None = None, key: Ed25519PrivateKey = CONSENT_KEY, subject: dict | None = None) -> dict:
    """Токен в формате ProposalStore._token_payload (C-1) + подпись."""
    subject = subject or {"candidate_id": proposed["candidate_id"], "artifact_hash": proposed["artifact_hash"]}
    signing_key = Ed25519PrivateKey.from_private_bytes(
        HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=UUID(harness).bytes,
            info=b"metabrain:consent-signing:v1",
        ).derive(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()))
    )
    signing_public_key = base64.urlsafe_b64encode(
        signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode().rstrip("=")
    certificate_payload = {
        "harness_id": harness,
        "signing_public_key": signing_public_key,
    }
    key_certificate = base64.urlsafe_b64encode(key.sign(rfc8785.dumps(certificate_payload))).decode().rstrip("=")
    payload = {
        "token_id": str(uuid4()),
        "harness_id": harness,
        "proposal_id": str(uuid4()),
        "subject_hash": subject_hash(subject),
        "scope": scope,
        "issued_by": str(uuid4()),
        "surface": "web",
        "issued_at": (NOW - timedelta(minutes=1)).isoformat(),
        "exp": (exp or NOW + timedelta(minutes=14)).isoformat(),
        "signing_public_key": signing_public_key,
        "key_certificate": key_certificate,
    }
    signature = base64.urlsafe_b64encode(signing_key.sign(rfc8785.dumps(payload))).decode().rstrip("=")
    return {**payload, "signature": signature}


def publish_personal(brain: Metabrain, harness: str = HARNESS_A, **changes) -> dict:
    proposed = brain.propose(candidate("personal", harness, **changes))
    assert proposed["verdict"] == "pass", proposed["scorecard"]["checks"]
    return brain.publish({**caller(harness), "candidate_id": proposed["candidate_id"]})


def publish_company(brain: Metabrain, **changes) -> dict:
    proposed = brain.propose(candidate("company", **changes))
    assert proposed["verdict"] == "pass", proposed["scorecard"]["checks"]
    return brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"], "consent_token": consent_for(proposed)})


# --- GT-c5-01 -------------------------------------------------------------------

def test_gt_c5_01_resolve_by_lock_returns_exactly_that_version(brain):
    first = publish_personal(brain)
    second = publish_personal(brain, supersedes=first["skill_id"], body="Проверить аналог и срок поставки до дедлайна")

    old = brain.resolve({**caller(HARNESS_A), "lock": f"{first['skill_id']}@1.0.0"})["skills"]
    assert [(s["skill_id"], s["version"]) for s in old] == [(first["skill_id"], "1.0.0")]
    assert second == {"skill_id": first["skill_id"], "version": "1.1.0"}


def test_gt_c5_01_foreign_personal_skill_by_lock_is_empty(brain):
    skill = publish_personal(brain, HARNESS_A)
    assert brain.resolve({**caller(HARNESS_B), "lock": f"{skill['skill_id']}@1.0.0"}) == {"skills": []}


# --- GT-c5-02 -------------------------------------------------------------------

def test_gt_c5_02_retrieval_never_returns_another_harness_data(brain):
    brain.index({**caller(HARNESS_A), "docs": [
        {"doc_id": "fact:order/1042", "kind": "fact", "text": "Заявка 1042: кронштейн A-17 в дефиците", "source": {"system": "crm", "object_ref": "order/1042"}},
        {"doc_id": "traj:job-1", "kind": "trajectory", "text": "Предложили аналог A-17M, куратор подтвердил"},
    ]})

    own = brain.search({**caller(HARNESS_A), "query": "кронштейн дефицит"})["hits"]
    foreign = brain.search({**caller(HARNESS_B), "query": "кронштейн дефицит"})["hits"]
    facts_only = brain.search({**caller(HARNESS_A), "query": "аналог кронштейн", "kinds": ["fact"]})["hits"]

    assert [h["doc_id"] for h in own] == ["fact:order/1042"]
    assert own[0]["source"] == {"system": "crm", "object_ref": "order/1042"}
    assert foreign == []
    assert {h["kind"] for h in facts_only} == {"fact"}


def test_retrieval_index_is_an_upsert_per_harness(brain):
    for text in ("старый текст про склад", "новый текст про склад"):
        brain.index({**caller(HARNESS_A), "docs": [{"doc_id": "card:1", "kind": "card", "text": text}]})
    hits = brain.search({**caller(HARNESS_A), "query": "склад"})["hits"]
    assert [h["text"] for h in hits] == ["новый текст про склад"]


# --- GT-c5-03 -------------------------------------------------------------------

def test_gt_c5_03_failed_scorecard_is_not_published(brain):
    proposed = brain.propose(candidate("personal", body="Звонить клиенту по +7 900 111-22-33"))
    assert proposed["verdict"] == "fail"
    with pytest.raises(Rejected) as error:
        brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})
    assert error.value.code == "verdict_fail"


def test_gt_c5_03_scorecard_with_forged_signature_is_not_published(connect):
    real = ScoreCardSigner(Ed25519PrivateKey.generate())
    forger = InProcessValidator(signer=ScoreCardSigner(Ed25519PrivateKey.generate()), keys_signer=real)
    brain = Metabrain(connect, forger, load_policy(), CONSENT_PUBLIC, clock=lambda: NOW)
    proposed = brain.propose(candidate("personal"))

    with pytest.raises(Rejected) as error:
        brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})
    assert error.value.code == "unknown_key"
    assert forger.key_requests == 2  # ключи перечитаны один раз перед отказом


def test_publish_is_bound_to_the_proposing_harness(brain):
    proposed = brain.propose(candidate("personal"))
    with pytest.raises(NotFound):
        brain.publish({**caller(HARNESS_B), "candidate_id": proposed["candidate_id"]})


def test_candidate_is_published_once(brain):
    proposed = brain.propose(candidate("personal"))
    brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})
    with pytest.raises(Conflict):
        brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})


# --- GT-c5-04 -------------------------------------------------------------------

@pytest.mark.parametrize("make_token, code", [
    (lambda p: None, "consent_required"),
    (lambda p: consent_for(p, subject={"candidate_id": str(uuid4()), "artifact_hash": p["artifact_hash"]}), "consent_other_subject"),
    (lambda p: consent_for(p, harness=HARNESS_B), "consent_other_harness"),
    (lambda p: consent_for(p, scope="passport"), "consent_wrong_scope"),
    (lambda p: consent_for(p, exp=NOW - timedelta(seconds=1)), "consent_expired"),
    (lambda p: consent_for(p, key=Ed25519PrivateKey.generate()), "bad_consent_signature"),
])
def test_gt_c5_04_company_skill_requires_the_authors_consent(brain, make_token, code):
    proposed = brain.propose(candidate("company"))
    with pytest.raises(Rejected) as error:
        brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"], "consent_token": make_token(proposed)})
    assert error.value.code == code
    assert brain.resolve({**caller(HARNESS_A), "task_class": "quote"}) == {"skills": []}


def test_gt_c5_04_consent_token_is_single_use(connect):
    with connect() as connection:
        store = MetabrainStore(connection)
        token_id = uuid4()
        store.use_consent(token_id=token_id, harness_id=UUID(HARNESS_A), candidate_id=uuid4())
        with pytest.raises(ConsentReused):
            store.use_consent(token_id=token_id, harness_id=UUID(HARNESS_A), candidate_id=uuid4())


# --- GT-c5-05 -------------------------------------------------------------------

def test_gt_c5_05_company_skill_is_visible_in_the_company_only(brain):
    published = publish_company(brain)

    in_company = brain.resolve({**caller(HARNESS_B), "task_class": "quote"})["skills"]
    other_company = brain.resolve({**caller(HARNESS_C, OTHER_COMPANY), "task_class": "quote"})["skills"]

    assert [s["skill_id"] for s in in_company] == [published["skill_id"]]
    assert in_company[0]["scope"] == "company" and in_company[0]["provenance"]["consent_ref"]
    assert other_company == []


def test_personal_skill_is_visible_to_its_harness_only(brain):
    publish_personal(brain, HARNESS_A)
    assert len(brain.resolve({**caller(HARNESS_A), "task_class": "quote"})["skills"]) == 1
    assert brain.resolve({**caller(HARNESS_B), "task_class": "quote"})["skills"] == []


def test_resolve_filters_by_query_and_role_tags(brain):
    publish_personal(brain)
    publish_personal(brain, title="Сверка счетов", body="Сверять счета поставщика с актом", applies_to={"role_tags": ["finance"], "task_class": "reconcile"})

    by_query = brain.resolve({**caller(HARNESS_A), "query": "нехватка аналог склад"})["skills"]
    by_tags = brain.resolve({**caller(HARNESS_A), "role_tags": ["finance"]})["skills"]
    assert [s["title"] for s in by_query] == ["Аналог при нехватке позиции"]
    assert [s["title"] for s in by_tags] == ["Сверка счетов"]


# --- GT-c5-07 -------------------------------------------------------------------

def test_gt_c5_07_supersedes_creates_minor_version_and_keeps_history_immutable(brain, connect):
    first = publish_personal(brain)
    publish_personal(brain, supersedes=first["skill_id"], body="Новая редакция: проверить ещё срок поставки")

    latest = brain.resolve({**caller(HARNESS_A), "task_class": "quote"})["skills"]
    assert [(s["version"], s["supersedes"]) for s in latest] == [("1.1.0", first["skill_id"])]

    with connect() as connection, pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
        connection.execute("UPDATE mb_skills SET body = 'взлом'")


def test_supersedes_cannot_take_over_someone_elses_skill(brain):
    foreign = publish_personal(brain, HARNESS_B)
    proposed = brain.propose(candidate("personal", HARNESS_A, supersedes=foreign["skill_id"]))
    with pytest.raises(Rejected) as error:
        brain.publish({**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})
    assert error.value.code == "supersedes_not_visible"


# --- совместимость с согласиями харнесса (C-1) -----------------------------------

def test_consent_issued_by_the_harness_proposal_store_is_accepted(database, brain):
    from src.harness.proposals import ProposalStore
    from src.harness.store import HarnessStore

    key_material = base64.b64encode(CONSENT_KEY.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
    with connector(database)() as connection:
        identity = HarnessStore(connection).register_person(
            channel="telegram", channel_user_id="mb-compat", company_name="Compat"
        )
        proposals = ProposalStore(
            connection,
            secret_store=EnvSecretStore(
                {
                    "CONSENT_SIGNING_KEY": key_material,
                    "CONSENT_PUBLIC_KEY": base64.b64encode(
                        CONSENT_PUBLIC.public_bytes(Encoding.Raw, PublicFormat.Raw)
                    ).decode(),
                }
            ),
            signing_key_ref="env:CONSENT_SIGNING_KEY",
            token_ttl=timedelta(days=3650),
        )
        company, harness = str(identity.company_id), str(identity.harness_id)
        proposed = brain.propose(candidate("company", harness, company))
        proposal = proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="skill_publish", summary="Навык «Аналог при нехватке позиции» в компанию",
            subject={"candidate_id": proposed["candidate_id"], "artifact_hash": proposed["artifact_hash"]},
        )
        decision = proposals.decide(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal.id,
            decision="approve", actor_person_id=identity.person_id, surface="web",
        )
        connection.commit()

    token = decision.consent_token
    wire = {
        "token_id": str(token.token_id), "harness_id": str(token.harness_id),
        "proposal_id": str(token.proposal_id), "subject_hash": token.subject_hash, "scope": token.scope,
        "issued_by": str(token.issued_by), "surface": token.surface,
        "issued_at": token.issued_at.astimezone(timezone.utc).isoformat(),
        "exp": token.exp.astimezone(timezone.utc).isoformat(),
        "signing_public_key": token.signing_public_key, "key_certificate": token.key_certificate,
        "signature": token.signature,
    }
    published = brain.publish({**caller(harness, company), "candidate_id": proposed["candidate_id"], "consent_token": wire})
    assert published["version"] == "1.0.0"


# --- журнал ---------------------------------------------------------------------

def test_every_call_is_audited_with_its_outcome_and_audit_is_append_only(brain, connect):
    brain.resolve({**caller(HARNESS_A), "task_class": "quote"})
    with pytest.raises(NotFound):
        brain.publish({**caller(HARNESS_A), "candidate_id": str(uuid4())})

    with connect() as connection:
        rows = connection.execute("SELECT operation, outcome, harness_id FROM mb_audit ORDER BY id").fetchall()
        assert [(op, outcome) for op, outcome, _ in rows] == [("skills.resolve", "ok"), ("skill.publish", "not_found")]
        assert {str(h) for _, _, h in rows} == {HARNESS_A}
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            connection.execute("DELETE FROM mb_audit")


# --- политика (без БД) -----------------------------------------------------------

def test_default_policy_routes_critic_to_another_family():
    policy = load_policy()
    critic = resolve_policy(policy, sub_role="critic")["llm"]
    developer = resolve_policy(policy, sub_role="developer")["llm"]
    assert critic["family"] != developer["family"]
    assert policy["autonomy"]["promotion_thresholds"]["L1_to_L2"]["incident_free_days"] == 14


def test_policy_narrows_l2_classes_to_the_employee():
    policy = {**load_policy()}
    policy["autonomy"] = {**policy["autonomy"], "allowed_l2_classes": {"twin-a": ["crm.order.set_status"], "twin-b": ["stock.reserve"]}}
    assert resolve_policy(policy, employee_id="twin-a")["autonomy"]["allowed_l2_classes"] == {"twin-a": ["crm.order.set_status"]}


def test_policy_with_critic_in_developer_family_is_rejected():
    policy = load_policy()
    policy["llm_routing"]["routes"]["critic"] = policy["llm_routing"]["routes"]["developer"]
    with pytest.raises(PolicyError):
        validate_policy(policy)


def test_default_policy_caps_the_demo_at_five_dollars():
    limits = load_policy()["limits"]
    assert limits["budget_usd_total"]["installation"] == 5.0
    assert limits["budget_usd_per_day"]["employee"] <= limits["budget_usd_per_day"]["harness"] <= 5.0


@pytest.mark.parametrize("mutate", [
    lambda p: p["pricing"]["per_million_tokens"].pop("deepseek-v4-pro"),
    lambda p: p["pricing"]["per_million_tokens"]["deepseek-flash"].update(output=-1),
    lambda p: p["limits"]["budget_usd_per_day"].update(employee=2.0),
    lambda p: p["limits"]["budget_usd_per_day"].update(harness=6.0),
    lambda p: p["limits"]["budget_usd_total"].update(installation=0),
])
def test_policy_rejects_unpriced_providers_and_inconsistent_limits(mutate):
    policy = load_policy()
    mutate(policy)
    with pytest.raises(PolicyError):
        validate_policy(policy)


def test_policy_get_rejects_unknown_sub_role(brain):
    with pytest.raises(InvalidRequest):
        brain.policy({**caller(HARNESS_A), "sub_role": "boss"})


# --- согласие (без БД) -------------------------------------------------------------

def test_consent_key_formats_and_missing_key():
    raw = CONSENT_PUBLIC.public_bytes(Encoding.Raw, PublicFormat.Raw)
    assert load_public_key(base64.b64encode(raw).decode()) is not None
    assert load_public_key(base64.urlsafe_b64encode(raw).decode().rstrip("=")) is not None
    with pytest.raises(ConsentRejected) as error:
        verify_consent({}, None, harness_id=HARNESS_A, scope="skill_publish", subject={})
    assert error.value.code == "consent_key_not_configured"


def test_consent_uses_harness_key_certified_by_the_root_public_key():
    proposed = {"candidate_id": str(uuid4()), "artifact_hash": "a" * 64}
    token = consent_for(proposed)
    subject = {"candidate_id": proposed["candidate_id"], "artifact_hash": proposed["artifact_hash"]}

    assert verify_consent(
        token, CONSENT_PUBLIC, harness_id=HARNESS_A, scope="skill_publish", subject=subject, now=NOW
    ) == token["token_id"]

    token["key_certificate"] = "invalid"
    with pytest.raises(ConsentRejected) as error:
        verify_consent(
            token, CONSENT_PUBLIC, harness_id=HARNESS_A, scope="skill_publish", subject=subject, now=NOW
        )
    assert error.value.code == "bad_consent_signature"


# --- HTTP и клиент валидатора ---------------------------------------------------------

def test_http_maps_errors_and_reports_database_readiness():
    def no_db():
        raise Unavailable("database_unavailable")

    brain = Metabrain(no_db, InProcessValidator(), load_policy(), None)
    client = TestClient(create_app(brain, Readiness(no_db)))

    assert client.get("/healthz").json() == {"status": "ok", "service": "metabrain", "db": False}
    assert client.post("/v1/skills/resolve", json=caller(HARNESS_A)).status_code == 503


def test_http_round_trip(brain):
    client = TestClient(create_app(brain))
    proposed = client.post("/v1/skills/propose", json=candidate("personal")).json()
    published = client.post("/v1/skills/publish", json={**caller(HARNESS_A), "candidate_id": proposed["candidate_id"]})
    resolved = client.post("/v1/skills/resolve", json={**caller(HARNESS_A), "lock": f"{published.json()['skill_id']}@1.0.0"})

    assert published.status_code == 200
    assert resolved.json()["skills"][0]["title"] == "Аналог при нехватке позиции"
    assert client.post("/v1/skills/resolve", json={"harness_id": "x"}).status_code == 400
    assert client.post("/v1/skills/publish", json={**caller(HARNESS_A), "candidate_id": str(uuid4())}).status_code == 404


def test_http_validator_client_handles_errors_and_reads_keys():
    signer = ScoreCardSigner(Ed25519PrivateKey.generate())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/keys":
            return httpx.Response(200, json={"keys": signer.keys()})
        return httpx.Response(503, json={"error": "signing_key_not_configured"})

    client = HttpValidatorClient("http://validator:8080", transport=httpx.MockTransport(handler))
    assert set(client.keys()) == {signer.key_id}
    with pytest.raises(ValidatorUnavailable):
        client.validate({}, "skill", {})
