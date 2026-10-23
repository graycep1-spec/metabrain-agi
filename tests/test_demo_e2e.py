"""T-28 [D] локально: сценарий демо, шаги 1–7 (`specs/50-tasks.md`) — от регистрации в Telegram до навыка у двойника B.

Всё настоящее, кроме внешних сервисов: Telegram-адаптер ходит в API харнесса через HTTP-клиент бота, кабинет — через
API харнесса, вход — Telegram OIDC (фейковый провайдер), DeepSeek — фейковый HTTP-провайдер, отвечающий по фактам из
промпта. Валидатор, мета-мозг, хранилище секретов, коннектор с DLP и бюджетом, runtime по расписанию — настоящие.
GT-dl-01..05 в объёме, который можно проверить без настоящей модели.
"""

import asyncio
import json
import os
import re
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi.testclient import TestClient
import httpx

from src.common.config import load_settings
from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.telegram import HTTPC1API, TelegramAdapter
from src.harness.wiring import build_connector
from src.metabrain.policy import load_policy
from src.web.app import create_app as create_web, http_harness
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime
from src.workforce.skills import SkillFlow
from src.workforce.swarm import SwarmHandler, visible_skills
from tests.support_harness import (
    CONSENT_KEY,
    CONSENT_PUBLIC_KEY,
    LANDSCAPE_TOKEN_A,
    LANDSCAPE_TOKEN_B,
    harness_env,
)
from tests.test_skills import brain_for
from tests.test_web_auth import API_TOKEN, FakeTelegram, claims, make_auth, telegram_id


A, B = telegram_id("a"), telegram_id("b")
PII = ("ООО Ромашка", "ИП Петров", "sokolova@romashka.example", "Ирина Соколова")
FORM = {"content-type": "application/x-www-form-urlencoded"}
SKILL = {
    "title": "Аналог при нехватке позиции",
    "body": "Если позиции не хватает на складе, предложи аналог из analogs[], который покрывает количество, "
            "успевает к дедлайну по lead_time_days и даёт маржу по list_price не ниже min_margin.",
    "task_class": "quote", "role_tags": ["sales"], "examples": ["Не хватает позиции — аналог той же категории"],
}


class DemoDeepSeek:
    """Аналитик разбирает заявку по фактам из промпта; критик всё подтверждает; разработчик обобщает навык."""

    def __init__(self):
        self.bodies: list[str] = []
        self.analyst_prompts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        raw = request.content.decode("utf-8")
        self.bodies.append(raw)
        body = json.loads(raw)
        text = " ".join(m.get("content", "") for m in body["messages"])
        if "разработчик цифровых сотрудников" in text:
            answer = SKILL
        elif '"unsupported"' in text:
            answer = {"unsupported": [], "notes": "ok"}
        else:
            self.analyst_prompts.append(text)
            answer = self._analyst(text)
        return httpx.Response(200, json={
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 1500, "completion_tokens": 300, "total_tokens": 1800},
        })

    @staticmethod
    def _analyst(text: str) -> dict:
        skills = re.findall(r"\[(skill:[^\]]+)\]", text)
        if "[crm:order/1042]" in text:
            order, short, analog = "crm:order/1042", "stock:item/A-17", "A-17M"
            claims_ = [{"text": "В заявке 1042 нужно 10 шт. A-17, на складе 6.", "sources": [order, short]}]
        elif "[crm:order/1043]" in text:
            order, short, analog = "crm:order/1043", "stock:item/D-09", "D-09S"
            claims_ = [{"text": "В заявке 1043 нужно 8 шт. D-09, на складе 3.", "sources": [order, short]}]
        else:
            return {"claims": [], "recommendation": None}
        claims_.append({"text": f"Аналог {analog} есть на складе в нужном количестве.", "sources": [f"stock:item/{analog}"]})
        recommendation = {"text": f"Черновик КП: заменить позицию аналогом {analog}, маржа по list_price не ниже min_margin.",
                          "sources": skills[:1] + [order, f"stock:item/{analog}", f"pricing:price/{analog}"]}
        return {"claims": claims_, "recommendation": recommendation}


