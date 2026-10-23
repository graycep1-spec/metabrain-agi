"""Каталог преднастроенных ролей/процессов, конвейер и диагност (минимум по спекам, ADR-029)."""

from urllib.parse import urlencode

import pytest

from src.workforce.catalog import PROCESS_TEMPLATES, ROLE_TEMPLATES, listing
from tests.support_harness import harness_env
from tests.test_swarm import API_TOKEN, USER, FakeDeepSeek, api_for, connected_twin
from tests.test_web_cabinet import FORM, csrf, logged_in, post


# --- каталог: каждая роль обязана проходить валидатор ----------------------------------------

@pytest.mark.parametrize("template_id", [t["id"] for t in ROLE_TEMPLATES])
def test_each_role_template_passes_the_validator(template_id):
    with harness_env() as env:
        me = env.register("a")
        result = env.workforce.draft_from_template(me, template_id=template_id)
        assert result.get("proposal_id"), f"template {template_id} rejected: {result.get('reasons')}"
        assert result["passport_draft"]["provenance"]["source"] == "template"


def test_unknown_role_template_is_a_value_error():
    with harness_env() as env:
        me = env.register("a")
        with pytest.raises(ValueError):
            env.workforce.draft_from_template(me, template_id="no-such-role")


def test_catalog_listing_is_a_showcase_without_drafts():
    data = listing()
    assert len(data["roles"]) == len(ROLE_TEMPLATES) >= 6
    assert len(data["processes"]) == len(PROCESS_TEMPLATES) >= 6
    assert len({t["id"] for t in ROLE_TEMPLATES}) == len(ROLE_TEMPLATES)
    assert len({t["id"] for t in PROCESS_TEMPLATES}) == len(PROCESS_TEMPLATES)
    assert all("draft" not in r for r in data["roles"])


# --- кабинет: создание из каталога и конвейер ------------------------------------------------

def test_cabinet_creates_role_and_process_from_catalog_and_shows_pipeline():
    with harness_env() as env:
        env.register("a")
        web, _ = logged_in(env)

        home = web.get("/")
        assert "Конвейер найма" in home.text and "Создать из каталога" in home.text
        assert "Специалист поддержки" in home.text          # каталог на экране

        created = post(web, "/roles/from-template", {"csrf_token": csrf(web), "template_id": "support"})
        assert created.status_code == 303 and created.headers["location"] == "/proposals?notice=proposed"
        assert "Специалист поддержки" in web.get("/proposals").text

        # конвейер показывает ожидающий гейт
        assert "Гейт куратора · 1" in web.get("/").text

        processes = web.get("/processes")
        assert "Создать из каталога" in processes.text and "Обработка рекламации" in processes.text
        made = post(web, "/processes/from-template", {"csrf_token": csrf(web), "template_id": "complaint"})
        assert made.status_code == 303 and made.headers["location"] == "/processes?notice=process_created"
        names = [r[0] for r in env.connection.execute("SELECT name FROM wf_processes").fetchall()]
        assert "Обработка рекламации" in names


# --- диагност: разбор журнала роем, рекомендация — предложением (ADR-029) --------------------

