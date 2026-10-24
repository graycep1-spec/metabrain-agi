"""Настройки: баланс/токены, галочка мета-мозга; подключения: LLM, каталог, кастом."""

from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode
from uuid import uuid4

from src.landscape.catalog import catalog
from tests.support_harness import harness_env
from tests.test_llm_settings import FORM, csrf, logged_in


def seed_usage(env, me):
    with env.connection.transaction():
        env.connection.execute(
            """
            INSERT INTO llm_usage
                (id, harness_id, employee_id, provider, model, prompt_tokens,
                 completion_tokens, total_tokens, cost_usd, created_at)
            VALUES (%s, %s, %s, 'deepseek', 'deepseek-flash', 100, 50, 150, %s, %s)
            """,
            (uuid4(), str(me.harness_id), str(uuid4()), Decimal("0.004"), datetime.now(timezone.utc)),
        )


def test_settings_show_llm_balance_tokens_and_metabrain_toggle():
    with harness_env() as env:
        me = env.register("a")
        seed_usage(env, me)
        web = logged_in(env, editable=True)

        page = web.get("/settings")
        assert page.status_code == 200
        assert "Баланс и токены" in page.text and "5.00" in page.text
        assert "0.0040" in page.text and "150" in page.text
        assert "Мета-мозг" in page.text and 'name="enabled"' in page.text
        assert "подключать мета-мозг" in page.text.lower()

        off = web.post("/settings/metabrain", content=urlencode({"csrf_token": csrf(web)}),
                       headers=FORM, follow_redirects=False)
        assert off.status_code == 303
        assert env.connection.execute(
            "SELECT metabrain_enabled FROM org_settings WHERE company_id = %s", (me.company_id,)
        ).fetchone()[0] is False
        assert "Мета-мозг" in web.get("/settings").text


def test_connections_show_llm_card_catalog_and_custom_connection():
    with harness_env(vault_key=b"x" * 32) as env:
        me = env.register("a")
        web = logged_in(env, editable=True)

        page = web.get("/connections")
        assert page.status_code == 200
        assert "Внешняя модель LLM" in page.text and "активно" in page.text
        assert "sk-••••••••" in page.text
        assert "Кастомное подключение" in page.text
        for expected in ("ИТ-инфраструктура", "Платформы", "Бизнес-системы",
                         "Active Directory", "Outlook.com / Microsoft 365",
                         "RDP", "SSH", "WinRM", "Техническая учётная запись"):
            assert expected in page.text, expected

        created = web.post(
            "/connections/custom",
            content=urlencode({
                "csrf_token": csrf(web), "name": "Внутренний SSH", "kind": "ssh",
                "target": "ssh://host.example:22", "auth_type": "ssh_key",
                "secret": "secret-value", "allowed_roles": "Куратор, ИТ-администратор",
                "note": "только чтение",
            }),
            headers=FORM, follow_redirects=False,
        )
        assert created.status_code == 303
        page = web.get("/connections")
        assert "Внутренний SSH" in page.text and "ssh://host.example:22" in page.text
        assert "••••••••" in page.text and "secret-value" not in page.text

        row = env.connection.execute(
            "SELECT connection_id, status, allowed_roles FROM custom_connections "
            "WHERE harness_id = %s AND name = 'Внутренний SSH'", (me.harness_id,)
        ).fetchone()
        assert row is not None and row[1] == "configured" and "ИТ-администратор" in row[2]
        secret = env.connection.execute(
            "SELECT system FROM harness_secrets WHERE system = %s", (f"custom:{row[0]}",)
        ).fetchone()
        assert secret is not None


def test_catalog_has_required_groups_and_role_policies():
    groups = {group["id"]: group for group in catalog()}
    assert set(groups) == {"it", "platforms", "business"}
    names = {item["name"] for group in groups.values() for item in group["connectors"]}
    assert {"Active Directory", "Outlook.com / Microsoft 365", "RDP", "SSH", "WinRM"} <= names
    for group in groups.values():
        for item in group["connectors"]:
            assert item["allowed_roles"] and item["denied_roles"] and item["auth"]


def test_custom_connection_without_secret_needs_credentials():
    with harness_env(vault_key=b"x" * 32) as env:
        env.register("a")
        web = logged_in(env, editable=True)
        created = web.post(
            "/connections/custom",
            content=urlencode({
                "csrf_token": csrf(web), "name": "Без секрета", "kind": "custom",
                "target": "https://api.example", "auth_type": "token",
                "allowed_roles": "Куратор", "note": "",
            }),
            headers=FORM, follow_redirects=False,
        )
        assert created.status_code == 303
        row = env.connection.execute(
            "SELECT status, secret_ref FROM custom_connections WHERE name = 'Без секрета'"
        ).fetchone()
        assert row == ("needs_credentials", None)
