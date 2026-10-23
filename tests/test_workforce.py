"""T-13 (паспорт роли) и T-15 (runtime работ): GT-c6-01..-04, -08, R-14, R-16."""

from datetime import datetime, timezone
import json

import psycopg
import pytest

from src.harness.proposals import ConsentError
from src.workforce.cron import Cron, CronError
from src.workforce.passport import DEFAULT_CRON, DraftError, LLMPassportDrafter, parse_draft
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime
from src.workforce.store import WorkforceStore
from tests.support_harness import FakeDrafter, harness_env, journal_kinds


# --- черновик паспорта (без БД) --------------------------------------------------------

def test_parse_draft_normalizes_and_turns_gaps_into_questions_to_the_curator():
    content = "```json\n" + json.dumps({
        "role": {"title": "Менеджер", "mission": "КП по заявкам"},
        "duties": [{"text": "Разбирать заявки"}, "Сверять наличие"],
        "goals": [{"text": "КП быстро"}],
        "schedule": [{"cron": "каждые 15 минут", "duty": 1}, {"cron": "0 9 * * 1-5", "duty": 2}],
    }, ensure_ascii=False) + "\n```"
    draft = parse_draft(content)

    assert [d["duty_id"] for d in draft["duties"]] == ["d1", "d2"]
    assert draft["schedule"] == [{"cron": "0 9 * * 1-5", "duty_id": "d2"}]
    questions = " ".join(e["condition"] for e in draft["escalation"])
    assert "метрику" in questions and "период" in questions and "расписание" in questions
    assert all(e["to"] == "curator" for e in draft["escalation"])


def test_parse_draft_without_schedule_gets_the_default():
    draft = parse_draft(json.dumps({"role": {"title": "a", "mission": "b"}, "duties": [{"text": "c"}]}))
    assert draft["schedule"] == [{"cron": DEFAULT_CRON, "duty_id": "d1"}]


@pytest.mark.parametrize("content", ["нет json", "[]", '{"role": {"title": "a"}}', '{"role": {"title": "a", "mission": "b"}, "duties": []}'])
def test_parse_draft_rejects_answers_that_are_not_a_passport(content):
    with pytest.raises(DraftError):
        parse_draft(content)


def test_llm_drafter_uses_the_developer_route_and_parses_the_answer():
    class FakeConnector:
        def chat_completions(self, context, request):
            self.context, self.request = context, request
            answer = {"role": {"title": "Т", "mission": "М"}, "duties": [{"text": "Д"}]}

            class Result:
                response = {"choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}]}
            return Result()

    connector = FakeConnector()
    draft = LLMPassportDrafter(connector).draft("Я менеджер", context="ctx")
    assert draft["role"]["title"] == "Т"
    assert connector.request.messages[-1] == {"role": "user", "content": "Я менеджер"}


# --- cron (без БД) ---------------------------------------------------------------------------

def test_cron_matching_and_slots():
    every_15 = Cron("*/15 * * * *")
    at = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
    assert every_15.matches(at) and not every_15.matches(at.replace(minute=7))
    assert [s.minute for s in every_15.slots(at.replace(hour=11, minute=40), at)] == [45, 0]
    sunday = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
    assert Cron("0 9 * * 7").matches(sunday) and Cron("0 9 * * 0").matches(sunday)
    assert not Cron("0 9 * * 1-5").matches(sunday)


@pytest.mark.parametrize("expression", ["* * * *", "61 * * * *", "*/0 * * * *", "a b c d e"])
def test_cron_rejects_bad_expressions(expression):
    with pytest.raises(CronError):
        Cron(expression)


# --- T-13: паспорт на PostgreSQL --------------------------------------------------------

def active_twin(env, identity):
    drafted = env.workforce.draft(identity, kind="twin", role_text="Я менеджер по продажам")
    token = env.approve(identity, drafted["proposal_id"])
    env.workforce.on_passport_approved(identity, proposal_id=drafted["proposal_id"], token=token)
    env.connection.commit()
    return drafted


def test_gt_c6_01_role_text_to_active_twin_on_l0():
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер по продажам, готовлю КП")

        employees = {e["kind"]: e for e in env.workforce.list(me)}
        assert employees["twin"]["status"] == "draft"
        assert employees["researcher"]["status"] == "active"
        assert drafted["passport_draft"]["autonomy"] == {}
        assert drafted["passport_draft"]["tools"]["write_classes"] == []
        assert env.drafter.calls[0][1].sub_role == "developer"

        token = env.approve(me, drafted["proposal_id"])
        applied = env.workforce.on_passport_approved(me, proposal_id=drafted["proposal_id"], token=token)

        assert applied == {"employee_id": drafted["employee_id"], "version": 1, "status": "active"}
        twin = next(e for e in env.workforce.list(me) if e["kind"] == "twin")
        assert (twin["status"], twin["active_version"], twin["autonomy"]) == ("active", 1, "L0")
        assert journal_kinds(env, me)[-3:] == [
            "proposal.decision",
            "proposal.applied",
            "employee.passport.activated",
        ]


def test_passport_rejected_by_the_validator_creates_no_employee_and_no_proposal():
    class RejectingValidator:
        def validate(self, artifact, profile, context):
            return {"scorecard_id": "sc-1", "verdict": "fail",
                    "checks": [{"name": "rights", "verdict": "fail", "metrics": {"excess": {"connectors": ["erp"]}}}]}

        def keys(self, refresh=False):
            return {}

    with harness_env(validator=RejectingValidator()) as env:
        me = env.register()
        result = env.workforce.draft(me, kind="twin", role_text="Я менеджер")

        assert result["status"] == "rejected" and "rights" in result["reasons"][0]
        assert [e["kind"] for e in env.workforce.list(me)] == ["researcher"]
        assert env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id) == ()
        assert "employee.passport.rejected" in journal_kinds(env, me)


