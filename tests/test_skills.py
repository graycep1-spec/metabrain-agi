"""T-23: навыки из работы — рекомендация → личный навык → навык компании по согласию → в работе другого ЦС.

C-6 «Навыки из работы», C-5 `skill.propose`/`skill.publish`, ADR-027; GT-dl-04, GT-dl-05 (сценарий навыка из демо). Мета-мозг — настоящий, в процессе, на той же БД;
коннектор и бюджет — настоящие, вместо DeepSeek — фейковый HTTP-провайдер.
"""

import base64
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import httpx
import psycopg
import pytest

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.wiring import build_connector
from src.metabrain.client import HttpMetabrainClient
from src.metabrain.policy import load_policy
from src.metabrain.service import Metabrain, Rejected, Unavailable
from src.metabrain.store import migrate
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime
from src.workforce.skills import SkillFlow, clean_skill, identifiers
from src.web.app import create_app as create_web, http_harness
from src.workforce.swarm import SwarmHandler, build_answer, visible_skills
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY
from tests.support_harness import harness_env
from tests.test_swarm import FACTS, PII, FakeDeepSeek, connected_twin


API_TOKEN = "skills-api-token"
USER_A, USER_B = "10" + "a".encode().hex(), "10" + "b".encode().hex()
SKILL = {
    "title": "Аналог при нехватке позиции",
    "body": "Если позиции не хватает на складе, предложи аналог из analogs[] с той же категорией и маржой не ниже min_margin.",
    "task_class": "quote", "role_tags": ["sales"], "examples": ["Нет нужного количества — аналог с близкой ценой"],
}


def consent_public_key():
    raw = base64.b64decode(CONSENT_KEY)
    return Ed25519PrivateKey.from_private_bytes(raw).public_key()


def reply(model, answer):
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
    })


class SkillDeepSeek(FakeDeepSeek):
    """Разработчик обобщает рекомендацию в навык; аналитик ссылается на навык, если он есть в промпте."""

    def __init__(self, skill=None):
        super().__init__()
        self.skill = skill or SKILL
        self.analyst_prompts: list[str] = []

    def handler(self, request):
        raw = request.content.decode("utf-8")
        body = json.loads(raw)
        text = " ".join(m.get("content", "") for m in body["messages"])
        if "разработчик цифровых сотрудников" in text:
            self.bodies.append(raw)
            return reply(body["model"], self.skill)
        if body["model"] == "deepseek-flash":
            self.analyst_prompts.append(text)
            skills = re.findall(r"\[(skill:[^\]]+)\]", text)
            orders = re.findall(r"\[(crm:order/[^\]]+)\]", text)
            if skills and orders:
                self.bodies.append(raw)
                return reply(body["model"], {
                    "claims": [{"text": "В заявке есть позиция в дефиците.", "sources": [orders[0]]}],
                    "recommendation": {"text": "Предложить аналог по правилу компании.",
                                       "sources": [skills[0], orders[0]]},
                })
        return super().handler(request)


class FlakyMetabrain:
    """Мета-мозг, у которого отказывает одна операция."""

    def __init__(self, brain, broken: str):
        self._brain, self._broken = brain, broken

    def __getattr__(self, name):
        if name == self._broken:
            def fail(request):
                raise Unavailable("database_unavailable")
            return fail
        return getattr(self._brain, name)


def brain_for(env) -> Metabrain:
    url = os.environ["TEST_DATABASE_URL"]

    def connect():
        return psycopg.connect(url, options=f"-c search_path={env.schema}")

    with connect() as connection:
        migrate(connection)
    return Metabrain(connect, env.validator, load_policy(), consent_public_key())


def stack(env, provider, metabrain, web_auth=None):
    settings = load_settings({"SERVICE_NAME": "harness", "LLM_PROVIDER": "deepseek",
                              "DEEPSEEK_API_BASE": "https://api.deepseek.test"})
    secrets = EnvSecretStore({
        "DEEPSEEK_API_KEY": "sk-test-not-real",
        "CONSENT_SIGNING_KEY": CONSENT_KEY,
        "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
    })
    connector = build_connector(load_policy(), settings, env.connection, secrets,
                                http_client=httpx.Client(transport=httpx.MockTransport(provider.handler)))
    swarm = SwarmHandler(env.connection, connector=connector, validator=env.validator,
                         collect=env.researcher.collect_facts, proposals=env.proposals, journal=env.journal,
                         skills=lambda **scope: visible_skills(metabrain, **scope))
    flow = SkillFlow(env.connection, connector=connector, metabrain=metabrain, proposals=env.proposals,
                     journal=env.journal, clock=env.clock)
    runtime = Runtime(env.connection, handler=swarm, sink=HarnessReportSink(env.connection), clock=env.clock)
    app = create_harness(env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
                         secret_store=secrets, workforce=env.workforce, researcher=env.researcher,
                         ask_runtime=runtime, skills=flow if metabrain is not None else None, web_auth=web_auth)
    return TestClient(app)


