"""T-32, T-33: экраны кабинета — паспорт и его правка (GT-c1-12), предложения (GT-c1-11, R-28), журнал."""

from urllib.parse import parse_qs, urlsplit

import pytest

from src.workforce.passport import DraftError, apply_patch, passport_diff
from src.workforce.service import PassportConflict
from tests.support_harness import harness_env
from tests.test_web_auth import FakeTelegram, claims, harness_client, make_auth, op, telegram_id, web_with
from tests.test_workforce import active_twin


FORM = {"content-type": "application/x-www-form-urlencoded"}


def logged_in(env):
    telegram = FakeTelegram()
    auth = make_auth(env, telegram)
    web = web_with(env, auth)
    start = web.get("/login/start", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))
    assert web.get(f"/auth/callback?code=code&state={query['state']}").status_code == 200
    return web, auth


def csrf(web) -> str:
    return web.cookies.get("mb_session").split(".", 1)[1]


def post(web, url, fields: dict, **kwargs):
    from urllib.parse import urlencode
    return web.post(url, content=urlencode(fields), headers=FORM, follow_redirects=False, **kwargs)


def edit_fields(web, employee_id, **changes):
    page = web.get(f"/employees/{employee_id}/edit")
    assert page.status_code == 200
    fields = {
        "csrf_token": csrf(web), "title": "Менеджер по продажам", "mission": "Готовить КП по заявкам",
        "duties": "Разбирать новые заявки\nСверять наличие на складе",
        "goals": "КП за 2 часа | quote_hours | 2 | day", "may": "предлагать аналоги", "must_not": "скидка ниже min_margin",
        "escalation": "нет позиции и аналога", "schedule": "*/15 * * * * | 1",
    }
    fields.update(changes)
    return fields


# --- правка паспорта (PostgreSQL: нужен действующий паспорт) ------------------------------------------------------------------

def passport():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        return env.workforce.get(me, employee_id=drafted["employee_id"])["passport"]


@pytest.mark.parametrize("patch", [
    {"tools": {"connectors": ["erp"]}},
    {"goals": [{"text": "КП быстро"}]},
    {"schedule": [{"cron": "каждый час", "duty": 1}]},
    {"schedule": [{"cron": "*/5 * * * *", "duty": 7}]},
    {},
])
def test_patch_rejects_tools_bad_goals_and_bad_schedule(patch):
    with pytest.raises(DraftError):
        apply_patch(passport(), patch)


def test_diff_shows_added_and_removed_lines_and_changed_values():
    base = passport()
    draft = apply_patch(base, {"duties": [{"text": "Разбирать новые заявки"}, {"text": "Сверять наличие"}],
                               "role": {"title": "Старший менеджер", "mission": base["role"]["mission"]}})
    changed = {**base, "role": draft["role"], "duties": draft["duties"]}
    diff = {d["field"]: d for d in passport_diff(base, changed)}
    assert diff["duties"]["added"] == ["Сверять наличие"] and diff["duties"]["removed"] == []
    assert (diff["role.title"]["before"], diff["role.title"]["after"]) == ("Менеджер по продажам", "Старший менеджер")


# --- GT-c1-12: правка в кабинете — предложение, а не изменение ----------------------------------

def test_gt_c1_12_cabinet_edit_creates_a_proposal_and_keeps_the_active_version():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, _ = logged_in(env)
        employee_id = drafted["employee_id"]

        card = web.get(f"/employees/{employee_id}")
        assert card.status_code == 200 and "Разбирать новые заявки" in card.text and "Изменить паспорт" in card.text

        response = post(web, f"/employees/{employee_id}/edit", edit_fields(web, employee_id))
        assert response.status_code == 303 and response.headers["location"] == "/proposals?notice=proposed"

        listing = web.get("/proposals")
        assert "Сверять наличие на складе" in listing.text and "class=\"added\"" in listing.text
        state = env.workforce.get(me, employee_id=drafted["employee_id"])
        assert state["active_version"] == 1
        assert [v["status"] for v in state["versions"]] == ["proposed", "active"]


