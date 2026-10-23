"""Golden C-4: GT-c4-01..07.

GT-c4-03 и GT-c4-07 — подпись и привязка ScoreCard (T-11); 01, 02, 04, 06 — чекеры T-12 [D].
GT-c4-05 (профиль Action) ждёт M7 и помечен xfail(strict).
"""

from collections.abc import Mapping
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from src.validator.checkers import default_checkers
from src.validator.scorecard import (
    ScoreCardRejected,
    ScoreCardSigner,
    verify_scorecard,
)
from src.validator.service import NOT_IMPLEMENTED, CheckOutcome, PROFILE_CHECKS, Validator


M7 = pytest.mark.xfail(strict=True, reason="T-12 полный объём (профиль Action, M7)")

COMPANY_ID = str(uuid4())
HARNESS_A = str(uuid4())
HARNESS_B = str(uuid4())


def context(harness_id: str = HARNESS_A) -> dict:
    return {"company_id": COMPANY_ID, "harness_id": harness_id, "employee_id": "twin-a"}


@pytest.fixture
def signer() -> ScoreCardSigner:
    return ScoreCardSigner(Ed25519PrivateKey.generate())


@pytest.fixture
def validator(signer) -> Validator:
    return Validator(signer, default_checkers(), profile_set="demo-v1")


def check(card, name):
    return next(item for item in card.checks if item.name == name)


def failed_by(card, name) -> bool:
    """Отказ именно этим чекером, а не заглушкой «не реализовано»."""
    result = check(card, name)
    return (
        card.verdict == "fail"
        and result.verdict == "fail"
        and result.metrics.get("reason") != NOT_IMPLEMENTED
    )


# --- артефакты из демо-процесса (specs/60-demo-landscape.md) ---------------

SOURCES = [
    {"source_id": "s1", "system": "crm", "object_ref": "order/1042", "read_at": "2026-10-20T09:00:00+00:00"},
    {"source_id": "s2", "system": "stock", "object_ref": "item/A-17", "read_at": "2026-10-20T09:00:01+00:00"},
]


def answer(claims: list[dict]) -> dict:
    return {"text": "КП по заявке 1042 готово", "claims": claims, "sources": SOURCES}


GROUNDED = answer([
    {"claim_id": "c1", "text": "Позиции A-17 не хватает 4 шт.", "source_ids": ["s2"]},
    {"claim_id": "c2", "text": "Дедлайн клиента — 25.10", "source_ids": ["s1"]},
])
UNGROUNDED = answer([
    {"claim_id": "c1", "text": "Позиции A-17 не хватает 4 шт.", "source_ids": ["s2"]},
    {"claim_id": "c2", "text": "Клиент готов ждать две недели", "source_ids": []},
])

# Адаптер по контракту C-3 («Среда исполнения адаптера»).
CRM_ADAPTER = '''\
from datetime import datetime, timezone

SYSTEM = "crm"
COLLECTIONS = {"order": "/orders"}


class Adapter:
    def __init__(self, http):
        self.http = http
        self.layout = None

    def _layout(self):
        if self.layout is None:
            self.layout = self.http.get("/schema")
        return self.layout

    def capabilities(self):
        return {"reads": ["order"], "writes": []}

    def discover(self):
        return {"system": SYSTEM, "objects": self._layout()["objects"]}

    def probe(self, ref):
        obj, key = ref.split("/", 1)
        data = self.http.get(COLLECTIONS[obj] + "/" + key)
        labels = {}
        for item in self._layout()["objects"]:
            if item["name"] == obj:
                labels = {f: label for f, label in item["sensitivity"].items() if f in data}
        return {
            "ref": ref,
            "data": data,
            "source": {"system": SYSTEM, "object_ref": ref, "read_at": datetime.now(timezone.utc).isoformat()},
            "sensitivity": labels,
        }
'''

ADAPTER_MANIFEST = {
    "system": "crm",
    "version": "0.1.0",
    "auth": {"kind": "token", "secret_ref": "env:CRM_TOKEN"},
    "read_scopes": ["orders:read"],
    "writes": [],
    "provenance": {"author": "researcher", "job_id": "job-1", "created_at": "2026-10-20T09:00:00+00:00"},
    "tests": ["order/1037"],
}
CLEAN_ADAPTER = {
    "manifest": ADAPTER_MANIFEST,
    "files": {"adapter.py": CRM_ADAPTER},
    "test_data_ref": "demo-landscape:fixture-v1",
}
HARDCODED_SECRET_ADAPTER = {
    **CLEAN_ADAPTER,
    "files": {"adapter.py": CRM_ADAPTER + "\nTOKEN = 'sk-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0'\n"},
}

