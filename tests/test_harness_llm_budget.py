from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal
import os
from uuid import uuid4

import psycopg
import pytest

from src.harness.llm_usage import PostgresBudgetLedger
from src.llm.budget import PolicyBudgetGuard
from src.llm.connector import (
    BudgetExceededError,
    LLMContext,
    ProviderConfig,
    TokenUsage,
    UsageEvent,
)
from src.metabrain.policy import load_policy
from src.harness.store import HarnessStore


@pytest.fixture
def budget_ledger() -> Iterator[tuple[psycopg.Connection, PostgresBudgetLedger]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL contract tests")

    schema = f"test_llm_budget_{uuid4().hex}"
    with psycopg.connect(database_url) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        admin.commit()

    connection = psycopg.connect(database_url, options=f"-c search_path={schema}")
    try:
        HarnessStore(connection)
        yield connection, PostgresBudgetLedger(connection)
    finally:
        connection.close()
        with psycopg.connect(database_url) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
            admin.commit()


def test_gt_c2_09_persistent_installation_budget_covers_all_harnesses(budget_ledger):
    connection, ledger = budget_ledger
    policy = load_policy()
    connection.execute(
        """
        INSERT INTO llm_usage
            (id, harness_id, employee_id, provider, model, prompt_tokens,
             completion_tokens, total_tokens, cost_usd, created_at)
        VALUES (%s, %s, %s, %s, %s, 10, 10, 20, %s, %s)
        """,
        (
            uuid4(),
            "harness-1",
            "employee-1",
            "deepseek-flash",
            "deepseek-flash",
            Decimal("4.99"),
            datetime.now(timezone.utc),
        ),
    )
    guard = PolicyBudgetGuard(policy=policy, ledger=ledger)
    provider = ProviderConfig(
        name="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        api_key_ref="env:DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
        family="deepseek-v4-pro",
        max_tokens=8192,
        temperature=0,
    )
    context = LLMContext(
        company_id="company-2",
        harness_id="harness-2",
        employee_id="employee-2",
        job_id="job-2",
        sub_role="critic",
    )

    with pytest.raises(BudgetExceededError) as error:
        guard.check(context, provider, {"messages": [{"content": "hello"}]})

    assert error.value.limit_name == "budget_usd_total.installation"
    assert error.value.limit == Decimal("5.0")
    assert error.value.remaining == Decimal("0.01")
    assert connection.execute(
        "SELECT count(*) FROM llm_budget_reservations"
    ).fetchone()[0] == 0


def test_settling_reservation_persists_actual_cost_and_releases_estimate(budget_ledger):
    connection, ledger = budget_ledger
    policy = load_policy()
    guard = PolicyBudgetGuard(policy=policy, ledger=ledger)
    context = LLMContext(
        company_id="company-1",
        harness_id="harness-1",
        employee_id="employee-1",
        job_id="job-1",
        sub_role="critic",
    )
    provider = ProviderConfig(
        name="deepseek-v4-pro",
        base_url="https://api.deepseek.com",
        api_key_ref="env:DEEPSEEK_API_KEY",
        model="deepseek-v4-pro",
        family="deepseek-v4-pro",
        max_tokens=8192,
        temperature=0,
    )
    reservation = guard.check(context, provider, {"messages": [{"content": "hello"}]})
    event = UsageEvent(
        context=context,
        provider=provider.name,
        model=provider.model,
        usage=TokenUsage(10, 5, 15),
        cost=Decimal("0.00002"),
        latency_ms=2.0,
    )

    guard.settle(reservation, event)

    assert connection.execute(
        "SELECT count(*) FROM llm_budget_reservations"
    ).fetchone()[0] == 0
    assert connection.execute(
        "SELECT harness_id, employee_id, total_tokens, cost_usd FROM llm_usage"
    ).fetchone() == ("harness-1", "employee-1", 15, Decimal("0.000020000"))
