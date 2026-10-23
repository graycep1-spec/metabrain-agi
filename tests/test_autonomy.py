"""T-34: блок «Автономия» — уровни по классам действий и метрики против порогов политики (C-1 `employee.autonomy`, C-6).

Повышение и понижение (`employee.autonomy.request`/`.lower`, GT-c1-14, GT-c6-05) — в T-27: без классов записи (T-25) их нечем применить.
"""

from uuid import UUID

from src.workforce.service import unmet_thresholds
from src.workforce.store import WorkforceStore
from tests.support_harness import harness_env
from tests.test_web_cabinet import logged_in
from tests.test_workforce import active_twin


def decided(env, me, employee_id, kind, statuses, **subject):
    """Предложения сотрудника с решениями куратора: approve / return / incident."""
    for status in statuses:
        proposal = env.proposals.create_proposal(
            person_id=me.person_id, company_id=me.company_id, harness_id=me.harness_id, kind=kind,
            summary="Предложение", subject={"employee_id": str(employee_id), **subject},
        )
        if status != "pending":
            env.proposals.decide(person_id=me.person_id, harness_id=me.harness_id, proposal_id=proposal.id,
                                 decision=status, actor_person_id=me.person_id, surface="web")


def test_unmet_thresholds_treat_unknown_metrics_as_unmet():
    thresholds = {"min_decided": 10, "accepted_ratio": 0.8, "eval_score": 0.9}
    assert unmet_thresholds({"decided": 10, "accepted_ratio": 0.8, "eval_score": None}, thresholds) == ["eval_score"]
    assert unmet_thresholds({"decided": 3, "accepted_ratio": None}, thresholds) == ["min_decided", "accepted_ratio", "eval_score"]


def test_demo_twin_is_l0_and_recommendations_count_towards_the_l1_threshold():
    with harness_env() as env:
        me = env.register()
        employee_id = UUID(active_twin(env, me)["employee_id"])
        decided(env, me, employee_id, "recommendation", ["approve"] * 3 + ["return", "pending"])

        state = env.workforce.autonomy(me, employee_id=employee_id)
        assert state["classes"] == []
        assert state["recommendations"]["decided"] == 4 and state["recommendations"]["accepted_ratio"] == 0.75
        assert state["l1_unmet"] == ["min_decided", "accepted_ratio"]

        decided(env, me, employee_id, "recommendation", ["approve"] * 6)
        assert env.workforce.autonomy(me, employee_id=employee_id)["l1_unmet"] == []      # 10 решений, 90% принято


def test_write_classes_show_levels_metrics_and_eligibility():
    with harness_env() as env:
        me = env.register()
        employee_id = UUID(active_twin(env, me)["employee_id"])
        store = WorkforceStore(env.connection)
        current = store.passport(employee_id=employee_id, version=1)
        passport = {**current["passport"], "version": 2, "autonomy": {"quote.send": "L1"},
                    "tools": {**current["passport"]["tools"], "write_classes": ["quote.draft", "quote.send"]}}
        store.add_passport(employee_id=employee_id, version=2, passport=passport, artifact=current["artifact"],
                           scorecard=current["scorecard"])
        store.activate(employee_id=employee_id, version=2)
        decided(env, me, employee_id, "action", ["approve"] * 8 + ["return"] * 2, action_class="quote.draft")
        decided(env, me, employee_id, "action", ["approve"] * 25 + ["incident"], action_class="quote.send")

        classes = {c["action_class"]: c for c in env.workforce.autonomy(me, employee_id=employee_id)["classes"]}
        draft, send = classes["quote.draft"], classes["quote.send"]
        assert (draft["level"], draft["metrics"]["decided"], draft["eligible_for"]) == ("L0", 10, "L1")
        assert send["level"] == "L1" and send["metrics"]["incidents"] == 1 and send["metrics"]["incident_free_days"] == 0
        # L1 → L2: нет оценки на eval, свежий инцидент, класс не в policy.autonomy.allowed_l2_classes.
        assert send["eligible_for"] is None and set(send["unmet"]) == {"eval_score", "incident_free_days"}


def test_employee_card_shows_the_autonomy_block():
    with harness_env() as env:
        me = env.register()
        employee_id = UUID(active_twin(env, me)["employee_id"])
        decided(env, me, employee_id, "recommendation", ["approve", "approve"])
        web, _ = logged_in(env)
        card = web.get(f"/employees/{employee_id}")
        assert "Автономия" in card.text and "Уровень L0" in card.text
        assert "решено 2, принято 2 (100%)" in card.text and "Порог для L1: 10 решений" in card.text