def cabinet(env, provider, metabrain):
    """Кабинет со входом по Telegram OIDC поверх того же харнесса."""
    from tests.test_web_auth import FakeTelegram, claims, make_auth, telegram_id

    telegram = FakeTelegram()
    harness = stack(env, provider, metabrain, web_auth=make_auth(env, telegram))
    web = TestClient(create_web(http_harness("http://harness", API_TOKEN, client=harness),
                                web_base_url="https://cabinet.example.test"), base_url="https://cabinet.example.test")
    start = web.get("/login/start", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))
    assert web.get(f"/auth/callback?code=code&state={query['state']}").status_code == 200
    return harness, web


def share_form(web, skill_id):
    token = web.cookies.get("mb_session").split(".", 1)[1]
    return web.post(f"/skills/{skill_id}/share", content=urlencode({"csrf_token": token}),
                    headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)


def call(client, user, op, **payload):
    response = client.post(f"/v1/operations/{op}", json={"channel_user_id": user, **payload},
                           headers={"Authorization": f"Bearer {API_TOKEN}"})
    return response.status_code, response.json()


def approve(client, user, proposal_id):
    return call(client, user, "proposal.decide", proposal_id=proposal_id, decision="approve", surface="telegram")


def colleague(env, me):
    """Второй человек той же компании со своим двойником (харнесс B)."""
    other = env.harness.register_person(channel="telegram", channel_user_id=USER_B, trusted_company_id=me.company_id)
    env.connection.commit()
    connected_twin(env, other, secret_ref="env:CRM_TOKEN_B")  # manager-b видит свои заявки (1043)
    return other


def kinds(env, identity, prefix="skill."):
    rows = env.connection.execute(
        "SELECT kind, payload FROM journal_entries WHERE harness_id = %s AND kind LIKE %s ORDER BY created_at, id",
        (identity.harness_id, prefix + "%"),
    ).fetchall()
    return [(kind, payload) for kind, payload in rows]


# --- без БД ---------------------------------------------------------------------------------

def test_skill_draft_is_cleaned_and_identifiers_come_from_sensitive_facts():
    cleaned = clean_skill({**SKILL, "task_class": "Не латиница", "role_tags": ["sales", "", 5], "examples": "x"})
    assert cleaned["task_class"] == "general" and cleaned["role_tags"] == ["sales"] and cleaned["examples"] == []
    with pytest.raises(ValueError):
        clean_skill({"title": " ", "body": "x"})
    facts = FACTS + [{"system": "stock", "ref": "item/A-17", "data": {"sku": "A-17", "qty": 4}}]
    # Ключ и значение чувствительного поля — да; объект без чувствительных полей — нет.
    assert identifiers(facts) == ["1042", "order/1042", "ООО Ромашка"]


def test_skill_sources_are_named_in_the_answer_and_carry_skill_ref():
    skill = {"skill_id": "5f0c", "version": "1.0.0", "scope": "company", "title": "Аналог", "body": "...",
             "created_at": "2026-10-22T09:00:00+00:00"}
    answer = build_answer([], {"text": "Предложить аналог.", "sources": ["skill:5f0c@1.0.0", "crm:order/1042"]}, FACTS, [skill])
    assert answer["text"] == "Рекомендация: Предложить аналог. (источник: навык «Аналог», crm order/1042)"
    assert answer["sources"][0] == {"source_id": "skill:5f0c@1.0.0", "system": "skill", "object_ref": "5f0c@1.0.0",
                                    "skill_ref": "5f0c@1.0.0", "read_at": "2026-10-22T09:00:00+00:00"}


def test_http_metabrain_client_maps_errors():
    def handler(request):
        if request.url.path.endswith("/publish"):
            return httpx.Response(403, json={"error": "consent_reused"})
        if request.url.path.endswith("/propose"):
            return httpx.Response(502, text="bad gateway")
        return httpx.Response(200, json={"skills": []})

    client = HttpMetabrainClient("http://metabrain:8080", transport=httpx.MockTransport(handler))
    assert client.resolve({"company_id": "c", "harness_id": "h"}) == {"skills": []}
    with pytest.raises(Rejected) as rejected:
        client.publish({})
    assert rejected.value.code == "consent_reused"
    with pytest.raises(Unavailable):
        client.propose({})
    assert visible_skills(FlakyMetabrain(client, "resolve"), company_id="c", harness_id="h") == []


# --- GT-dl-04, GT-dl-05: опыт одного ЦС через гейт доходит до другого -------------------------

