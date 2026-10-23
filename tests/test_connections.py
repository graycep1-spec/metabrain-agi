"""T-34: экран «Подключения и карта», ввод доступа в хранилище секретов харнесса (ADR-028) — GT-c1-13, GT-c1-15.

Путь из демо: `/connect crm` в Telegram → ссылка в кабинет → доступ в форме → `vault:<id>` → предложение `connect`
→ решение куратора → Исследователь читает `/schema` с этим доступом.
"""

import base64
import logging
import os
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.harness.api import create_app as create_harness
from src.harness.vault import VaultUnavailable, load_key
from src.web.app import create_app as create_web, http_harness
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, LANDSCAPE_TOKEN_A, harness_env
from tests.test_web_auth import API_TOKEN, FakeTelegram, claims, make_auth, telegram_id


KEY = os.urandom(32)
FORM = {"content-type": "application/x-www-form-urlencoded"}
USER_A = str(telegram_id("a"))


class Recorder:
    """Все ответы харнесса кабинету — чтобы проверить, что секрета в них нет."""

    def __init__(self, client: TestClient):
        self.client, self.bodies = client, []

    def post(self, *args, **kwargs):
        response = self.client.post(*args, **kwargs)
        self.bodies.append(response.text)
        return response


def logged_in(env):
    """Кабинет с входом по Telegram OIDC поверх харнесса с хранилищем секретов."""
    telegram = FakeTelegram()
    auth = make_auth(env, telegram)
    harness = TestClient(create_harness(
        env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
        secret_store=EnvSecretStore(
            {"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY}
        ), web_auth=auth,
        workforce=env.workforce, researcher=env.researcher, vault=env.vault,
    ))
    recorder = Recorder(harness)
    web = TestClient(create_web(http_harness("http://harness", API_TOKEN, client=recorder),
                                web_base_url="https://cabinet.example.test"), base_url="https://cabinet.example.test")
    start = web.get("/login/start", follow_redirects=False)
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    telegram.issue("code", query["code_challenge"], claims(telegram_id(), query["nonce"]))
    assert web.get(f"/auth/callback?code=code&state={query['state']}").status_code == 200
    return web, harness, recorder


def bot(harness, op, **payload):
    response = harness.post(f"/v1/operations/{op}", json={"channel_user_id": USER_A, **payload},
                            headers={"Authorization": f"Bearer {API_TOKEN}"})
    return response.status_code, response.json()


def csrf(web) -> str:
    return web.cookies.get("mb_session").split(".", 1)[1]


def submit(web, fields):
    return web.post("/connections", content=urlencode({"csrf_token": csrf(web), **fields}), headers=FORM,
                    follow_redirects=False)


def dump(env) -> str:
    """Все строки всех таблиц схемы харнесса текстом — кроме шифротекста хранилища."""
    tables = [row[0] for row in env.connection.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (env.schema,)
    ).fetchall()]
    chunks = []
    for table in tables:
        columns = "secret_id, harness_id, system" if table == "harness_secrets" else "*"
        rows = env.connection.execute(f'SELECT row_to_json(t)::text FROM (SELECT {columns} FROM "{table}") t').fetchall()
        chunks.extend(row[0] for row in rows)
    return "\n".join(chunks)


# --- хранилище ------------------------------------------------------------------------------

def test_vault_keeps_only_ciphertext_bound_to_harness_and_system():
    with harness_env(vault_key=KEY) as env:
        me, other = env.register("a"), env.register("b")
        ref = env.vault.put(me, system="crm", secret="  token-123456  ")
        assert ref.startswith("vault:") and env.vault.resolve(ref) == "token-123456"
        assert env.vault.owner(ref) == (me.harness_id, "crm") and env.vault.owner("vault:" + str(uuid4())) is None
        assert "token-123456" not in dump(env)
        raw = env.connection.execute("SELECT ciphertext FROM harness_secrets").fetchone()[0]
        assert b"token-123456" not in bytes(raw)

        # Шифротекст, переставленный в другой харнесс или систему, не расшифровывается (AAD).
        env.connection.execute("UPDATE harness_secrets SET harness_id = %s, company_id = %s",
                               (other.harness_id, other.company_id))
        with pytest.raises(SecretNotFoundError):
            env.vault.resolve(ref)
        with pytest.raises(ValueError):
            env.vault.put(me, system="crm", secret="line\nbreak")


def test_vault_without_key_refuses_and_key_must_be_32_bytes():
    with harness_env() as env:
        me = env.register("a")
        with pytest.raises(VaultUnavailable):
            env.vault.put(me, system="crm", secret="token")
    assert len(load_key(base64.b64encode(KEY).decode())) == 32
    with pytest.raises(ValueError):
        load_key(base64.b64encode(b"short").decode())


# --- GT-c1-13: доступ из формы — только в хранилище ------------------------------------------

