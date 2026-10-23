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