def test_edit_form_errors_are_shown_and_nothing_is_proposed():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, _ = logged_in(env)
        employee_id = drafted["employee_id"]

        bad_schedule = post(web, f"/employees/{employee_id}/edit", edit_fields(web, employee_id, schedule="*/15 * * * *"))
        bad_cron = post(web, f"/employees/{employee_id}/edit", edit_fields(web, employee_id, schedule="каждый час | 1"))
        no_change = post(web, f"/employees/{employee_id}/edit", edit_fields(web, employee_id, duties="Разбирать новые заявки"))
        assert bad_schedule.status_code == 400 and "cron | номер обязанности" in bad_schedule.text
        assert bad_cron.status_code == 400 and "Проверьте форму" in bad_cron.text
        assert no_change.status_code == 400
        assert len(env.workforce.get(me, employee_id=employee_id)["versions"]) == 1


def test_edit_without_csrf_is_refused():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, _ = logged_in(env)
        fields = edit_fields(web, drafted["employee_id"])
        del fields["csrf_token"]
        assert post(web, f"/employees/{drafted['employee_id']}/edit", fields).status_code == 403
        assert len(env.workforce.get(me, employee_id=drafted["employee_id"])["versions"]) == 1


def test_draft_is_edited_before_the_first_approval_and_replaces_its_proposal():
    """Шаг 2 демо: черновик → правка расписания в кабинете → подтверждение."""
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        env.connection.commit()
        web, _ = logged_in(env)
        employee_id = drafted["employee_id"]
        card = web.get(f"/employees/{employee_id}")
        assert "Изменить черновик" in card.text

        response = post(web, f"/employees/{employee_id}/edit", edit_fields(web, employee_id, schedule="0 9 * * 1-5 | 1"))
        assert response.status_code == 303
        page = web.get("/proposals")
        assert page.text.count("Изменить паспорт") == 1 and "0 9 * * 1-5" in page.text   # одно предложение, новый черновик
        old = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="passport", status="returned")
        assert [str(p.id) for p in old] == [drafted["proposal_id"]]

        researcher = next(e for e in env.workforce.list(me) if e["kind"] == "researcher")
        with pytest.raises(PassportConflict):
            env.workforce.propose_edit(me, employee_id=researcher["employee_id"], patch={"duties": [{"text": "x"}]})


# --- GT-c1-11, R-28: решение в кабинете, повтор из Telegram — 409 -------------------------------

def test_gt_c1_11_decision_in_cabinet_then_telegram_button_is_409_and_one_journal_entry():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, auth = logged_in(env)
        post(web, f"/employees/{drafted['employee_id']}/edit", edit_fields(web, drafted["employee_id"]))
        proposal_id = env.workforce.get(me, employee_id=drafted["employee_id"])["pending_proposals"][0]

        decided = post(web, f"/proposals/{proposal_id}/decide", {"csrf_token": csrf(web), "decision": "approve"})
        assert decided.headers["location"] == "/proposals?notice=approved"
        assert env.workforce.get(me, employee_id=drafted["employee_id"])["active_version"] == 2

        telegram = op(harness_client(env, auth), "proposal.decide", channel_user_id=str(telegram_id()),
                      proposal_id=proposal_id, decision="approve", surface="telegram")
        assert telegram[0] == 409
        again = post(web, f"/proposals/{proposal_id}/decide", {"csrf_token": csrf(web), "decision": "return"})
        assert again.headers["location"] == "/proposals?notice=conflict"
        assert "уже решено" in web.get("/proposals?notice=conflict").text

        decisions = env.connection.execute(
            "SELECT payload FROM journal_entries WHERE kind = 'proposal.decision' AND payload->>'proposal_id' = %s",
            (proposal_id,),
        ).fetchall()
        assert len(decisions) == 1 and decisions[0][0]["surface"] == "web"


def test_return_in_cabinet_marks_the_version_returned():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, _ = logged_in(env)
        post(web, f"/employees/{drafted['employee_id']}/edit", edit_fields(web, drafted["employee_id"]))
        proposal_id = env.workforce.get(me, employee_id=drafted["employee_id"])["pending_proposals"][0]
        post(web, f"/proposals/{proposal_id}/decide", {"csrf_token": csrf(web), "decision": "return"})
        state = env.workforce.get(me, employee_id=drafted["employee_id"])
        assert state["active_version"] == 1 and [v["status"] for v in state["versions"]] == ["returned", "active"]


