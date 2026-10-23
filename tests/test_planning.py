"""C-6: процессы, проекты, назначения сотрудников и задачи (планирование)."""

from datetime import date, timedelta

import pytest

from src.workforce.planning import PlanningConflict, PlanningInvalid, PlanningNotFound, PlanningService
from tests.support_harness import harness_env
from tests.test_workforce import active_twin


def service(env) -> PlanningService:
    return PlanningService(env.connection, journal=env.journal)


def test_process_project_assign_task_flow():
    with harness_env() as env:
        me = env.register()
        twin = active_twin(env, me)
        plan = service(env)

        process = plan.create_process(
            me, name="Разбор почты", purpose="Извлекать суть и поручения",
            inputs=["письма"], outputs=["суть", "поручения"],
        )
        project = plan.create_project(
            me, name="Пилот", goal="Сократить время КП",
            starts_on=date.today(), ends_on=date.today() + timedelta(days=30),
            budget_usd=500, status="active",
        )
        assert project["name"] == "Пилот"
        assignment = plan.assign(
            me, employee_id=twin["employee_id"], target_type="process",
            target_id=process["process_id"], role="исполнитель",
        )
        task = plan.create_task(
            me, assignment_id=assignment["assignment_id"], title="Разобрать 10 писем",
        )

        loaded = plan.get_process(me, process["process_id"])
        assert loaded["name"] == "Разбор почты"
        assert loaded["assignments"][0]["employee_title"] == "Менеджер по продажам"
        assert loaded["assignments"][0]["tasks"][0]["title"] == task["title"]

        assert plan.summary(me) == {"processes": 1, "projects": 1, "assignments": 1, "tasks": 1}
        updated = plan.update_task(me, task_id=task["task_id"], status="doing")
        assert updated["status"] == "doing"
        assert plan.list_tasks(me, assignment_id=assignment["assignment_id"])[0]["status"] == "doing"

        with pytest.raises(PlanningConflict):
            plan.assign(me, employee_id=twin["employee_id"], target_type="process",
                        target_id=process["process_id"])
        with pytest.raises(PlanningInvalid):
            plan.update_task(me, task_id=task["task_id"], status="unknown")


def test_planning_is_isolated_by_harness_and_company():
    with harness_env() as env:
        me = env.register("a")
        other = env.register("b")
        mine = active_twin(env, me)
        theirs = active_twin(env, other)
        plan = service(env)
        process = plan.create_process(me, name="Мой процесс")

        assert plan.list_processes(other) == []
        with pytest.raises(PlanningNotFound):
            plan.get_process(other, process["process_id"])
        with pytest.raises(PlanningNotFound):
            plan.assign(me, employee_id=theirs["employee_id"], target_type="process",
                        target_id=process["process_id"])

        their_process = plan.create_process(other, name="Их процесс")
        assert {p["name"] for p in plan.list_processes(other)} == {"Их процесс"}
        with pytest.raises(PlanningNotFound):
            plan.assign(other, employee_id=mine["employee_id"], target_type="process",
                        target_id=their_process["process_id"])


def test_seed_examples_is_idempotent():
    with harness_env() as env:
        me = env.register()
        active_twin(env, me)
        plan = service(env)
        first = plan.seed_examples(me)
        assert first["processes"] == 2 and first["projects"] == 1
        assert first["assignments"] == 3 and first["tasks"] == 3
        second = plan.seed_examples(me)
        assert second == first
        assert len(plan.list_processes(me)) == 2
        assert len(plan.list_projects(me)) == 1
