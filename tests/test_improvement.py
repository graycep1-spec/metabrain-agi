"""T-37 (ADR-029): отладчик читает телеметрию → предложение self_improvement → согласие → применение."""

import json
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID

import httpx
from fastapi.testclient import TestClient

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.wiring import build_connector
from src.metabrain.policy import load_policy
from src.web.app import create_app as create_web, http_harness
from src.workforce.improvement import ImprovementFlow
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime
from src.workforce.store import WorkforceStore
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, harness_env
from tests.test_catalog import _Failing
from tests.test_web_auth import API_TOKEN, FakeTelegram, claims, make_auth, telegram_id
from tests.test_web_cabinet import FORM, csrf, post
from tests.test_workforce import active_twin


class FakeDebugger:
    """Модель отладчика (deepseek-v4-pro): диагноз + патч эскалации."""

    def __init__(self, patch=None):
        self.patch = patch or {"escalation": [{"condition": "нет позиции и подходящего аналога"},
                                              {"condition": "ошибка адаптера повторяется"}]}
        self.calls = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.calls.append(body)
        answer = {"diagnosis": "Падает из-за отсутствия правила эскалации при ошибке адаптера.",
                  "patch": self.patch}
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 200, "total_tokens": 1100},
        })


def improvement_for(env, debugger: FakeDebugger) -> ImprovementFlow:
    settings = load_settings({"SERVICE_NAME": "harness", "LLM_PROVIDER": "deepseek",
                              "DEEPSEEK_API_BASE": "https://api.deepseek.test"})
    secrets = EnvSecretStore({"DEEPSEEK_API_KEY": "sk-test-not-real",
                              "CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY})
    connector = build_connector(load_policy(), settings, env.connection, secrets,
                                http_client=httpx.Client(transport=httpx.MockTransport(debugger.handler)))
    return ImprovementFlow(env.connection, connector=connector, workforce=env.workforce,
                           journal=env.journal, clock=env.clock)


def break_twice(env, me) -> UUID:
    """Активный двойник + две упавшие работы одной обязанности (без авто-диагноста)."""
    active_twin(env, me)
    store = WorkforceStore(env.connection)
    employee_id = env.connection.execute(
        "SELECT employee_id FROM wf_employees WHERE kind <> 'researcher' LIMIT 1").fetchone()[0]
    employee = store.get_employee(employee_id=employee_id, harness_id=me.harness_id)
    runtime = Runtime(env.connection, handler=_Failing(), sink=HarnessReportSink(env.connection),
                      clock=env.clock, auto_diagnose=False)
    for text in ("раз", "два"):
        assert runtime.run_job(runtime.enqueue(employee, duty_id="d1", trigger="event",
                                               input={"text": text})) == "failed"
    return employee_id


def test_scan_finds_repeated_failures_and_blocked_answers():
    with harness_env() as env:
        me = env.register("a")
        employee_id = break_twice(env, me)
        env.journal.append_entry(person_id=me.person_id, company_id=me.company_id, harness_id=me.harness_id,
                                 kind="answer.blocked", employee_id=employee_id, payload={"reasons": ["x"]})
        env.journal.append_entry(person_id=me.person_id, company_id=me.company_id, harness_id=me.harness_id,
                                 kind="answer.blocked", employee_id=employee_id, payload={"reasons": ["y"]})
        flow = improvement_for(env, FakeDebugger())
        findings = flow.scan(me)
        kinds = {f["kind"] for f in findings}
        assert kinds == {"failed_jobs", "answers_blocked"}
        assert all(f["count"] >= 2 for f in findings)


def test_gt_t37_failure_pattern_becomes_a_gated_proposal_and_applies_on_consent():
    """GT T-37: упавшая работа (повтор) → драфт-предложение; применение только после согласия."""
    with harness_env() as env:
        me = env.register("a")
        employee_id = break_twice(env, me)
        flow = improvement_for(env, FakeDebugger())

        result = flow.run(me)
        assert len(result["proposals"]) == 1, result
        proposal_id = UUID(result["proposals"][0]["proposal_id"])

        row = env.connection.execute(
            "SELECT kind, subject, score_ref FROM proposals WHERE id = %s", (proposal_id,)).fetchone()
        assert row[0] == "self_improvement"
        subject = row[1]
        assert "эскалации" in subject["diagnosis"] and subject["finding"]["kind"] == "failed_jobs"
        assert subject["diff"] and row[2]            # валидатор прошёл, diff есть
        # версия паспорта лежит как proposed, действующая не изменилась (применение — только после согласия)
        versions = env.connection.execute(
            "SELECT version, status FROM wf_passports WHERE employee_id = %s ORDER BY version",
            (employee_id,)).fetchall()
        assert versions[-1][1] == "proposed" and versions[0][1] == "active"
        # журнал
        assert env.connection.execute(
            "SELECT count(*) FROM journal_entries WHERE kind = 'self_improvement.proposed'").fetchone()[0] == 1

        # повторный запуск — дедуп, второго предложения нет
        again = flow.run(me)
        assert not again["proposals"] and any(s.get("reason") == "already_pending" for s in again["skipped"])

        # согласие куратора применяет версию штатным механизмом
        token = env.approve(me, proposal_id)
        applied = env.workforce.on_passport_approved(me, proposal_id=proposal_id, token=token)
        assert applied["status"] == "active"
        active = env.workforce.get(me, employee_id=UUID(str(employee_id)))["passport"]
        assert any("повторяется" in e["condition"] for e in active["escalation"])


def test_cabinet_runs_improvement_and_shows_debugger_diagnosis():
    with harness_env() as env:
        me = env.register("a")
        break_twice(env, me)
        debugger = FakeDebugger()
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        harness = TestClient(create_harness(
            env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
            secret_store=EnvSecretStore({"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY}),
            web_auth=auth, workforce=env.workforce, researcher=env.researcher,
            improvement=improvement_for(env, debugger),
        ))
        web = TestClient(create_web(http_harness("http://harness", API_TOKEN, client=harness),
                                    web_base_url="https://cabinet.example.test"),
                         base_url="https://cabinet.example.test")
        start = web.get("/login/start", follow_redirects=False)
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))
        assert web.get(f"/auth/callback?code=code&state={query['state']}").status_code == 200

        home = web.get("/")
        assert "Самосовершенствование" in home.text and "находок:" in home.text

        run = post(web, "/improve", {"csrf_token": csrf(web)})
        assert run.status_code == 303 and run.headers["location"] == "/proposals?notice=improved"
        proposals = web.get("/proposals")
        assert "Диагноз отладчика" in proposals.text and "Самосовершенствование" in proposals.text