class Demo:
    def __init__(self, env):
        self.env = env
        self.provider = DemoDeepSeek()
        self.telegram_sent: list[tuple[str, str]] = []
        self.oidc = FakeTelegram()
        self.brain = brain_for(env)
        settings = load_settings({"SERVICE_NAME": "harness", "LLM_PROVIDER": "deepseek",
                                  "DEEPSEEK_API_BASE": "https://api.deepseek.test"})
        secrets = EnvSecretStore(
            {
                "DEEPSEEK_API_KEY": "sk-test-not-real",
                "CONSENT_SIGNING_KEY": CONSENT_KEY,
                "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
            }
        )
        connector = build_connector(load_policy(), settings, env.connection, secrets,
                                    http_client=httpx.Client(transport=httpx.MockTransport(self.provider.handler)))
        swarm = SwarmHandler(env.connection, connector=connector, validator=env.validator,
                             collect=env.researcher.collect_facts, proposals=env.proposals, journal=env.journal,
                             skills=lambda **scope: visible_skills(self.brain, **scope))
        skills = SkillFlow(env.connection, connector=connector, metabrain=self.brain, proposals=env.proposals,
                           journal=env.journal, clock=env.clock)
        self.ask_runtime = Runtime(env.connection, handler=swarm, sink=HarnessReportSink(env.connection), clock=env.clock)
        self.scheduler = Runtime(env.connection, handler=swarm, clock=env.clock,
                                 sink=HarnessReportSink(env.connection, send=lambda chat, text: self.telegram_sent.append((chat, text))))
        self.harness_app = create_harness(
            env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test", secret_store=secrets,
            web_auth=make_auth(env, self.oidc), workforce=env.workforce, researcher=env.researcher,
            ask_runtime=self.ask_runtime, skills=skills, vault=env.vault,
        )
        self.harness = TestClient(self.harness_app)
        self.bot = TelegramAdapter(HTTPC1API("http://harness", api_token=API_TOKEN, client=httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.harness_app), base_url="http://harness")))

    # --- Telegram ---------------------------------------------------------------------------

    def say(self, user: int, text: str):
        return asyncio.run(self.bot.handle_message(telegram_user_id=user, text=text))

    def press(self, user: int, callback_data: str):
        return asyncio.run(self.bot.handle_callback(telegram_user_id=user, callback_data=callback_data))

    # --- кабинет ----------------------------------------------------------------------------

    def cabinet(self, user: int) -> TestClient:
        web = TestClient(create_web(http_harness("http://harness", API_TOKEN, client=self.harness),
                                    web_base_url="https://cabinet.example.test"), base_url="https://cabinet.example.test")
        start = web.get("/login/start", follow_redirects=False)
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        code = f"code-{user}"
        self.oidc.issue(code, query["code_challenge"], claims(user, query["nonce"]))
        assert web.get(f"/auth/callback?code={code}&state={query['state']}").status_code == 200
        return web

    @staticmethod
    def post(web: TestClient, url: str, **fields):
        token = web.cookies.get("mb_session").split(".", 1)[1]
        return web.post(url, content=urlencode({"csrf_token": token, **fields}), headers=FORM, follow_redirects=False)

    def api(self, user: int, op: str, **payload):
        response = self.harness.post(f"/v1/operations/{op}", json={"channel_user_id": str(user), **payload},
                                     headers={"Authorization": f"Bearer {API_TOKEN}"})
        return response.json()

    def pending(self, user: int, kind: str) -> list[dict]:
        return self.api(user, "proposal.list", kind=kind, status="pending")["proposals"]

    def approve_in_cabinet(self, web: TestClient, user: int, kind: str) -> list[str]:
        decided = []
        for proposal in self.pending(user, kind):
            response = self.post(web, f"/proposals/{proposal['proposal_id']}/decide", decision="approve")
            assert response.status_code == 303 and response.headers["location"] == "/proposals?notice=approved", response.text
            decided.append(proposal["proposal_id"])
        return decided

    def connect_in_cabinet(self, web: TestClient, user: int, system: str, token: str) -> None:
        """`/connect` в боте → ссылка в кабинет → доступ в форме → подключение и адаптер подтверждены."""
        reply = self.say(user, f"/connect {system}")
        link = re.search(r"https://cabinet\.example\.test(/connections\?\S+?)\.?\s", reply.text + " ").group(1)
        query = {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()}
        assert f'value="{system}" selected' in web.get(link).text
        response = self.post(web, "/connections", system=system, secret=token, connect_request_id=query["connect_request_id"])
        assert response.headers["location"] == "/proposals?notice=connect"
        assert self.approve_in_cabinet(web, user, "connect")
        assert self.approve_in_cabinet(web, user, "adapter")