def test_consent_for_a_passport_is_single_use():
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        token = env.approve(me, drafted["proposal_id"])
        env.workforce.on_passport_approved(me, proposal_id=drafted["proposal_id"], token=token)
        with pytest.raises(ConsentError):
            env.workforce.on_passport_approved(me, proposal_id=drafted["proposal_id"], token=token)


def test_gt_c6_08_passport_versions_are_immutable():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            env.connection.execute(
                "UPDATE wf_passports SET passport = passport || '{\"autonomy\": {\"x\": \"L2\"}}' WHERE employee_id = %s",
                (drafted["employee_id"],),
            )


def test_other_harness_cannot_see_or_activate_my_employee():
    with harness_env() as env:
        me, other = env.register("a"), env.register("b")
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        assert [e["kind"] for e in env.workforce.list(other)] == ["researcher"]
        token = env.approve(me, drafted["proposal_id"])
        with pytest.raises(ConsentError):
            env.workforce.on_passport_approved(other, proposal_id=drafted["proposal_id"], token=token)


# --- T-15: runtime --------------------------------------------------------------------------

class Handler:
    def __init__(self, crash_after_first_step=False, fail=False):
        self.crash, self.fail = crash_after_first_step, fail
        self.calls = {"collect": 0, "write": 0}
        self.keys = []

    def run(self, job, passport, steps):
        if self.fail:
            raise RuntimeError("analyst failed")
        facts = steps.run("collect", lambda key: self._count("collect", key, {"orders": ["1042"]}))
        if self.crash:
            self.crash = False
            raise KeyboardInterrupt  # имитация падения процесса: не Exception
        steps.run("write", lambda key: self._count("write", key, {"quote": "КП 1042"}))
        return {"text": f"Разобраны заявки: {facts['orders']}", "sources": [{"source_id": "s1"}],
                "score_ref": "sc-report", "proposal_ids": []}

    def _count(self, name, key, value):
        self.calls[name] += 1
        self.keys.append((name, key))
        return value


class Sink:
    def __init__(self):
        self.reports = []

    def deliver(self, job, report):
        self.reports.append((job, report))


def runtime(env, handler, sink=None):
    return Runtime(env.connect_new_connection(), handler=handler, sink=sink or Sink(), clock=env.clock)


def test_gt_c6_02_employee_without_an_approved_passport_gets_no_jobs():
    with harness_env() as env:
        me = env.register()
        env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        env.connection.commit()
        assert runtime(env, Handler()).schedule() == 0


