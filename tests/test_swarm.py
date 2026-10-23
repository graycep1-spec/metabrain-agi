"""T-14: рой ЦС и `ask` — GT-c1-01, -02, -07, R-04, R-05; отчёт по расписанию с источниками (C-6, ADR-026).

Коннектор и бюджет — настоящие (C-2, ADR-021), вместо DeepSeek — фейковый HTTP-провайдер.
"""

from datetime import timedelta
from decimal import Decimal
import json

from fastapi.testclient import TestClient
import httpx
import pytest

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.wiring import build_connector
from src.metabrain.policy import load_policy
from src.validator.scorecard import verify_scorecard
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime
from src.workforce.swarm import SwarmHandler, build_answer, clean_claims, fact_lines
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, LANDSCAPE_TOKEN_A, harness_env
from tests.test_workforce import active_twin


API_TOKEN = "swarm-api-token"
USER = "10" + "a".encode().hex()
PII = ("ООО Ромашка", "sokolova@romashka.example", "+7 900 111-22-33", "Ирина Соколова")


class FakeDeepSeek:
    """OpenAI-совместимый /chat/completions: аналитик — deepseek-flash, критик — deepseek-v4-pro."""

    def __init__(self, analyst=None, critic=None):
        self.analyst = analyst or {
            "claims": [{"text": "В заявке 1042 позиции A-17 нужно 10 шт.", "sources": ["crm:order/1042"]}],
            "recommendation": {"text": "Предложить клиенту аналог для A-17.", "sources": ["crm:order/1042"]},
        }
        self.critic = critic or {"unsupported": [], "notes": "ok"}
        self.bodies: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        self.bodies.append(raw)
        body = json.loads(raw)
        answer = self.analyst if body["model"] == "deepseek-flash" else self.critic
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500},
        })


def swarm_for(env, provider: FakeDeepSeek) -> SwarmHandler:
    settings = load_settings({"SERVICE_NAME": "harness", "LLM_PROVIDER": "deepseek",
                              "DEEPSEEK_API_BASE": "https://api.deepseek.test"})
    secrets = EnvSecretStore({
        "DEEPSEEK_API_KEY": "sk-test-not-real",
        "CONSENT_SIGNING_KEY": CONSENT_KEY,
        "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
    })
    connector = build_connector(load_policy(), settings, env.connection, secrets,
                                http_client=httpx.Client(transport=httpx.MockTransport(provider.handler)))
    return SwarmHandler(env.connection, connector=connector, validator=env.validator,
                        collect=env.researcher.collect_facts, proposals=env.proposals, journal=env.journal)


def connected_twin(env, me, secret_ref="env:CRM_TOKEN_A"):
    """Двойник с действующим паспортом и доступом к crm (T-13 + T-19)."""
    active_twin(env, me)
    proposal = env.connect(me, secret_ref=secret_ref)
    connected = env.researcher.on_connect_approved(me, proposal_id=proposal.id, token=env.approve(me, proposal.id))
    adapter_id = connected["adapter_proposal_id"]
    applied = env.researcher.on_adapter_approved(me, proposal_id=adapter_id, token=env.approve(me, adapter_id))
    extension = applied["passport_proposals"][0]
    env.workforce.on_passport_approved(me, proposal_id=extension, token=env.approve(me, extension))
    env.connection.commit()