def test_t28_demo_scenario_steps_1_to_7():
    with harness_env(vault_key=os.urandom(32)) as env:
        demo = Demo(env)

        # 1. A регистрируется в Telegram и создаёт компанию; B входит по инвайту A.
        reply = demo.say(A, "/start")
        registered = demo.press(A, reply.buttons[0].callback_data)
        invite = re.search(r"start=(\S+)", registered.text).group(1)
        demo.press(B, demo.say(B, f"/start {invite}").buttons[0].callback_data)
        a = env.harness.identity_for_channel_user(channel="telegram", channel_user_id=str(A))
        b = env.harness.identity_for_channel_user(channel="telegram", channel_user_id=str(B))
        assert a.company_id == b.company_id and a.harness_id != b.harness_id

        # 2. /role → кабинет: черновик паспорта, правка расписания → подтверждение в кабинете.
        assert "Черновик роли создан" in demo.say(A, "/role Я менеджер по продажам, готовлю КП по новым заявкам").text
        web_a = demo.cabinet(A)
        twin_a = next(e for e in demo.api(A, "employee.list")["employees"] if e["kind"] == "twin")
        assert "Изменить черновик" in web_a.get(f"/employees/{twin_a['employee_id']}").text
        form = {**demo.api(A, "employee.get", employee_id=twin_a["employee_id"])["passport"]}
        edited = demo.post(web_a, f"/employees/{twin_a['employee_id']}/edit", title=form["role"]["title"],
                           mission=form["role"]["mission"], duties="\n".join(d["text"] for d in form["duties"]),
                           goals="КП за 2 часа | quote_hours | 2 | day", may="предлагать аналоги",
                           must_not="скидка ниже min_margin", escalation="нет позиции и аналога",
                           schedule="0 12 * * 1-5 | 1")
        assert edited.status_code == 303
        assert len(demo.approve_in_cabinet(web_a, A, "passport")) == 1          # одно предложение — правленый черновик
        twin = demo.api(A, "employee.get", employee_id=twin_a["employee_id"])
        assert twin["status"] == "active" and twin["passport"]["triggers"]["schedule"][0]["cron"] == "0 12 * * 1-5"

        # 3. Подключение demo-landscape (read-only, согласие) → адаптеры через валидатор → карта crm, stock, pricing.
        for system in ("crm", "stock", "pricing"):
            demo.connect_in_cabinet(web_a, A, system, LANDSCAPE_TOKEN_A)
        assert len(demo.approve_in_cabinet(web_a, A, "passport")) == 1          # все три системы — одной версией
        landscape = demo.api(A, "landscape.map")
        assert {s["system"]: s["status"] for s in landscape["systems"]} == {"crm": "active", "stock": "active", "pricing": "active"}
        links = {(o["name"], link["to"]) for item in landscape["map"].values() for o in item["objects"] for link in o.get("links", [])}
        assert ("order", "stock.item.sku") in links and ("price", "stock.item.sku") in links           # GT-dl-01
        assert "Карта ландшафта" in web_a.get("/connections").text

        # 4. Обход по расписанию: двойник A находит заявку 1042 и присылает черновик КП с аналогом и источниками.
        tick = demo.scheduler.tick()
        assert (tick.enqueued, tick.finished, tick.failed) == (1, 1, 0)
        chat, report = demo.telegram_sent[-1]
        assert chat == str(A) and "A-17M" in report and "(источник:" in report
        entry = demo.api(A, "journal.list", kind="report")["entries"][0]
        assert {s["system"] for s in entry["sources"]} == {"crm", "stock", "pricing"}                   # GT-dl-02
        prompt_a = demo.provider.analyst_prompts[-1]
        assert "[pricing:price/A-17M]" in prompt_a and "[stock:item/A-17M]" in prompt_a
        a17m = next(b for b in demo.provider.bodies if "{{fact:" not in b and "420" in b and "340" in b)  # цены дошли
        assert a17m and not any(p in body for body in demo.provider.bodies for p in PII)                 # GT-dl-03

        # 5. A подтверждает рекомендацию → навык → профиль Skill → согласие на публикацию в компанию.
        assert len(demo.approve_in_cabinet(web_a, A, "recommendation")) == 1
        assert len(demo.approve_in_cabinet(web_a, A, "skill_publish")) == 1
        company = demo.api(A, "skills.list", scope="company")["skills"]
        assert len(company) == 1 and company[0]["consent"]
        published = json.dumps(company[0], ensure_ascii=False)
        assert "Ромашка" not in published and "1042" not in published                                   # GT-dl-04
        assert "Аналог при нехватке позиции" in web_a.get("/skills").text

        # 6. Двойник B разбирает заявку 1043 с навыком компании: skill_id@version в трейсе и в sources[].
        role_b = demo.say(B, "/role Я менеджер по продажам, готовлю КП по новым заявкам")
        assert "подтверждено" in demo.press(B, role_b.buttons[0].callback_data).text                    # кнопка в Telegram
        web_b = demo.cabinet(B)
        demo.connect_in_cabinet(web_b, B, "crm", LANDSCAPE_TOKEN_B)
        demo.connect_in_cabinet(web_b, B, "stock", LANDSCAPE_TOKEN_B)
        demo.connect_in_cabinet(web_b, B, "pricing", LANDSCAPE_TOKEN_B)
        assert len(demo.approve_in_cabinet(web_b, B, "passport")) == 1
        env.clock.advance(minutes=15)                                    # слот 12:15: у B */15, у A — только 12:00
        tick = demo.scheduler.tick()
        assert (tick.enqueued, tick.finished, tick.failed) == (1, 1, 0)
        chat, report = demo.telegram_sent[-1]
        ref = f"{company[0]['skill_id']}@{company[0]['version']}"
        assert chat == str(B) and "навык «Аналог при нехватке позиции»" in report and "D-09S" in report
        entry_b = demo.api(B, "journal.list", kind="report")["entries"][0]
        assert any(s.get("skill_ref") == ref for s in entry_b["sources"])                               # GT-dl-05
        assert not any(s["object_ref"] == "order/1042" for s in entry_b["sources"])
        trace = env.connection.execute(
            """
            SELECT s.result FROM wf_job_steps s JOIN wf_jobs j ON j.job_id = s.job_id
            WHERE j.harness_id = %s AND s.name = 'skills'
            """, (b.harness_id,),
        ).fetchone()[0]
        assert [f"{s['skill_id']}@{s['version']}" for s in trace] == [ref]
        assert demo.pending(B, "recommendation") and not demo.pending(B, "action")      # вопроса куратору про аналог нет

        # 7. Журнал кабинета A: паспорт → подключение → отчёт с источниками → решение → публикация навыка.
        for kind, label in (("employee.passport.activated", "Паспорт действует"), ("landscape.connected", "Система подключена"),
                            ("adapter.activated", "Адаптер включён"), ("report", "Отчёт сотрудника"),
                            ("proposal.decision", "Решение куратора"), ("skill.published", "Навык опубликован")):
            page = web_a.get(f"/journal?kind={kind}")
            assert page.status_code == 200 and page.text.count(f"<td>{label}</td>") >= 1, kind
        assert "Источники: crm order/1042" in web_a.get("/journal?kind=report").text
        assert LANDSCAPE_TOKEN_A not in web_a.get("/journal").text
