"""T-34: пауза ЦС (C-1 `employee.pause`/`employee.resume`) — новые работы не стартуют, начатая доделывается."""

from urllib.parse import urlencode
from uuid import UUID

import pytest

from src.workforce.service import EmployeeNotFound, PassportConflict
from tests.support_harness import harness_env
from tests.test_web_cabinet import csrf, logged_in
from tests.test_workforce import Handler, active_twin, runtime


FORM = {"content-type": "application/x-www-form-urlencoded"}


def twin(env, me):
    return UUID(active_twin(env, me)["employee_id"])


def test_paused_employee_gets_no_new_jobs_and_resumes_with_a_fresh_slot():
    with harness_env() as env:
        me = env.register()
        employee_id = twin(env, me)
        rt = runtime(env, Handler())
        assert rt.schedule() == 1                                   # работа в очереди до паузы

        assert env.workforce.set_paused(me, employee_id=employee_id, paused=True)["status"] == "paused"
        env.connection.commit()
        assert rt.run_one() == "failed"                             # не стартует: снята как «сотрудник не активен» (T-15)
        env.clock.advance(minutes=15)
        assert rt.schedule() == 0                                   # и новых слотов нет
        with pytest.raises(EmployeeNotFound):
            env.workforce.employee_for_ask(me)                      # ask к сотруднику на паузе — 404

        env.workforce.set_paused(me, employee_id=employee_id, paused=False)
        env.connection.commit()
        assert rt.schedule() == 1                                   # после возобновления — свежий слот, не устаревший
        assert rt.run_one() == "done"


def test_new_passport_version_does_not_lift_the_pause_and_pause_is_idempotent():
    with harness_env() as env:
        me = env.register()
        employee_id = twin(env, me)
        env.workforce.set_paused(me, employee_id=employee_id, paused=True)
        assert env.workforce.set_paused(me, employee_id=employee_id, paused=True)["status"] == "paused"

        edited = env.workforce.propose_edit(me, employee_id=employee_id, patch={"duties": [{"text": "Разбирать заявки"}]})
        env.workforce.on_passport_approved(me, proposal_id=UUID(edited["proposal_id"]),
                                           token=env.approve(me, UUID(edited["proposal_id"])))
        got = env.workforce.get(me, employee_id=employee_id)
        assert got["status"] == "paused" and got["active_version"] == 2

        kinds = [row[0] for row in env.connection.execute(
            "SELECT kind FROM journal_entries WHERE harness_id = %s AND kind LIKE 'employee.%%' ORDER BY created_at",
            (me.harness_id,),
        ).fetchall()]
        assert kinds.count("employee.paused") == 1


def test_researcher_and_draft_cannot_be_paused():
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        employees = {e["kind"]: e for e in env.workforce.list(me)}
        with pytest.raises(ValueError):
            env.workforce.set_paused(me, employee_id=UUID(employees["researcher"]["employee_id"]), paused=True)
        with pytest.raises(PassportConflict):
            env.workforce.set_paused(me, employee_id=UUID(drafted["employee_id"]), paused=True)


def test_cabinet_pauses_and_resumes_from_the_employee_card():
    with harness_env() as env:
        me = env.register()
        employee_id = twin(env, me)
        web, _ = logged_in(env)

        card = web.get(f"/employees/{employee_id}")
        assert "Поставить на паузу" in card.text
        assert web.post(f"/employees/{employee_id}/pause", content=urlencode({}), headers=FORM,
                        follow_redirects=False).status_code == 403                    # без CSRF
        paused = web.post(f"/employees/{employee_id}/pause", content=urlencode({"csrf_token": csrf(web)}), headers=FORM,
                          follow_redirects=False)
        assert paused.status_code == 303 and paused.headers["location"] == f"/employees/{employee_id}?notice=paused"
        card = web.get(paused.headers["location"])
        assert "На паузе" in card.text and "Возобновить" in card.text and "новые работы не стартуют" in card.text

        resumed = web.post(f"/employees/{employee_id}/resume", content=urlencode({"csrf_token": csrf(web)}), headers=FORM,
                           follow_redirects=False)
        assert resumed.headers["location"] == f"/employees/{employee_id}?notice=resumed"
        assert env.workforce.get(me, employee_id=employee_id)["status"] == "active"