CURATOR_SCOPES = {"connectors": ["crm", "stock", "pricing"], "read_scopes": ["orders:read"], "write_classes": []}


def passport(connectors: list[str], autonomy: Mapping[str, str]) -> dict:
    return {
        "passport": {
            "employee_id": "twin-a",
            "version": 1,
            "kind": "twin",
            "harness_id": HARNESS_A,
            "owner_person_id": "p-a",
            "curator_person_id": "p-a",
            "role": {"title": "Менеджер по продажам", "mission": "Готовить КП по заявкам"},
            "duties": [{"duty_id": "d1", "text": "Разбирать новые заявки"}],
            "goals": [{"goal_id": "g1", "text": "КП за 2 часа", "metric": "quote_hours", "target": 2, "period": "day"}],
            "decision_bounds": {"may": ["предлагать аналоги"], "must_not": ["скидка ниже min_margin"]},
            "escalation": [{"condition": "нет позиции и аналога", "to": "curator"}],
            "tools": {"connectors": connectors, "read_scopes": ["orders:read"], "write_classes": []},
            "triggers": {"schedule": [{"cron": "*/15 * * * *", "duty_id": "d1"}], "events": []},
            "autonomy": dict(autonomy),
            "provenance": {"source": "role_text", "drafted_by": "developer", "consent_ref": None, "created_at": "2026-10-20T09:00:00+00:00"},
        },
        "curator_scopes": CURATOR_SCOPES,
    }


def action(level_allowed: str, current_level: str, consent: bool) -> dict:
    return {
        "action": {"action_class": "crm.order.set_status", "target_ref": "order/1042", "params": {"status": "quoted"}, "idempotency_key": "job-1:3"},
        "change_plan": {"action_class": "crm.order.set_status", "target_ref": "order/1042", "before": {"status": "in_work"}, "after": {"status": "quoted"}, "reversible": True},
        "grant": {"level": level_allowed, "scorecard_ref": None, "consent_token": "tok" if consent else None},
        "current_level": current_level,
    }


def skill(body: str) -> dict:
    return {
        "candidate": {
            "scope": "company",
            "company_id": COMPANY_ID,
            "author_person_id": "p-a",
            "title": "Аналог при нехватке позиции",
            "applies_to": {"role_tags": ["sales"], "task_class": "quote"},
            "body": body,
            "examples": [],
            "provenance": {"source_jobs": ["job-1"], "scorecard_ref": None, "consent_ref": None, "created_at": "2026-10-20T09:00:00+00:00"},
        },
        "source_job_summaries": [{"job_id": "job-1", "identifiers": ["ООО Ромашка", "order/1042"]}],
    }


# --- GT-c4-01 ------------------------------------------------------------------

def test_gt_c4_01_answer_without_source_fails_grounding(validator):
    card = validator.validate(UNGROUNDED, "answer", context())
    assert failed_by(card, "grounding")
    assert check(card, "grounding").metrics["uncovered"] == 1


def test_gt_c4_01_control_grounded_answer_passes(validator):
    card = validator.validate(GROUNDED, "answer", context())
    assert card.verdict == "pass"
    assert card.profile_set == "demo-v1"


# --- GT-c4-02 ------------------------------------------------------------------

def test_gt_c4_02_adapter_with_hardcoded_secret_fails_security(validator):
    card = validator.validate(HARDCODED_SECRET_ADAPTER, "adapter", context())
    assert failed_by(card, "security")


def test_gt_c4_02_unsafe_adapter_is_never_executed(validator):
    card = validator.validate(HARDCODED_SECRET_ADAPTER, "adapter", context())
    assert check(card, "sandbox").metrics["reason"] == "security_failed"


def test_gt_c4_02_control_clean_adapter_passes_whole_profile(validator):
    card = validator.validate(CLEAN_ADAPTER, "adapter", context())
    assert [c.name for c in card.checks] == ["schema", "security", "sandbox", "eval"]
    assert card.verdict == "pass", [(c.name, c.metrics, validator.log(c.log_ref)) for c in card.checks]


# --- GT-c4-03 ------------------------------------------------------------------

def _passing(profile: str) -> dict:
    return {(profile, name): (lambda artifact, ctx: CheckOutcome("pass")) for name in PROFILE_CHECKS[profile]}


@pytest.fixture
def passing_validator(signer) -> Validator:
    return Validator(signer, _passing("answer"))


def test_gt_c4_03_signed_scorecard_is_accepted(passing_validator, signer):
    card = passing_validator.validate(GROUNDED, "answer", context()).to_dict()
    verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_A)


