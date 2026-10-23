"""Чекеры T-12 [D] по правилам C-4 и GT-c3-04 (sandbox ловит вредный драфт)."""

import copy
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
import pytest

from src.demo_landscape.app import Grant, create_app as create_landscape
from src.demo_landscape.data import Landscape
from src.validator.checks import adapter, answer, detectors, passport, skill
from src.validator.checks.stand import STAND_USER, load_stand
from src.validator.scorecard import Context
from tests.test_validator_golden import (
    CLEAN_ADAPTER,
    CRM_ADAPTER,
    GROUNDED,
    passport as make_passport,
    skill as make_skill,
)


COMPANY = uuid4()
HARNESS = uuid4()
CTX = Context(COMPANY, HARNESS)
STAND = load_stand("demo-landscape:fixture-v1")
CRM_ROUTES = STAND.routes["crm"]


def with_files(source: str, **manifest_changes) -> dict:
    artifact = copy.deepcopy(CLEAN_ADAPTER)
    artifact["files"] = {"adapter.py": source}
    artifact["manifest"].update(manifest_changes)
    return artifact


def run(source: str, refs=("order/1042",), timeout: float = 10.0):
    return adapter.run_in_sandbox({"adapter.py": source}, CRM_ROUTES, list(refs), timeout=timeout)


def adapter_that(init_body: str, prelude: str = "") -> str:
    """Минимальный адаптер, который делает `init_body` в конструкторе."""
    return (
        prelude
        + "\nclass Adapter:\n    def __init__(self, http):\n        self.http = http\n"
        + "".join(f"        {line}\n" for line in init_body.splitlines())
        + "    def capabilities(self):\n        return {'reads': [], 'writes': []}\n"
        + "    def discover(self):\n        return {'system': 'crm', 'objects': []}\n"
        + "    def probe(self, ref):\n        return {}\n"
    )


# --- detectors ------------------------------------------------------------------

@pytest.mark.parametrize("text, category", [
    ("карта 4111 1111 1111 1111", "card"),
    ("паспорт: 4512 345678", "passport_ru"),
    ("СНИЛС 112-233-445 95", "snils"),
    ("ключ sk-abcdefghijklmnopqrstuvwx", "secret"),
    ("пишите sokolova@romashka.example", "email"),
    ("звоните +7 900 111-22-33", "phone"),
])
def test_detectors_find_each_category(text, category):
    assert category in detectors.detect(text)


@pytest.mark.parametrize("text", [
    "1234 5678 9012 3456",            # не проходит Луна
    "заявка 1042, 10 шт., 25.10",
    "маржа 0.19 при цене 420",
])
def test_detectors_ignore_business_numbers(text):
    assert detectors.detect(text) == {}


# --- Answer ---------------------------------------------------------------------

def test_grounding_rejects_dangling_source_and_missing_fields():
    dangling = copy.deepcopy(GROUNDED)
    dangling["claims"][0]["source_ids"] = ["s9"]
    broken = copy.deepcopy(GROUNDED)
    del broken["sources"][0]["read_at"]
    empty = {**GROUNDED, "claims": []}

    check = answer.grounding()
    assert check(dangling, CTX).metrics["unknown_source_refs"] == 1
    assert check(dangling, CTX).verdict == "fail"
    assert check(broken, CTX).verdict == "fail"
    assert check(empty, CTX).metrics["reason"] == "no_claims"


def test_grounding_threshold_comes_from_policy():
    half = copy.deepcopy(GROUNDED)
    half["claims"][1]["source_ids"] = []
    assert answer.grounding(0.0)(half, CTX).verdict == "fail"
    assert answer.grounding(0.5)(half, CTX).verdict == "pass"