def test_diagnose_runs_the_swarm_over_journal_and_proposes_a_fix():
    with harness_env() as env:
        me = env.register("a")
        connected_twin(env, me)                             # активный двойник с доступом к crm
        client = api_for(env, FakeDeepSeek())
        employee_id = env.connection.execute(
            "SELECT employee_id FROM wf_employees WHERE kind <> 'researcher' ORDER BY created_at LIMIT 1"
        ).fetchone()[0]

        response = client.post(
            "/v1/operations/employee.diagnose",
            json={"channel_user_id": USER, "employee_id": str(employee_id),
                  "question": "почему не готовит КП по заявкам?"},
            headers={"Authorization": f"Bearer {API_TOKEN}"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["diagnosis"] is True and body["answer"]
        assert body["proposal_ids"], "рекомендация должна уйти предложением куратору"
        # запрос помощи записан в журнал
        assert env.connection.execute(
            "SELECT count(*) FROM journal_entries WHERE kind = 'help.requested'"
        ).fetchone()[0] == 1


# --- профиль организации (ADR-035): наследование и отраслевой каталог ------------------------

def test_org_profile_saves_and_validates_in_cabinet():
    with harness_env() as env:
        env.register("a")
        web, _ = logged_in(env)
        pg = web.get("/organization")
        assert pg.status_code == 200 and "Профиль организации" in pg.text
        saved = post(web, "/organization", {"csrf_token": csrf(web), "industry": "manufacturing",
                                            "size": "small", "kpi": "выручка +20% за год"})
        assert saved.status_code == 303 and saved.headers["location"] == "/organization?notice=org_saved"
        row = env.connection.execute("SELECT industry, size, kpi FROM org_settings").fetchone()
        assert row == ("manufacturing", "small", "выручка +20% за год")
        bad = post(web, "/organization", {"csrf_token": csrf(web), "industry": "space", "size": "", "kpi": ""})
        assert bad.status_code == 400 and "Проверьте поля" in bad.text


def test_role_from_catalog_inherits_org_kpi_as_a_goal():
    with harness_env() as env:
        me = env.register("a")
        result = env.workforce.draft_from_template(
            me, template_id="support", org={"kpi": "срок ответа клиенту — до 1 часа"})
        assert result.get("proposal_id")
        goals = result["passport_draft"]["goals"]
        assert any(g["metric"] == "org_kpi" and "до 1 часа" in g["text"] for g in goals)


def test_free_text_role_gets_org_context_for_the_model():
    from tests.support_harness import FakeDrafter
    drafter = FakeDrafter()
    with harness_env(drafter=drafter) as env:
        env.register("a")
        web, _ = logged_in(env)
        post(web, "/organization", {"csrf_token": csrf(web), "industry": "logistics",
                                    "size": "micro", "kpi": "доставка в срок 98%"})
        post(web, "/roles", {"csrf_token": csrf(web), "role_text": "диспетчер доставки"})
        role_text = drafter.calls[-1][0]
        assert "Контекст организации" in role_text and "доставка в срок 98%" in role_text
        assert "Логистика" in role_text


def test_catalog_covers_every_industry_with_sme_processes():
    from src.harness.organization import INDUSTRIES
    for key in INDUSTRIES:
        if not key:
            continue
        count = sum(1 for t in PROCESS_TEMPLATES if t.get("industry") == key)
        assert count >= 2, f"industry {key} has only {count} processes"
    assert sum(1 for t in PROCESS_TEMPLATES if t.get("industry") == "all") >= 5
    assert all(t.get("industry") in ({"all"} | set(INDUSTRIES)) for t in PROCESS_TEMPLATES)


# --- авто-вызов диагноста при падении работы (ADR-034) ---------------------------------------

class _Failing:
    def run(self, job, passport, steps):
        raise RuntimeError("boom: adapter timeout")


def test_failed_job_calls_the_diagnost_once_per_window():
    from src.workforce.reports import HarnessReportSink
    from src.workforce.runtime import Runtime
    from src.workforce.store import WorkforceStore
    from tests.test_workforce import active_twin

    with harness_env() as env:
        me = env.register("a")
        active_twin(env, me)
        store = WorkforceStore(env.connection)
        employee_id = env.connection.execute(
            "SELECT employee_id FROM wf_employees WHERE kind <> 'researcher' LIMIT 1").fetchone()[0]
        employee = store.get_employee(employee_id=employee_id, harness_id=me.harness_id)
        runtime = Runtime(env.connection, handler=_Failing(), sink=HarnessReportSink(env.connection),
                          clock=env.clock)

        job_id = runtime.enqueue(employee, duty_id="d1", trigger="event", input={"text": "работа"})
        assert runtime.run_job(job_id) == "failed"

        help_count = lambda: env.connection.execute(
            "SELECT count(*) FROM journal_entries WHERE kind='help.requested' AND payload->>'auto'='true'"
        ).fetchone()[0]
        diag_jobs = lambda: env.connection.execute(
            "SELECT count(*) FROM wf_jobs WHERE input->>'diagnosis' = 'true'").fetchone()[0]
        assert help_count() == 1 and diag_jobs() == 1

        # повторное падение в окне — без второго вызова (троттл)
        job2 = runtime.enqueue(employee, duty_id="d1", trigger="event", input={"text": "ещё"})
        assert runtime.run_job(job2) == "failed"
        assert help_count() == 1 and diag_jobs() == 1

        # упавшая диагностика себя не диагностирует (нет зацикливания)
        diag_job = env.connection.execute(
            "SELECT job_id FROM wf_jobs WHERE input->>'diagnosis' = 'true' LIMIT 1").fetchone()[0]
        assert runtime.run_job(diag_job) == "failed"
        assert help_count() == 1 and diag_jobs() == 1
