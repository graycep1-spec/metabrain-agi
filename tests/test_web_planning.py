"""C-6: экраны процессов, проектов, назначений и задач в кабинете."""

from urllib.parse import urlencode

from src.workforce.planning import PlanningService
from tests.support_harness import harness_env
from tests.test_web_cabinet import FORM, csrf, logged_in
from tests.test_workforce import active_twin


def post(web, url, fields):
    return web.post(url, content=urlencode({"csrf_token": csrf(web), **fields}),
                    headers=FORM, follow_redirects=False)


def test_cabinet_process_project_assign_and_tasks():
    with harness_env() as env:
        me = env.register()
        active_twin(env, me)
        web, _ = logged_in(env)

        home = web.get("/")
        assert home.status_code == 200
        assert "Планирование" in home.text and "Создать роль" in home.text

        created = post(web, "/processes", {
            "name": "Разбор почты", "purpose": "Извлекать суть",
            "inputs": "письма", "outputs": "суть\nпоручения",
        })
        assert created.status_code == 303

        plan = PlanningService(env.connection, journal=env.journal)
        process = plan.list_processes(me)[0]
        page = web.get(f"/processes/{process['process_id']}")
        assert page.status_code == 200 and "Разбор почты" in page.text
        assert "Прикрепить сотрудника" in page.text

        twin = next(e for e in env.workforce.list(me) if e["kind"] == "twin")
        assigned = post(web, f"/processes/{process['process_id']}/assign",
                        {"employee_id": twin["employee_id"], "role": "исполнитель"})
        assert assigned.status_code == 303
        assignment = plan.get_process(me, process["process_id"])["assignments"][0]
        assert assignment["employee_title"] == twin["title"]

        task = post(web, f"/processes/{process['process_id']}/tasks",
                    {"assignment_id": assignment["assignment_id"], "title": "Разобрать 10 писем"})
        assert task.status_code == 303
        page = web.get(f"/processes/{process['process_id']}")
        assert "Разобрать 10 писем" in page.text and "исполнитель" in page.text

        project = post(web, "/projects", {
            "name": "Пилот", "goal": "Сократить время КП",
            "starts_on": "2026-10-23", "ends_on": "2026-11-22",
            "budget_usd": "500", "status": "active",
        })
        assert project.status_code == 303
        projects = web.get("/projects")
        assert projects.status_code == 200 and "Пилот" in projects.text

        summary = plan.summary(me)
        assert summary == {"processes": 1, "projects": 1, "assignments": 1, "tasks": 1}
        assert "Процессы: 1" in web.get("/").text


def test_role_form_creates_a_passport_draft():
    with harness_env() as env:
        env.register()
        web, _ = logged_in(env)
        created = post(web, "/roles", {"role_text": "Я аналитик, проверяю данные"})
        assert created.status_code == 303 and created.headers["location"].startswith("/proposals")


def test_seed_examples_button_fills_planning():
    with harness_env() as env:
        me = env.register()
        active_twin(env, me)
        web, _ = logged_in(env)
        seeded = web.post("/planning/examples", content=urlencode({"csrf_token": csrf(web)}),
                          headers=FORM, follow_redirects=False)
        assert seeded.status_code == 303
        assert "Разбор входящей почты" in web.get("/processes").text
        assert "Пилот MetaBrain в продажах" in web.get("/projects").text