def test_answer_allows_client_contacts_but_not_cards_or_secrets():
    contacts = {**GROUNDED, "text": "Контакт: sokolova@romashka.example, +7 900 111-22-33"}
    card = {**GROUNDED, "text": "Оплата картой 4111 1111 1111 1111"}
    assert answer.pii_dlp(contacts, CTX).verdict == "pass"
    assert answer.pii_dlp(card, CTX).verdict == "fail"
    assert answer.pii_dlp(card, CTX).metrics["forbidden"] == ["card"]


# --- Passport -------------------------------------------------------------------

def passport_artifact(**changes) -> dict:
    artifact = make_passport(["crm"], {})
    artifact["passport"]["harness_id"] = str(HARNESS)
    artifact["passport"].update(changes)
    return artifact


def test_passport_schema_checks_cron_and_trigger_duties():
    bad = passport_artifact(triggers={"schedule": [{"cron": "every 15 min", "duty_id": "d9"}], "events": []})
    result = passport.schema(bad, CTX)
    assert result.verdict == "fail" and result.metrics["errors"] == 2
    assert passport.schema(passport_artifact(), CTX).verdict == "pass"


def test_passport_rights_require_same_harness_and_read_only_researcher():
    other = passport_artifact(harness_id=str(uuid4()))
    researcher = passport_artifact(kind="researcher")
    researcher["passport"]["tools"]["write_classes"] = ["crm.order.set_status"]
    researcher["curator_scopes"]["write_classes"] = ["crm.order.set_status"]
    assert passport.rights(other, CTX).verdict == "fail"
    assert passport.rights(researcher, CTX).verdict == "fail"


def test_passport_level_changes_only_via_autonomy_change():
    previous = passport_artifact(autonomy={"crm.order.set_status": "L1"})["passport"]
    same = passport_artifact(autonomy={"crm.order.set_status": "L1"}, version=2)
    raised = passport_artifact(autonomy={"crm.order.set_status": "L2"}, version=2)
    same["previous_passport"] = previous
    raised["previous_passport"] = previous

    assert passport.autonomy(same, CTX).verdict == "pass"
    assert passport.autonomy(raised, CTX).metrics["changed_outside_autonomy_change"] == ["crm.order.set_status"]
    assert passport.autonomy(passport_artifact(autonomy={"x": "L5"}), CTX).verdict == "fail"


# --- Skill ----------------------------------------------------------------------

def skill_artifact(body: str = "Предлагать аналог при нехватке позиции", **changes) -> dict:
    artifact = make_skill(body)
    artifact["candidate"]["company_id"] = str(COMPANY)
    artifact["candidate"].update(changes)
    return artifact


def test_skill_schema_rejects_assigned_ids_foreign_company_and_foreign_owner():
    assert skill.schema(skill_artifact(), CTX).verdict == "pass"
    assert skill.schema(skill_artifact(skill_id="s1"), CTX).verdict == "fail"
    assert skill.schema(skill_artifact(company_id=str(uuid4())), CTX).verdict == "fail"
    personal = skill_artifact(scope="personal", owner_harness_id=str(uuid4()))
    assert skill.schema(personal, CTX).verdict == "fail"


def test_company_skill_must_not_carry_contacts():
    assert skill.pii_dlp(skill_artifact("Звонить по +7 900 111-22-33"), CTX).verdict == "fail"
    assert skill.pii_dlp(skill_artifact(), CTX).verdict == "pass"


def test_foreign_data_is_case_insensitive_and_skips_personal_skills():
    leaked = skill_artifact("Заявка ORDER/1042 — пример")
    personal = skill_artifact("Как у ООО Ромашка", scope="personal", owner_harness_id=str(HARNESS))
    assert skill.foreign_data(leaked, CTX).verdict == "fail"
    assert skill.foreign_data(personal, CTX).verdict == "pass"


def test_provenance_requires_summarized_source_jobs():
    unsummarized = skill_artifact()
    unsummarized["candidate"]["provenance"]["source_jobs"] = ["job-1", "job-2"]
    assert skill.provenance(unsummarized, CTX).verdict == "fail"
    assert skill.provenance(skill_artifact(), CTX).verdict == "pass"


