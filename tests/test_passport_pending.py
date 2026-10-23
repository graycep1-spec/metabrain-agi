"""C-6 «Одно ожидающее предложение паспорта»: правка и расширение tools заменяют прежнее предложение (T-28)."""

from uuid import UUID

import pytest

from src.harness.proposals import ProposalConflict
from tests.support_harness import harness_env
from tests.test_workforce import active_twin


def connect(env, me, system):
    """Подключение и адаптер подтверждены; предложения версий паспорта остаются ждать решения."""
    proposal = env.connect(me, system=system)
    connected = env.researcher.on_connect_approved(me, proposal_id=proposal.id, token=env.approve(me, proposal.id))
    adapter = connected["adapter_proposal_id"]
    return env.researcher.on_adapter_approved(me, proposal_id=adapter, token=env.approve(me, adapter))


def pending_passports(env, me):
    return env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="passport", status="pending")


def test_three_connections_in_a_row_leave_one_proposal_with_all_systems():
    with harness_env() as env:
        me = env.register()
        employee_id = UUID(active_twin(env, me)["employee_id"])
        first = connect(env, me, "crm")["passport_proposals"][0]
        connect(env, me, "stock")
        connect(env, me, "pricing")

        pending = pending_passports(env, me)
        assert len(pending) == 1
        assert sorted(pending[0].subject["passport"]["tools"]["connectors"]) == ["crm", "pricing", "stock"]
        assert pending[0].subject["previous_version"] == 1
        assert {d["field"] for d in pending[0].subject["diff"]} >= {"tools.connectors"}

        # Старое предложение уже отозвано: подтвердить его нельзя — откатить новые системы нечем.
        with pytest.raises(ProposalConflict):
            env.approve(me, UUID(first))
        env.workforce.on_passport_approved(me, proposal_id=pending[0].id, token=env.approve(me, pending[0].id))
        active = env.workforce.get(me, employee_id=employee_id)
        assert sorted(active["passport"]["tools"]["connectors"]) == ["crm", "pricing", "stock"]
        withdrawn = env.connection.execute(
            "SELECT count(*) FROM journal_entries WHERE harness_id = %s AND kind = 'proposal.withdrawn'", (me.harness_id,)
        ).fetchone()[0]
        assert withdrawn == 2


def test_curator_edit_keeps_new_tools_and_extension_keeps_the_edit():
    with harness_env() as env:
        me = env.register()
        employee_id = UUID(active_twin(env, me)["employee_id"])
        connect(env, me, "crm")                                   # ждёт: v2 с crm
        env.workforce.propose_edit(me, employee_id=employee_id, patch={"duties": [{"text": "Разбирать заявки"}]})
        connect(env, me, "stock")                                 # ждёт: v4 = правка + crm + stock

        pending = pending_passports(env, me)
        assert len(pending) == 1
        passport = pending[0].subject["passport"]
        assert [d["text"] for d in passport["duties"]] == ["Разбирать заявки"]
        assert sorted(passport["tools"]["connectors"]) == ["crm", "stock"]


def test_systems_connected_while_the_draft_waited_are_offered_right_after_approval():
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        connect(env, me, "crm")                                   # у черновика нет действующей версии — не расширяем

        applied = env.workforce.on_passport_approved(me, proposal_id=UUID(drafted["proposal_id"]),
                                                     token=env.approve(me, UUID(drafted["proposal_id"])))
        assert len(applied["passport_proposals"]) == 1
        pending = pending_passports(env, me)
        assert pending[0].subject["passport"]["tools"]["connectors"] == ["crm"]