def test_gt_c4_03_missing_signature_is_blocked(passing_validator, signer):
    card = passing_validator.validate(GROUNDED, "answer", context()).to_dict()
    card["signature"] = ""
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_A)
    assert error.value.code == "unsigned"


def test_gt_c4_03_scorecard_signed_by_another_key_is_blocked(signer):
    forger = ScoreCardSigner(Ed25519PrivateKey.generate())
    card = Validator(forger, _passing("answer")).validate(GROUNDED, "answer", context()).to_dict()
    keys = {signer.key_id: signer.public_key}

    with pytest.raises(ScoreCardRejected) as unknown:
        verify_scorecard(card, keys, artifact=GROUNDED, harness_id=HARNESS_A)
    assert unknown.value.code == "unknown_key"

    card["key_id"] = signer.key_id  # подделка: чужая подпись под нашим key_id
    with pytest.raises(ScoreCardRejected) as forged:
        verify_scorecard(card, keys, artifact=GROUNDED, harness_id=HARNESS_A)
    assert forged.value.code == "bad_signature"


def test_gt_c4_03_tampered_verdict_is_blocked(signer):
    card = Validator(signer, {}).validate(GROUNDED, "answer", context()).to_dict()
    assert card["verdict"] == "fail"
    card["verdict"] = "pass"
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_A)
    assert error.value.code == "bad_signature"


def test_gt_c4_03_tampered_profile_set_is_blocked(passing_validator, signer):
    card = passing_validator.validate(GROUNDED, "answer", context()).to_dict()
    card["profile_set"] = "demo-v1" if card["profile_set"] != "demo-v1" else "mvp-v1"
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_A)
    assert error.value.code == "bad_signature"


def test_gt_c4_03_scorecard_for_other_artifact_is_blocked(passing_validator, signer):
    card = passing_validator.validate(GROUNDED, "answer", context()).to_dict()
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=UNGROUNDED, harness_id=HARNESS_A)
    assert error.value.code == "artifact_mismatch"


def test_gt_c4_03_failed_verdict_is_not_released(signer):
    card = Validator(signer, {}).validate(GROUNDED, "answer", context()).to_dict()
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_A)
    assert error.value.code == "verdict_fail"


# --- GT-c4-04 ------------------------------------------------------------------

def test_gt_c4_04_passport_with_unconnected_tool_fails_rights(validator):
    card = validator.validate(passport(["crm", "erp"], {}), "passport", context())
    assert failed_by(card, "rights")
    assert check(card, "rights").metrics["excess"] == {"connectors": ["erp"]}


def test_gt_c4_04_new_passport_above_l0_fails_autonomy(validator):
    card = validator.validate(passport(["crm"], {"crm.order.set_status": "L1"}), "passport", context())
    assert failed_by(card, "autonomy")


def test_gt_c4_04_control_valid_passport_passes(validator):
    card = validator.validate(passport(["crm", "stock"], {}), "passport", context())
    assert card.verdict == "pass", [(c.name, validator.log(c.log_ref)) for c in card.checks]


# --- GT-c4-05 ------------------------------------------------------------------

@M7
def test_gt_c4_05_action_above_current_level_fails_policy_scope(validator):
    card = validator.validate(action("L1", "L0", consent=True), "action", context())
    assert failed_by(card, "policy_scope")


@M7
def test_gt_c4_05_l1_action_without_consent_fails(validator):
    card = validator.validate(action("L1", "L1", consent=False), "action", context())
    assert failed_by(card, "consent")


# --- GT-c4-06 ------------------------------------------------------------------

def test_gt_c4_06_company_skill_with_client_name_fails_foreign_data(validator):
    body = "Если у ООО Ромашка не хватает позиции, предложить аналог из analogs[]"
    card = validator.validate(skill(body), "skill", context())
    assert failed_by(card, "foreign_data")
    assert "Ромашка" not in str(card.to_dict())  # сами идентификаторы в ScoreCard не попадают


def test_gt_c4_06_control_anonymized_skill_passes(validator):
    body = "Если позиции не хватает, предложить аналог из analogs[], при марже не ниже min_margin"
    card = validator.validate(skill(body), "skill", context())
    assert card.verdict == "pass", [(c.name, validator.log(c.log_ref)) for c in card.checks]


# --- GT-c4-07 ------------------------------------------------------------------

def test_gt_c4_07_scorecard_of_harness_a_is_blocked_in_harness_b(passing_validator, signer):
    card = passing_validator.validate(GROUNDED, "answer", context(HARNESS_A)).to_dict()
    with pytest.raises(ScoreCardRejected) as error:
        verify_scorecard(card, {signer.key_id: signer.public_key}, artifact=GROUNDED, harness_id=HARNESS_B)
    assert error.value.code == "harness_mismatch"