# --- Adapter: schema и security -------------------------------------------------

@pytest.mark.parametrize("files", [
    {"../adapter.py": CRM_ADAPTER},
    {"helpers.py": "X = 1\n"},
    {"adapter.py": CRM_ADAPTER, "sub/dir.py": ""},
])
def test_adapter_schema_rejects_bad_file_layout(files):
    artifact = {**copy.deepcopy(CLEAN_ADAPTER), "files": files}
    assert adapter.schema(artifact, CTX).verdict == "fail"


@pytest.mark.parametrize("source, kind", [
    ("import os\n", "import"),
    ("from urllib import request\n", "import"),
    ("import socket\n", "import"),
    ("from . import x\n", "import"),
    ("x = eval('1')\n", "call"),
    ("y = open\n", "call"),
    ("z = getattr(object, name)\n", "call"),
    ("w = ().__class__\n", "dunder"),
    ("API_KEY = 'abcdef1234567890'\n", "secret"),
    ("HEADERS = {'Authorization': 'Bearer abcdefghijklmnop1234'}\n", "secret"),
    ("def broken(:\n", "syntax"),
])
def test_security_flags_dangerous_code(source, kind):
    artifact = with_files(CRM_ADAPTER + "\n" + source)
    kinds = {k for k, _ in adapter.security_problems(artifact)}
    assert kind in kinds
    assert adapter.security(artifact, CTX).verdict == "fail"


def test_security_allows_the_c3_import_list_and_local_modules():
    source = "import json\nimport re\nfrom urllib.parse import quote\nfrom collections import abc\nimport helpers\n" + CRM_ADAPTER
    artifact = with_files(source)
    artifact["files"]["helpers.py"] = "VALUE = 1\n"
    assert adapter.security_problems(artifact) == []


@pytest.mark.parametrize("changes", [
    {"auth": {"kind": "token", "secret_ref": "plain-token-value"}},
    {"auth": {"kind": "token", "secret_ref": "env:CRM_TOKEN", "token": "abc"}},
    {"read_scopes": ["*"]},
    {"read_scopes": ["orders:write"]},
])
def test_security_checks_manifest_secret_ref_and_scopes(changes):
    assert adapter.security(with_files(CRM_ADAPTER, **changes), CTX).verdict == "fail"


# --- Adapter: sandbox (GT-c3-04) -----------------------------------------------

def test_gt_c3_04_network_attempt_is_blocked_even_if_caught():
    source = adapter_that(
        "try:\n    socket.create_connection(('example.com', 80), timeout=1)\nexcept Exception:\n    pass",
        prelude="import socket\n",
    )
    result = run(source)
    assert not result.ok and result.reason == "blocked_operation"
    assert any(event.startswith("socket.") for event in result.blocked)