def test_without_a_duty_handler_the_scheduler_creates_nothing():
    with harness_env() as env:
        active_twin(env, env.register())
        assert runtime(env, None).schedule() == 0


def test_gt_c6_03_cron_starts_a_job_without_a_request_and_reports_to_the_curator():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        sent = []
        rt_conn = env.connect_new_connection()
        rt = Runtime(rt_conn, handler=Handler(), sink=HarnessReportSink(rt_conn, send=lambda chat, text: sent.append((chat, text))),
                     clock=env.clock)

        tick = rt.tick()

        assert (tick.enqueued, tick.finished, tick.failed) == (1, 1, 0)   # слот 12:00 из окна (11:45, 12:00]
        assert sent == [("10" + "a".encode().hex(), "Разобраны заявки: ['1042']")]
        row = env.connection.execute(
            "SELECT kind, employee_id, score_ref FROM journal_entries WHERE kind = 'report' AND harness_id = %s",
            (me.harness_id,),
        ).fetchone()
        assert row[0] == "report" and str(row[1]) == drafted["employee_id"] and row[2] == "sc-report"


def test_one_job_per_slot_and_per_duty():
    with harness_env() as env:
        active_twin(env, env.register())
        rt = runtime(env, Handler())
        assert rt.schedule() == 1
        assert rt.schedule() == 0                      # тот же слот
        env.clock.advance(minutes=15)
        assert rt.schedule() == 0                      # новый слот, но прошлая работа ещё в очереди
        assert rt.run_one() == "done"
        assert rt.schedule() == 1                      # обязанность свободна — слот 12:15


def test_gt_c6_04_restart_mid_job_resumes_without_repeating_finished_steps():
    with harness_env() as env:
        active_twin(env, env.register())
        handler, sink = Handler(crash_after_first_step=True), Sink()
        first = runtime(env, handler, sink)
        first.schedule()
        with pytest.raises(KeyboardInterrupt):
            first.run_one()

        second = runtime(env, handler, sink)
        assert second.run_one() is None                # аренда ещё не истекла
        env.clock.advance(minutes=6)
        assert second.run_one() == "done"

        assert handler.calls == {"collect": 1, "write": 1}
        assert len(sink.reports) == 1
        job_id = sink.reports[0][0].job_id
        state = WorkforceStore(env.connect_new_connection()).job_state(job_id=job_id)
        assert state["status"] == "done" and state["attempts"] == 2
        assert [s["status"] for s in state["steps"]] == ["done", "done"]


def test_failed_handler_fails_the_job_and_sends_no_report():
    with harness_env() as env:
        active_twin(env, env.register())
        sink = Sink()
        rt = runtime(env, Handler(fail=True), sink)
        rt.schedule()
        assert rt.run_one() == "failed" and sink.reports == []


def test_paused_employee_job_is_not_executed():
    with harness_env() as env:
        drafted = active_twin(env, env.register())
        rt = runtime(env, Handler())
        rt.schedule()
        env.connection.execute("UPDATE wf_employees SET status = 'paused' WHERE employee_id = %s", (drafted["employee_id"],))
        env.connection.commit()
        assert rt.run_one() == "failed"


# --- расширение паспорта по новому подключению (C-6) --------------------------------------------

def test_new_connection_proposes_a_passport_version_with_the_new_tool(monkeypatch):
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        monkeypatch.setattr(env.researcher, "tools", lambda harness_id: (["crm"], ["order:read"]))

        proposal_ids = env.workforce.propose_tool_extension(me, system="crm")
        assert len(proposal_ids) == 1
        proposal = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="passport", status="pending")[0]
        assert proposal.subject["version"] == 2 and proposal.subject["previous_version"] == 1
        assert proposal.subject["passport"]["tools"]["connectors"] == ["crm"]

        token = env.approve(me, proposal.id)
        env.workforce.on_passport_approved(me, proposal_id=proposal.id, token=token)
        twin = next(e for e in env.workforce.list(me) if e["employee_id"] == drafted["employee_id"])
        assert twin["active_version"] == 2
        assert env.workforce.propose_tool_extension(me, system="crm") == []   # уже есть — нечего предлагать


def test_fake_drafter_is_isolated_per_env():
    assert FakeDrafter().calls == []
