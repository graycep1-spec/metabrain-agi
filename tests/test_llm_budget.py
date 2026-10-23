from decimal import Decimal
from uuid import uuid4

import httpx
import pytest

from src.common.secrets import EnvSecretStore
from src.harness.llm_usage import PostgresBudgetLedger
from src.llm.budget import (
    PolicyBudgetGuard,
    PolicyCostCalculator,
)
from src.llm.connector import (
    BudgetExceededError,
    ChatRequest,
    LLMConnector,
    LLMContext,
    ProviderConfig,
    TokenUsage,
)
from src.metabrain.policy import load_policy


class RecordingLedger:
    def __init__(self, *, installation_spend=Decimal("0")):
        self.installation_spend = installation_spend
        self.reservations = []
        self.settled = []
        self.cancelled = []

    def reserve(self, context, estimated_cost, estimated_tokens, limits):
        total_limit = Decimal(str(limits["budget_usd_total"]["installation"]))
        if self.installation_spend + estimated_cost > total_limit:
            remaining = total_limit - self.installation_spend
            raise BudgetExceededError(
                f"budget_usd_total.installation limit {total_limit}; "
                f"remaining {remaining}; requested {estimated_cost}",
                limit=total_limit,
                limit_name="budget_usd_total.installation",
                remaining=remaining,
            )
        reservation = uuid4()
        self.reservations.append(
            (reservation, context, estimated_cost, estimated_tokens, limits)
        )
        return reservation

    def settle(self, reservation, event):
        self.settled.append((reservation, event))

    def cancel(self, reservation):
        self.cancelled.append(reservation)


def _provider(name="deepseek-v4-pro", max_tokens=8192):
    return ProviderConfig(
        name=name,
        base_url="https://api.deepseek.com",
        api_key_ref="env:DEEPSEEK_API_KEY",
        model=name,
        family=name,
        max_tokens=max_tokens,
        temperature=0,
    )


def _context():
    return LLMContext(
        company_id="company-1",
        harness_id="harness-1",
        employee_id="employee-1",
        job_id="job-1",
        sub_role="critic",
    )


def test_policy_cost_calculator_uses_uncached_input_and_output_prices():
    calculator = PolicyCostCalculator(load_policy())

    cost = calculator(_provider(), TokenUsage(2, 3, 5))

    assert cost == Decimal("0.00001452")


def test_budget_guard_reserves_upper_cost_and_tokens_from_policy():
    policy = load_policy()
    ledger = RecordingLedger()
    guard = PolicyBudgetGuard(policy=policy, ledger=ledger)

    reservation = guard.check(
        _context(),
        _provider(),
        ChatRequest(messages=[{"role": "user", "content": "hello"}]),
    )

    assert reservation == ledger.reservations[0][0]
    _, context, estimated_cost, estimated_tokens, limits = ledger.reservations[0]
    assert context == _context()
    assert estimated_cost == Decimal("0.03244032")
    assert estimated_tokens == 8192
    assert limits == policy["limits"]


def test_unpriced_provider_is_rejected_before_budget_reservation():
    ledger = RecordingLedger()
    guard = PolicyBudgetGuard(policy=load_policy(), ledger=ledger)

    with pytest.raises(BudgetExceededError, match="no price"):
        guard.check(
            _context(),
            _provider(name="unpriced-model"),
            ChatRequest(messages=[{"role": "user", "content": "hello"}]),
        )

    assert ledger.reservations == []


@pytest.mark.parametrize(
    ("limit_name", "spent_key", "spent_value"),
    [
        ("budget_usd_per_day.harness", "harness_daily_cost", Decimal("0.99")),
        ("budget_usd_per_day.employee", "employee_daily_cost", Decimal("0.49")),
        ("tokens_per_day.harness", "harness_daily_tokens", 1_495_000),
        ("tokens_per_day.employee", "employee_daily_tokens", 745_000),
    ],
)
def test_preflight_checks_each_daily_budget_and_token_limit(
    limit_name, spent_key, spent_value
):
    policy = load_policy()
    spent = {
        "installation_cost": Decimal("0"),
        "harness_daily_cost": Decimal("0"),
        "employee_daily_cost": Decimal("0"),
        "harness_daily_tokens": 0,
        "employee_daily_tokens": 0,
    }
    spent[spent_key] = spent_value

    with pytest.raises(BudgetExceededError) as error:
        PostgresBudgetLedger._check_limits(
            policy["limits"],
            spent,
            Decimal("0.03244032"),
            8192,
        )

    assert error.value.limit_name == limit_name


def test_gt_c2_09_installation_budget_blocks_provider_call_at_4_99_spend():
    policy = load_policy()
    ledger = RecordingLedger(installation_spend=Decimal("4.99"))
    budget = PolicyBudgetGuard(policy=policy, ledger=ledger)
    calls = []
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: calls.append(request)
            or httpx.Response(
                200,
                json={
                    "model": "deepseek-v4-pro",
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            )
        )
    )
    connector = LLMConnector(
        providers={
            "deepseek-flash": _provider("deepseek-flash", 8192),
            "deepseek-v4-pro": _provider(),
        },
        routes={
            "analyst": "deepseek-flash",
            "developer": "deepseek-flash",
            "critic": "deepseek-v4-pro",
            "researcher": "deepseek-v4-pro",
        },
        secret_store=EnvSecretStore({"DEEPSEEK_API_KEY": "test-key"}),
        http_client=client,
        budget_guard=budget,
        cost_calculator=PolicyCostCalculator(policy),
        usage_recorder=lambda event: None,
        dlp_rules={"pii": "mask"},
        dlp_enabled=True,
    )

    try:
        with pytest.raises(BudgetExceededError) as error:
            connector.chat_completions(
                _context(),
                ChatRequest(messages=[{"role": "user", "content": "hello"}]),
            )
    finally:
        client.close()

    assert error.value.limit_name == "budget_usd_total.installation"
    assert error.value.remaining == Decimal("0.01")
    assert calls == []


def test_successful_connector_settles_actual_cost_to_budget_ledger():
    policy = load_policy()
    ledger = RecordingLedger()
    budget = PolicyBudgetGuard(policy=policy, ledger=ledger)
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "model": "deepseek-v4-pro",
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                    },
                },
            )
        )
    )
    provider_configs = {
        "deepseek-flash": _provider("deepseek-flash"),
        "deepseek-v4-pro": _provider(),
    }
    connector = LLMConnector(
        providers=provider_configs,
        routes={
            "analyst": "deepseek-flash",
            "developer": "deepseek-flash",
            "critic": "deepseek-v4-pro",
            "researcher": "deepseek-flash",
        },
        secret_store=EnvSecretStore({"DEEPSEEK_API_KEY": "test-key"}),
        http_client=client,
        budget_guard=budget,
        cost_calculator=PolicyCostCalculator(policy),
        usage_recorder=lambda event: None,
        dlp_rules={"pii": "mask"},
        dlp_enabled=True,
    )

    try:
        result = connector.chat_completions(
            _context(), ChatRequest(messages=[{"role": "user", "content": "hello"}])
        )
    finally:
        client.close()

    assert len(ledger.settled) == 1
    assert ledger.settled[0] == (ledger.reservations[0][0], result.event)
    assert result.event.cost == Decimal("0.00001452")