def test_gt_c3_04_reading_files_outside_the_draft_is_blocked(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("real-production-secret", encoding="utf-8")
    source = adapter_that(f"try:\n    self.leak = open({str(secret)!r}).read()\nexcept Exception:\n    pass")
    result = run(source)
    assert not result.ok and "open" in result.blocked


def test_gt_c3_04_validator_secrets_are_not_in_the_sandbox_env(monkeypatch):
    monkeypatch.setenv("VALIDATOR_SIGNING_KEY", "must-not-leak")
    source = (
        "import os\n\nclass Adapter:\n    def __init__(self, http):\n        pass\n"
        "    def capabilities(self):\n        return {'reads': [dict(os.environ)], 'writes': []}\n"
        "    def discover(self):\n        return {}\n    def probe(self, ref):\n        return {}\n"
    )
    result = run(source)
    assert result.ok, result.log
    seen_env = result.result["capabilities"]["value"]["reads"][0]
    assert set(seen_env) <= {"SYSTEMROOT"}  # окружение адаптера пустое (на Windows — только SYSTEMROOT)
    assert "must-not-leak" not in str(result.result)


@pytest.mark.parametrize("body, prelude", [
    ("try:\n    subprocess.run(['echo', 'x'])\nexcept Exception:\n    pass", "import subprocess\n"),
    ("try:\n    open('out.txt', 'w').write('x')\nexcept Exception:\n    pass", ""),
    ("try:\n    os.remove('adapter.py')\nexcept Exception:\n    pass", "import os\n"),
])
def test_gt_c3_04_processes_and_file_writes_are_blocked(body, prelude):
    result = run(adapter_that(body, prelude))
    assert not result.ok and result.reason == "blocked_operation"


def test_sandbox_rejects_http_writes_in_the_demo_slice():
    result = run(adapter_that("try:\n    http.post('/orders')\nexcept Exception:\n    pass"))
    assert not result.ok and result.reason == "write_attempt"
    assert result.writes == ["POST /orders"]


def test_sandbox_times_out_endless_adapters():
    result = run(adapter_that("while True:\n    pass"), timeout=2)
    assert not result.ok and result.reason == "timeout"


def test_sandbox_reports_adapter_errors():
    result = run(adapter_that("raise RuntimeError('boom')"))
    assert not result.ok and result.reason == "adapter_error" and "boom" in result.log


def test_sandbox_filters_lists_by_params_like_the_real_api():
    source = adapter_that("self.new = http.get('/orders', params={'status': 'new'})")
    source = source.replace("return {'reads': [], 'writes': []}", "return {'reads': [o['order_id'] for o in self.new['items']], 'writes': []}")
    result = run(source)
    assert result.ok, result.log
    assert result.result["capabilities"]["value"]["reads"] == ["1042"]


# --- Adapter: eval --------------------------------------------------------------

def eval_of(artifact: dict):
    return adapter.eval_(artifact, CTX)


def test_eval_passes_the_clean_adapter():
    assert eval_of(copy.deepcopy(CLEAN_ADAPTER)).verdict == "pass"


def test_eval_catches_lost_sensitivity_labels():
    source = CRM_ADAPTER.replace('"sensitivity": labels,', '"sensitivity": {},')
    outcome = eval_of(with_files(source))
    assert outcome.verdict == "fail" and "lost sensitivity" in outcome.log


def test_eval_catches_data_that_differs_from_the_stand():
    source = CRM_ADAPTER.replace('"data": data,', '"data": {k: v for k, v in data.items() if k != "client"},')
    assert eval_of(with_files(source)).verdict == "fail"


def test_eval_catches_undeclared_writes():
    source = CRM_ADAPTER.replace('"writes": []}', '"writes": [{"action_class": "crm.order.set_status"}]}')
    outcome = eval_of(with_files(source))
    assert outcome.verdict == "fail" and "capabilities().writes" in outcome.log


def test_eval_rejects_test_refs_outside_the_stand():
    outcome = eval_of(with_files(CRM_ADAPTER, tests=["order/1043"]))  # заявка manager-b не видна
    assert outcome.verdict == "fail" and "not in the test stand" in outcome.log


def test_unknown_test_data_is_not_executed():
    artifact = {**copy.deepcopy(CLEAN_ADAPTER), "test_data_ref": "prod-crm"}
    assert adapter.sandbox(artifact, CTX).metrics["reason"] == "unknown_test_data"


# --- стенд совпадает с настоящим demo-landscape -------------------------------

def test_stand_matches_the_real_demo_landscape_api():
    client = TestClient(create_landscape(Landscape.load(), {"t": Grant(STAND_USER, frozenset(STAND.routes))}))
    for system, routes in STAND.routes.items():
        for path, body in routes.items():
            response = client.get(f"/{system}{path}", headers={"Authorization": "Bearer t"})
            assert response.status_code == 200, (system, path)
            assert response.json() == body, (system, path)


def test_context_is_typed():
    assert isinstance(CTX.harness_id, UUID)