def test_gt_dl_04_05_recommendation_becomes_a_company_skill_and_twin_b_uses_it():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        other = colleague(env, me)
        brain = brain_for(env)
        provider = SkillDeepSeek()
        client = stack(env, provider, brain)

        status, answer = call(client, USER_A, "ask", text="Что с заявкой 1042?")
        assert status == 200 and len(answer["proposal_ids"]) == 1
        status, decided = approve(client, USER_A, answer["proposal_ids"][0])
        assert status == 200 and decided["status"] == "approved"
        skill = decided["applied"]["skill"]
        assert skill["personal"]["status"] == "published" and skill["personal"]["version"] == "1.0.0"
        assert skill["company"]["status"] == "proposed"

        # Разработчику — только плейсхолдеры после DLP: ПДн клиента до внешней модели не доходят.
        developer = [b for b in provider.bodies if "разработчик цифровых сотрудников" in b]
        assert developer and not any(p in body for body in developer for p in PII)

        # Личный навык виден только автору; навык компании до согласия не виден никому.
        scope_a = {"company_id": str(me.company_id), "harness_id": str(me.harness_id)}
        scope_b = {"company_id": str(other.company_id), "harness_id": str(other.harness_id)}
        assert [s["scope"] for s in brain.resolve(scope_a)["skills"]] == ["personal"]
        assert brain.resolve(scope_b)["skills"] == []

        pending = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="skill_publish")
        assert [str(p.id) for p in pending] == [skill["company"]["proposal_id"]]
        assert "Аналог при нехватке позиции" in pending[0].summary and pending[0].score_ref
        assert set(pending[0].subject) == {"candidate_id", "artifact_hash"}

        status, published = approve(client, USER_A, skill["company"]["proposal_id"])
        assert status == 200 and published["applied"]["skill"]["status"] == "published"
        company_skill = published["applied"]["skill"]
        visible_b = brain.resolve(scope_b)["skills"]
        assert [(s["scope"], s["skill_id"]) for s in visible_b] == [("company", company_skill["skill_id"])]
        assert visible_b[0]["provenance"]["consent_ref"]
        # GT-dl-04: в навыке компании нет имени клиента и номера заявки.
        published_text = json.dumps(visible_b[0], ensure_ascii=False)
        assert "Ромашка" not in published_text and "1042" not in published_text

        # GT-dl-05: двойник B по своей заявке 1043 получает навык компании (личный навык A ему не виден)
        # и ссылается на него в ответе.
        status, answer_b = call(client, USER_B, "ask", text="Что с заявкой 1043?")
        assert status == 200, answer_b
        assert any(s["object_ref"] == "order/1043" for s in answer_b["sources"])
        assert not any(s["object_ref"] == "order/1042" for s in answer_b["sources"])
        ref = f"{company_skill['skill_id']}@{company_skill['version']}"
        assert {"system": "skill", "object_ref": ref, "skill_ref": ref} .items() <= next(
            s for s in answer_b["sources"] if s["system"] == "skill").items()
        assert "навык «Аналог при нехватке позиции»" in answer_b["answer"]
        prompt_b = provider.analyst_prompts[-1]
        assert re.findall(r"\[skill:([^\]]+)\]", prompt_b) == [ref]

        journal_a = [kind for kind, _ in kinds(env, me)]
        assert journal_a == ["skill.published", "skill.published"]


def test_company_skill_with_identifiers_from_the_job_is_not_offered_for_publication():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        leaky = {**SKILL, "body": "Для заявки 1042 и похожих — предложить аналог из analogs[]."}
        client = stack(env, SkillDeepSeek(skill=leaky), brain_for(env))

        _, answer = call(client, USER_A, "ask", text="Что с заявкой 1042?")
        status, decided = approve(client, USER_A, answer["proposal_ids"][0])

        assert status == 200
        skill = decided["applied"]["skill"]
        assert skill["personal"]["status"] == "published"
        assert skill["company"] == {"status": "rejected", "reasons": ["foreign_data"]}
        assert env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="skill_publish") == ()
        rejected = [p for k, p in kinds(env, me) if k == "skill.rejected"]
        assert rejected[0]["scope"] == "company" and rejected[0]["reasons"] == ["foreign_data"]
        assert "1042" not in json.dumps(rejected, ensure_ascii=False)


def test_skill_with_personal_data_is_rejected_entirely():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        unsafe = {**SKILL, "body": "Писать менеджеру на sokolova@romashka.example и предлагать аналог."}
        brain = brain_for(env)
        client = stack(env, SkillDeepSeek(skill=unsafe), brain)

        _, answer = call(client, USER_A, "ask", text="Что с заявкой 1042?")
        status, decided = approve(client, USER_A, answer["proposal_ids"][0])

        assert status == 200 and decided["applied"]["skill"] == {"personal": {"status": "rejected", "reasons": ["pii_dlp"]}}
        assert brain.resolve({"company_id": str(me.company_id), "harness_id": str(me.harness_id)})["skills"] == []