def test_decision_without_csrf_and_surface_spoofing_are_refused():
    with harness_env() as env:
        me = env.register()
        drafted = env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        env.connection.commit()
        web, auth = logged_in(env)
        assert post(web, f"/proposals/{drafted['proposal_id']}/decide", {"decision": "approve"}).status_code == 403
        session_token, csrf_token = web.cookies.get("mb_session").split(".", 1)
        spoof = op(harness_client(env, auth), "proposal.decide", session_token=session_token, csrf_token=csrf_token,
                   proposal_id=drafted["proposal_id"], decision="approve", surface="telegram")
        assert spoof[0] == 400
        assert env.workforce.get(me, employee_id=drafted["employee_id"])["status"] == "draft"


def test_proposal_previews_for_new_passport_connect_and_adapter():
    with harness_env() as env:
        me = env.register()
        env.workforce.draft(me, kind="twin", role_text="Я менеджер")
        connect = env.connect(me)
        env.connection.commit()
        web, _ = logged_in(env)
        listing = web.get("/proposals").text
        assert "Автономия: L0" in listing and "Подключить систему <b>crm</b>" in listing

        decided = post(web, f"/proposals/{connect.id}/decide", {"csrf_token": csrf(web), "decision": "approve"})
        assert decided.status_code == 303
        listing = web.get("/proposals").text
        assert "Система <b>crm</b>, только чтение: order:read" in listing


# --- журнал --------------------------------------------------------------------------------------

def test_journal_lists_entries_with_filters_and_names():
    with harness_env() as env:
        me = env.register()
        drafted = active_twin(env, me)
        web, _ = logged_in(env)

        everything = web.get("/journal")
        assert everything.status_code == 200 and "Паспорт действует" in everything.text and "Вход в кабинет" in everything.text
        decisions = web.get("/journal?kind=proposal.decision")
        assert "Решение куратора" in decisions.text and "Вход в кабинет</td>" not in decisions.text
        by_employee = web.get(f"/journal?employee_id={drafted['employee_id']}")
        assert "Менеджер по продажам" in by_employee.text


def test_revoked_session_goes_back_to_login():
    with harness_env() as env:
        env.register()
        web, auth = logged_in(env)
        session_token = web.cookies.get("mb_session").split(".", 1)[0]
        env.connection.execute("UPDATE web_sessions SET revoked_at = now()")
        env.connection.commit()
        response = web.get("/proposals", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/login"
        assert session_token


def test_employees_screen_groups_by_role_and_filters():
    """П.1: управление ЦС по ролям — группировка, фильтры, быстрая пауза."""
    with harness_env() as env:
        me = env.register()
        active_twin(env, me)   # активный двойник «Менеджер по продажам» + служебный Исследователь
        web, _ = logged_in(env)

        home = web.get("/")
        assert home.status_code == 200 and "Сотрудники по ролям" in home.text
        assert "Менеджер по продажам" in home.text and "Исследователь" in home.text
        assert "Пауза" in home.text   # быстрое действие у активного двойника

        only_twin = web.get("/", params={"role": "Менеджер по продажам"})
        assert "Менеджер по продажам" in only_twin.text and "Исследователь" not in only_twin.text

        paused = web.get("/", params={"status": "paused"})
        assert "По фильтру никого" in paused.text


def test_self_registration_creates_org_in_cabinet_end_to_end():
    """ADR-030: новый человек входит через Telegram и создаёт организацию прямо в кабинете."""
    from urllib.parse import urlencode
    with harness_env() as env:                                    # никто не зарегистрирован
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        web = web_with(env, auth)
        start = web.get("/login/start", follow_redirects=False)
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))

        cb = web.get(f"/auth/callback?code=code&state={query['state']}", follow_redirects=False)
        assert cb.status_code == 303 and cb.headers["location"] == "/bootstrap"
        assert any(c.startswith("mb_bootstrap") for c in cb.headers.get_list("set-cookie"))
        bootstrap_page = web.get("/bootstrap").text
        assert "Создать организацию" in bootstrap_page and "Присоединиться по приглашению" in bootstrap_page

        created = web.post("/bootstrap", content=urlencode({"company_name": "ООО Саморег"}),
                           headers={"content-type": "application/x-www-form-urlencoded"}, follow_redirects=False)
        assert created.status_code == 200 and 'http-equiv="refresh"' in created.text
        assert any(c.startswith("mb_session") for c in created.headers.get_list("set-cookie"))

        home = web.get("/")
        assert home.status_code == 200 and "Исследователь" in home.text          # новый харнесс со служебным ЦС
        names = [r[0] for r in env.connection.execute("SELECT name FROM companies").fetchall()]
        assert "ООО Саморег" in names
        # шаблон новой организации наполнен автоматически (ADR-030): пример-сотрудник + процессы
        assert env.connection.execute("SELECT count(*) FROM wf_employees WHERE kind <> 'researcher'").fetchone()[0] >= 1
        assert env.connection.execute("SELECT count(*) FROM wf_processes").fetchone()[0] >= 2
        assert "Менеджер по продажам" in home.text