def api_for(env, provider):
    swarm = swarm_for(env, provider)
    runtime = Runtime(env.connection, handler=swarm, sink=HarnessReportSink(env.connection), clock=env.clock)
    app = create_harness(env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
                         secret_store=EnvSecretStore({
                             "CONSENT_SIGNING_KEY": CONSENT_KEY,
                             "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                         }),
                         workforce=env.workforce, researcher=env.researcher, ask_runtime=runtime)
    return TestClient(app)


def ask(client, text="Что с заявкой 1042?"):
    response = client.post("/v1/operations/ask", json={"channel_user_id": USER, "text": text},
                           headers={"Authorization": f"Bearer {API_TOKEN}"})
    return response.status_code, response.json()


# --- без БД: сборка ответа ------------------------------------------------------------------

FACTS = [{"system": "crm", "ref": "order/1042", "data": {"order_id": "1042", "client": "ООО Ромашка"},
          "source": {"system": "crm", "object_ref": "order/1042", "read_at": "2026-10-22T09:00:00+00:00"},
          "sensitivity": {"client": "pii"}}]


def test_facts_go_to_the_model_only_as_placeholders():
    lines = fact_lines(FACTS)
    assert "{{fact:crm:order/1042.client}}" in lines and "Ромашка" not in lines


def test_claims_without_known_sources_are_dropped_and_text_is_built_only_from_claims():
    claims, recommendation, dropped = clean_claims({
        "claims": [{"text": "Факт с источником", "sources": ["crm:order/1042"]},
                   {"text": "Выдумка", "sources": []}, {"text": "Чужой источник", "sources": ["erp:x/1"]}],
        "recommendation": {"text": "Совет без источника", "sources": []},
    }, {"crm:order/1042"})
    assert [c["text"] for c in claims] == ["Факт с источником"] and recommendation is None and dropped == 3
    answer = build_answer(claims, None, FACTS)
    assert answer["text"] == "• Факт с источником (источник: crm order/1042)"
    assert answer["claims"] == [{"claim_id": "c1", "text": "Факт с источником", "source_ids": ["crm:order/1042"]}]


# --- GT-c1-01: ответ по данным подключённой системы ------------------------------------------

def test_gt_c1_01_ask_answers_with_sources_and_a_valid_scorecard():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        provider = FakeDeepSeek()
        status, body = ask(api_for(env, provider))

        assert status == 200
        assert body["sources"][0]["object_ref"] == "order/1042" and body["sources"][0]["system"] == "crm"
        assert "Рекомендация: Предложить клиенту аналог" in body["answer"]
        answer_artifact = {"text": body["answer"], "claims": [
            {"claim_id": "c1", "text": "В заявке 1042 позиции A-17 нужно 10 шт.", "source_ids": ["crm:order/1042"]},
            {"claim_id": "c2", "text": "Предложить клиенту аналог для A-17.", "source_ids": ["crm:order/1042"]},
        ], "sources": body["sources"]}
        state = env.connection.execute(
            "SELECT result FROM wf_job_steps WHERE name = 'validate' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()[0]
        verify_scorecard(state, env.validator.keys(), artifact=answer_artifact, harness_id=me.harness_id)
        assert state["scorecard_id"] == body["score_ref"]

        proposals = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="recommendation")
        assert [str(p.id) for p in proposals] == body["proposal_ids"] and proposals[0].score_ref == body["score_ref"]


def test_pii_never_reaches_the_external_model_and_cost_is_counted():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        provider = FakeDeepSeek()
        assert ask(api_for(env, provider))[0] == 200

        assert len(provider.bodies) == 2                                   # аналитик и критик
        assert {json.loads(b)["model"] for b in provider.bodies} == {"deepseek-flash", "deepseek-v4-pro"}
        for body in provider.bodies:
            for value in PII:
                assert value not in body                                    # C-2, GT-c2-07
            assert LANDSCAPE_TOKEN_A not in body
        spent = env.connection.execute("SELECT count(*), sum(cost_usd) FROM llm_usage").fetchone()
        assert spent[0] == 2 and Decimal(spent[1]) > 0


def test_gt_c1_07_r_05_no_sub_role_names_in_the_answer():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        _, body = ask(api_for(env, FakeDeepSeek()))
        lowered = json.dumps(body, ensure_ascii=False).lower()
        for name in ("аналитик", "критик", "разработчик", "analyst", "critic", "developer"):
            assert name not in lowered


# --- GT-c1-02: без источника ответ не выдаётся -----------------------------------------------

@pytest.mark.parametrize("provider", [
    FakeDeepSeek(analyst={"claims": [{"text": "Клиент готов ждать две недели", "sources": []}], "recommendation": None}),
    FakeDeepSeek(analyst={"claims": [{"text": "Остаток A-17 — 6", "sources": ["erp:item/A-17"]}]}),
    FakeDeepSeek(critic={"unsupported": [1, 2]}),
    FakeDeepSeek(analyst={"claims": [{"text": "Оплата картой 4111 1111 1111 1111", "sources": ["crm:order/1042"]}]}),
])
def test_gt_c1_02_unsupported_or_unsafe_answers_are_blocked(provider):
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        status, body = ask(api_for(env, provider))
        assert status == 422 and body["error"] == "answer_blocked" and body["message"]
        kinds = [r[0] for r in env.connection.execute("SELECT kind FROM journal_entries").fetchall()]
        assert "answer.blocked" in kinds
        assert env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="recommendation") == ()


def test_without_connected_systems_the_answer_is_blocked_with_a_hint():
    with harness_env() as env:
        me = env.register("a")
        active_twin(env, me)
        provider = FakeDeepSeek()
        status, body = ask(api_for(env, provider))
        assert status == 422 and "подключённых систем" in body["message"]
        assert provider.bodies == []                                       # модель даже не вызывалась


def test_ask_errors_no_employee_busy_and_budget():
    with harness_env() as env:
        me = env.register("a")
        provider = FakeDeepSeek()
        assert ask(api_for(env, provider))[0] == 404                       # нет активного сотрудника

        connected_twin(env, me)
        twin = env.workforce.employee_for_ask(me)
        env.connection.execute(
            "INSERT INTO wf_jobs (job_id, company_id, harness_id, employee_id, duty_id, trigger, status) "
            "VALUES (gen_random_uuid(), %s, %s, %s, 'ask', 'ask', 'running')",
            (me.company_id, me.harness_id, twin.employee_id),
        )
        env.connection.commit()
        assert ask(api_for(env, provider)) == (409, {"error": "employee_busy"})
        env.connection.execute("UPDATE wf_jobs SET status = 'done'")
        env.connection.execute(
            "INSERT INTO llm_usage (id, harness_id, employee_id, provider, model, prompt_tokens, completion_tokens, "
            "total_tokens, cost_usd) VALUES (gen_random_uuid(), 'other', 'x', 'deepseek-flash', 'm', 0, 0, 0, 4.999)"
        )
        env.connection.commit()
        status, body = ask(api_for(env, provider))
        assert status == 429 and body["error"] == "budget_exceeded"        # потолок установки 5 $ (ADR-021)


# --- работа по расписанию: отчёт с источниками куратору ----------------------------------------

def test_scheduled_duty_reports_with_sources_to_the_curator():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        sent = []
        rt_connection = env.connect_new_connection()
        provider = FakeDeepSeek()
        settings = load_settings({"SERVICE_NAME": "harness", "DEEPSEEK_API_BASE": "https://api.deepseek.test"})
        connector = build_connector(load_policy(), settings, rt_connection,
                                    EnvSecretStore({"DEEPSEEK_API_KEY": "sk-test-not-real"}),
                                    http_client=httpx.Client(transport=httpx.MockTransport(provider.handler)))
        from src.harness.journal import JournalStore
        from src.harness.proposals import ProposalStore
        from src.landscape.service import ResearcherService
        from tests.support_harness import registry

        proposals = ProposalStore(rt_connection, secret_store=EnvSecretStore({
                                      "CONSENT_SIGNING_KEY": CONSENT_KEY,
                                      "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                                  }),
                                  signing_key_ref="env:CONSENT_SIGNING_KEY")
        journal = JournalStore(rt_connection)
        researcher = ResearcherService(
            rt_connection, harness=env.harness, proposals=proposals, journal=journal, validator=env.validator,
            registry=registry(), secret_store=EnvSecretStore({"CRM_TOKEN_A": LANDSCAPE_TOKEN_A}),
            http_client=env.landscape_http,
        )
        swarm = SwarmHandler(rt_connection, connector=connector, validator=env.validator,
                             collect=researcher.collect_facts, proposals=proposals, journal=journal)
        runtime = Runtime(rt_connection, handler=swarm,
                          sink=HarnessReportSink(rt_connection, send=lambda chat, text: sent.append((chat, text))),
                          clock=env.clock)

        env.clock.advance(minutes=15)
        tick = runtime.tick()
        assert (tick.enqueued, tick.finished) == (1, 1)
        assert sent[0][0] == USER and "(источник: crm order/1042)" in sent[0][1]
        task = json.loads(provider.bodies[0])["messages"][0]["content"]
        assert "Выполни обязанность «Разбирать новые заявки»" in task
        report = env.connection.execute(
            "SELECT payload, sources, score_ref FROM journal_entries WHERE kind = 'report' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert report[1][0]["object_ref"] == "order/1042" and report[2]
        _ = timedelta