def test_skill_failure_does_not_undo_the_curator_decision():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        client = stack(env, SkillDeepSeek(), FlakyMetabrain(brain_for(env), "propose"))

        _, answer = call(client, USER_A, "ask", text="Что с заявкой 1042?")
        status, decided = approve(client, USER_A, answer["proposal_ids"][0])

        assert status == 200 and decided["status"] == "approved"
        assert decided["applied"]["skill"] == {"status": "failed", "reason": "database_unavailable"}
        assert [k for k, _ in kinds(env, me)] == ["skill.failed"]


def test_publication_waits_for_the_metabrain_and_consent_is_not_spent():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        brain = brain_for(env)
        client = stack(env, SkillDeepSeek(), brain)
        _, answer = call(client, USER_A, "ask", text="Что с заявкой 1042?")
        _, decided = approve(client, USER_A, answer["proposal_ids"][0])
        proposal_id = decided["applied"]["skill"]["company"]["proposal_id"]

        down = stack(env, SkillDeepSeek(), FlakyMetabrain(brain, "publish"))
        status, body = approve(down, USER_A, proposal_id)
        assert status == 503 and body["error"] == "workforce_unavailable"
        still = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="skill_publish")
        assert [p.status for p in still] == ["pending"]

        status, body = approve(client, USER_A, proposal_id)
        assert status == 200 and body["applied"]["skill"]["status"] == "published"


# --- T-34: экран «Навыки» --------------------------------------------------------------------------

def test_cabinet_lists_skills_and_shares_a_personal_one_again_after_a_return():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        harness, web = cabinet(env, SkillDeepSeek(), brain_for(env))
        _, answer = call(harness, USER_A, "ask", text="Что с заявкой 1042?")
        _, decided = approve(harness, USER_A, answer["proposal_ids"][0])
        personal_id = decided["applied"]["skill"]["personal"]["skill_id"]
        # Куратор вернул публикацию в компанию — навык остался личным.
        status, _ = call(harness, USER_A, "proposal.decide", proposal_id=decided["applied"]["skill"]["company"]["proposal_id"],
                         decision="return", surface="telegram")
        assert status == 200

        page = web.get("/skills")
        assert page.status_code == 200 and "Аналог при нехватке позиции" in page.text
        assert f"/skills/{personal_id}/share" in page.text and "Навыки компании" in page.text

        shared = share_form(web, personal_id)
        assert shared.status_code == 303 and shared.headers["location"] == "/proposals?notice=shared"
        pending = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="skill_publish",
                                               status="pending")
        assert len(pending) == 1
        status, published = approve(harness, USER_A, str(pending[0].id))
        assert status == 200 and published["applied"]["skill"]["status"] == "published"

        status, listed = call(harness, USER_A, "skills.list", scope="company")
        assert status == 200 and [(s["scope"], s["consent"], s["mine"]) for s in listed["skills"]] == [("company", True, True)]
        assert listed["skills"][0]["source_jobs"] == [answer_job(env, me)]
        company_id = listed["skills"][0]["skill_id"]
        assert share_form(web, company_id).status_code == 404  # делиться можно только своим личным навыком
        assert call(harness, USER_A, "skill.share", skill_id="00000000-0000-0000-0000-000000000000")[0] == 404


def test_sharing_a_personal_skill_with_job_identifiers_is_refused_with_reasons():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)
        leaky = {**SKILL, "body": "Для заявки 1042 и похожих — предложить аналог из analogs[]."}
        harness, web = cabinet(env, SkillDeepSeek(skill=leaky), brain_for(env))
        _, answer = call(harness, USER_A, "ask", text="Что с заявкой 1042?")
        _, decided = approve(harness, USER_A, answer["proposal_ids"][0])
        personal_id = decided["applied"]["skill"]["personal"]["skill_id"]

        status, body = call(harness, USER_A, "skill.share", skill_id=personal_id)
        assert status == 422 and body == {"error": "skill_rejected", "reasons": ["foreign_data"]}
        page = share_form(web, personal_id)
        assert page.status_code == 422 and "остаётся личным" in page.text and "1042" not in page.text


def test_without_metabrain_skills_are_unavailable():
    with harness_env() as env:
        env.register("a")
        harness, web = cabinet(env, SkillDeepSeek(), None)
        assert call(harness, USER_A, "skills.list")[0] == 501
        page = web.get("/skills")
        assert page.status_code == 501 and "Мета-мозг не подключён" in page.text


def answer_job(env, identity):
    return str(env.connection.execute(
        "SELECT job_id FROM wf_jobs WHERE harness_id = %s AND trigger = 'ask' ORDER BY job_id LIMIT 1", (identity.harness_id,)
    ).fetchone()[0])
