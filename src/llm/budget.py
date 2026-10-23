from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from uuid import UUID

from src.llm.connector import (
    BudgetExceededError,
    LLMContext,
    ProviderConfig,
    TokenUsage,
    UsageEvent,
)


class BudgetLedger(Protocol):
    def reserve(
        self,
        context: LLMContext,
        estimated_cost: Decimal,
        estimated_tokens: int,
        limits: Mapping[str, Any],
    ) -> UUID: ...

    def settle(self, reservation: UUID, event: UsageEvent) -> None: ...

    def cancel(self, reservation: UUID) -> None: ...


class PolicyCostCalculator:
    def __init__(self, policy: Mapping[str, Any]):
        try:
            self._prices = policy["pricing"]["per_million_tokens"]
        except (KeyError, TypeError) as error:
            raise ValueError("policy.pricing.per_million_tokens is required") from error

    def __call__(self, provider: ProviderConfig, usage: TokenUsage) -> Decimal:
        price = self._price_for(provider)
        return (
            Decimal(usage.prompt_tokens) * price["input_cache_miss"]
            + Decimal(usage.completion_tokens) * price["output"]
        ) / Decimal(1_000_000)

    def estimate(self, provider: ProviderConfig) -> tuple[Decimal, int]:
        price = self._price_for(provider)
        cost = Decimal(provider.max_tokens) * price["output"] / Decimal(1_000_000)
        return cost, provider.max_tokens

    def _price_for(self, provider: ProviderConfig) -> dict[str, Decimal]:
        try:
            raw_price = self._prices[provider.name]
            return {
                name: _decimal(raw_price[name])
                for name in ("input_cache_hit", "input_cache_miss", "output")
            }
        except KeyError as error:
            raise BudgetExceededError(
                f"no price configured for provider {provider.name}",
                limit_name=f"pricing.{provider.name}",
            ) from error
        except (TypeError, InvalidOperation) as error:
            raise ValueError(f"invalid pricing for provider {provider.name}") from error


class PolicyBudgetGuard:
    def __init__(self, *, policy: Mapping[str, Any], ledger: BudgetLedger):
        try:
            limits = policy["limits"]
        except (KeyError, TypeError) as error:
            raise ValueError("policy.limits is required") from error
        if not isinstance(limits, Mapping):
            raise ValueError("policy.limits must be an object")
        self._limits = limits
        self._ledger = ledger
        self._cost_calculator = PolicyCostCalculator(policy)

    def check(
        self, context: LLMContext, provider: ProviderConfig, request: Any
    ) -> UUID:
        estimated_cost, estimated_tokens = self._cost_calculator.estimate(provider)
        return self._ledger.reserve(
            context, estimated_cost, estimated_tokens, self._limits
        )

    def settle(self, reservation: UUID | None, event: UsageEvent) -> None:
        if reservation is None:
            raise ValueError("budget reservation is required to settle usage")
        self._ledger.settle(reservation, event)

    def cancel(self, reservation: UUID | None) -> None:
        if reservation is not None:
            self._ledger.cancel(reservation)


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise InvalidOperation
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise InvalidOperation
    return result