def test_connect_start_rejects_systems_outside_configured_registry():
    with harness_env() as env:
        env.register("a")
        harness = TestClient(create_harness(
            env.connection,
            api_token=API_TOKEN,
            app_base_url="https://cabinet.example.test",
            secret_store=EnvSecretStore(
                {"CONSENT_SIGNING_KEY": CONSENT_KEY, "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY}
            ),
            system_registry={"crm"},
        ))

        assert bot(harness, "landscape.connect.start", system="crm")[0] == 201
        assert bot(harness, "landscape.connect.start", system="erp")[0] == 400

def test_gt_c1_13_access_from_the_cabinet_reaches_the_system_and_nowhere_else(caplog):
    caplog.set_level(logging.DEBUG)
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        web, harness, recorder = logged_in(env)

        status, started = bot(harness, "landscape.connect.start", system="crm")
        assert status == 201
        link = urlsplit(started["cabinet_url"])
        assert link.path == "/connections"
        form = web.get(f"/connections?{link.query}")
        assert form.status_code == 200 and 'value="crm" selected' in form.text
        assert started["connect_request_id"] in form.text and 'type="password"' in form.text

        response = submit(web, {"system": "crm", "secret": LANDSCAPE_TOKEN_A,
                                "connect_request_id": started["connect_request_id"]})
        assert response.status_code == 303 and response.headers["location"] == "/proposals?notice=connect"
        proposals = web.get("/proposals?notice=connect")
        assert "Доступ сохранён, подключение предложено" in proposals.text

        status, listed = bot(harness, "proposal.list", kind="connect", status="pending")
        connect = listed["proposals"][0]
        assert connect["payload"]["secret_ref"].startswith("vault:")

        # Решение куратора в кабинете → Исследователь читает /schema с доступом из хранилища → предложение адаптера.
        decided = web.post(f"/proposals/{connect['proposal_id']}/decide",
                           content=urlencode({"csrf_token": csrf(web), "decision": "approve"}), headers=FORM,
                           follow_redirects=False)
        assert decided.status_code == 303 and decided.headers["location"] == "/proposals?notice=approved"
        status, listed = bot(harness, "proposal.list", kind="adapter", status="pending")
        assert len(listed["proposals"]) == 1
        assert listed["proposals"][0]["payload"]["manifest"]["auth"]["secret_ref"] == connect["payload"]["secret_ref"]

        everything = "\n".join(recorder.bodies) + dump(env) + caplog.text + form.text + proposals.text
        assert LANDSCAPE_TOKEN_A not in everything
        status, journal = bot(harness, "journal.list", kind="secret.stored")
        assert journal["entries"][0]["payload"] == {"system": "crm", "secret_ref": connect["payload"]["secret_ref"]}


def test_landscape_map_shows_each_system_state():
    with harness_env(vault_key=KEY) as env:
        me = env.register("a")
        web, harness, _ = logged_in(env)

        def states():
            status, body = bot(harness, "landscape.map")
            assert status == 200
            return {s["system"]: s["status"] for s in body["systems"]}

        assert states() == {"crm": "available", "stock": "available", "pricing": "available"}
        submit(web, {"system": "crm", "secret": LANDSCAPE_TOKEN_A})
        assert states()["crm"] == "pending"
        connect = env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="connect")[0]
        applied = env.researcher.on_connect_approved(me, proposal_id=connect.id, token=env.approve(me, connect.id))
        assert states()["crm"] == "pending"  # ждёт решения по адаптеру
        adapter = applied["adapter_proposal_id"]
        env.researcher.on_adapter_approved(me, proposal_id=adapter, token=env.approve(me, adapter))
        env.connection.commit()
        assert states()["crm"] == "active"

        page = web.get("/connections")
        assert "работает" in page.text and "Карта ландшафта" in page.text and "order" in page.text
        assert '<option value="crm"' not in page.text  # подключённую систему повторно не предлагаем


# --- отказы -----------------------------------------------------------------------------------

def test_connect_refuses_foreign_or_service_secret_refs_and_unknown_systems():
    with harness_env(vault_key=KEY) as env:
        env.register("a")
        other = env.register("b")
        _, harness, _ = logged_in(env)
        foreign = env.vault.put(other, system="crm", secret="foreign-token")
        status, mine = bot(harness, "landscape.secret.put", system="stock", secret="stock-token")
        assert status == 201

        for system, ref in (("crm", foreign), ("crm", mine["secret_ref"]), ("crm", "env:CONSENT_SIGNING_KEY"),
                            ("crm", "env:DEEPSEEK_API_KEY"), ("crm", "env:HARNESS_DATABASE_URL"), ("erp", "env:CRM_TOKEN_A")):
            status, body = bot(harness, "landscape.connect", system=system, secret_ref=ref)
            assert status == 400, (system, ref, body)
        assert bot(harness, "landscape.secret.put", system="erp", secret="x")[0] == 400
        assert bot(harness, "landscape.connect.start", system="erp")[0] == 400
        assert bot(harness, "landscape.connect", system="crm", secret_ref="env:CRM_TOKEN_A")[0] == 201


def test_without_store_key_the_form_explains_and_nothing_is_proposed():
    with harness_env() as env:
        me = env.register("a")
        web, harness, _ = logged_in(env)
        assert bot(harness, "landscape.secret.put", system="crm", secret="token")[0] == 503
        response = submit(web, {"system": "crm", "secret": "token-xyz"})
        assert response.status_code == 503 and "SECRET_STORE_KEY" in response.text and "token-xyz" not in response.text
        assert env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="connect") == ()


def test_expired_or_used_telegram_link_is_refused_and_the_form_drops_it():
    with harness_env(vault_key=KEY) as env:
        me = env.register("a")
        web, harness, _ = logged_in(env)
        _, started = bot(harness, "landscape.connect.start", system="crm")
        assert submit(web, {"system": "crm", "secret": LANDSCAPE_TOKEN_A,
                            "connect_request_id": started["connect_request_id"]}).status_code == 303
        again = submit(web, {"system": "crm", "secret": "second-token-value",
                             "connect_request_id": started["connect_request_id"]})
        assert again.status_code == 400 and "устарела или уже использована" in again.text
        assert started["connect_request_id"] not in again.text and "second-token-value" not in again.text
        assert len(env.proposals.list_proposals(person_id=me.person_id, harness_id=me.harness_id, kind="connect")) == 1
