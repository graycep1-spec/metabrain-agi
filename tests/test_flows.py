"""ADR-036: преднастроенные сценарии — роль + процессы + прикрепления + задачи одной кнопкой."""

import pytest

from src.workforce.catalog import FLOW_TEMPLATES, PROCESS_TEMPLATES, ROLE_TEMPLATES, listing
from src.workforce.flows import FlowService
from src.workforce.planning import PlanningService
from tests.support_harness import harness_env
from tests.test_web_cabinet import csrf, logged_in, post


def flows_for(env) -> FlowService:
    return FlowService(env.connection, workforce=env.workforce,
                       planning=PlanningService(env.connection, journal=env.journal, clock=env.clock),
                       journal=env.journal, clock=env.clock)


def test_every_flow_references_existing_role_and_processes():
    roles = {t["id"] for t in ROLE_TEMPLATES}
    processes = {t["id"] for t in PROCESS_TEMPLATES}
    assert len(FLOW_TEMPLATES) >= 3
    for flow in FLOW_TEMPLATES:
        assert flow["role"] in roles, flow["id"]
        assert flow["steps"], flow["id"]
        for step in flow["steps"]:
            assert step["process"] in processes, (flow["id"], step["process"])
            assert step["tasks"], (flow["id"], step["process"])
    shown = listing()["flows"]
    assert len(shown) == len(FLOW_TEMPLATES)
    assert all(f["role_title"] and f["processes"] and f["tasks"] for f in shown)


def test_start_builds_the_whole_chain_and_gates_the_passport():
    with harness_env() as env:
        me = env.register("a")
        flows = flows_for(env)

        result = flows.start(me, template_id="sales-quotes")

        assert result["status"] == "started" and result["proposal_id"]
        assert len(result["processes"]) == 2 and result["assignments"] == 2 and result["tasks"] == 3
        # сотрудник создан, но паспорт ждёт гейта — работа не началась
        employee = env.connection.execute(
            "SELECT status FROM wf_employees WHERE employee_id = %s", (result["employee_id"],)).fetchone()[0]
        assert employee != "active"
        assert env.connection.execute(
            "SELECT count(*) FROM proposals WHERE kind = 'passport' AND status = 'pending'").fetchone()[0] == 1
        assert env.connection.execute("SELECT count(*) FROM wf_assignments").fetchone()[0] == 2
        assert env.connection.execute("SELECT count(*) FROM wf_tasks").fetchone()[0] == 3
        assert env.connection.execute(
            "SELECT count(*) FROM journal_entries WHERE kind = 'flow.started'").fetchone()[0] == 1


def test_starting_twice_reuses_processes_instead_of_duplicating_them():
    with harness_env() as env:
        me = env.register("a")
        flows = flows_for(env)

        flows.start(me, template_id="sales-quotes")
        before = env.connection.execute("SELECT count(*) FROM wf_processes").fetchone()[0]
        second = flows.start(me, template_id="sales-quotes")

        assert second["status"] == "started"
        assert env.connection.execute("SELECT count(*) FROM wf_processes").fetchone()[0] == before
        assert len(flows.status(me)) == 2


def test_unknown_flow_is_rejected():
    with harness_env() as env:
        me = env.register("a")
        flows = flows_for(env)
        with pytest.raises(ValueError):
            flows.start(me, template_id="nope")


def test_cabinet_shows_flows_and_deploys_one():
    with harness_env() as env:
        env.register("a")
        web, _ = logged_in(env)

        page = web.get("/flows")
        assert page.status_code == 200
        assert "Продажи: от заявки до КП" in page.text and "Развернуть сценарий" in page.text

        started = post(web, "/flows/start", {"csrf_token": csrf(web), "template_id": "support-line"})
        assert started.status_code == 303 and started.headers["location"] == "/proposals?notice=flow_started"
        assert "ждёт вашего согласия" in web.get("/proposals?notice=flow_started").text

        again = web.get("/flows")
        assert "Развёрнутые сценарии" in again.text and "ждёт согласия" in again.text