def _begin_bootstrap(env, suffix="a"):
    """Новый telegram-пользователь доходит до экрана /bootstrap; возвращаем его web-клиент с тикетом."""
    telegram = FakeTelegram()
    web = web_with(env, make_auth(env, telegram))
    start = web.get("/login/start", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    telegram.issue("code", query["code_challenge"], claims(telegram_id(suffix), query["nonce"]))
    cb = web.get(f"/auth/callback?code=code&state={query['state']}", follow_redirects=False)
    assert cb.status_code == 303 and cb.headers["location"] == "/bootstrap"
    return web


def test_colleague_joins_existing_org_by_invite_code_in_cabinet():
    """ADR-030: куратор создаёт код приглашения в кабинете, коллега входит и присоединяется к той же орг."""
    import re
    with harness_env() as env:
        owner = _begin_bootstrap(env, "a")
        assert post(owner, "/bootstrap", {"company_name": "ООО Владелец"}).status_code == 200
        processes_before = env.connection.execute("SELECT count(*) FROM wf_processes").fetchone()[0]

        # куратор создаёт код приглашения прямо в кабинете
        invite_page = post(owner, "/invite", {"csrf_token": csrf(owner)})
        assert invite_page.status_code == 200
        code = re.search(r"start=([^\s<]+)", invite_page.text).group(1)

        # новый коллега входит через Telegram и присоединяется по коду
        joiner = _begin_bootstrap(env, "b")
        joined = post(joiner, "/bootstrap", {"invite": code})
        assert joined.status_code == 200
        assert any(c.startswith("mb_session") for c in joined.headers.get_list("set-cookie"))

        # присоединился к существующей орг: новой компании нет, обе харнессы в одной орг, шаблон не пересеивался
        assert env.connection.execute("SELECT count(*) FROM companies").fetchone()[0] == 1
        assert env.connection.execute("SELECT count(DISTINCT company_id) FROM harnesses").fetchone()[0] == 1
        assert env.connection.execute("SELECT count(*) FROM harnesses").fetchone()[0] == 2
        assert env.connection.execute("SELECT count(*) FROM wf_processes").fetchone()[0] == processes_before

        # у новичка своё пространство: служебный Исследователь есть, а пример-сотрудник
        # по-прежнему один — у владельца (присоединение шаблон не пересеивает)
        home = joiner.get("/")
        assert home.status_code == 200 and "Исследователь" in home.text
        assert env.connection.execute(
            "SELECT count(*) FROM wf_employees WHERE kind <> 'researcher'"
        ).fetchone()[0] == 1


def test_invalid_invite_code_keeps_ticket_and_allows_retry():
    """Опечатка в коде не должна сжигать одноразовый тикет — вход можно повторить."""
    with harness_env() as env:
        web = _begin_bootstrap(env, "a")
        bad = post(web, "/bootstrap", {"invite": "no-such-code"})
        assert bad.status_code == 400 and "Код приглашения не найден" in bad.text
        assert web.cookies.get("mb_bootstrap")                     # тикет цел
        ok = post(web, "/bootstrap", {"company_name": "ООО Повтор"})
        assert ok.status_code == 200
        assert any(c.startswith("mb_session") for c in ok.headers.get_list("set-cookie"))


def test_bootstrap_page_without_ticket_redirects_to_login():
    with harness_env() as env:
        telegram = FakeTelegram()
        web = web_with(env, make_auth(env, telegram))
        assert web.get("/bootstrap", follow_redirects=False).headers["location"] == "/login"
