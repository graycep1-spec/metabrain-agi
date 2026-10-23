from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection

from src.llm.connector import BudgetExceededError, LLMContext, UsageEvent


class PostgresBudgetLedger:
    _LOCK_ID = 4_971_119

    def __init__(self, connection: Connection):
        self._connection = connection

    def reserve(
        self,
        context: LLMContext,
        estimated_cost: Decimal,
        estimated_tokens: int,
        limits: Mapping[str, Any],
    ) -> UUID:
        reservation_id = uuid4()
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        with self._connection.transaction():
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)", (self._LOCK_ID,)
            )
            self._connection.execute(
                "DELETE FROM llm_budget_reservations WHERE expires_at <= clock_timestamp()"
            )
            spent = self._spent(context, day_start)
            self._check_limits(limits, spent, estimated_cost, estimated_tokens)
            self._connection.execute(
                """
                INSERT INTO llm_budget_reservations
                    (id, harness_id, employee_id, estimated_cost_usd, estimated_tokens)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    reservation_id,
                    context.harness_id,
                    context.employee_id,
                    estimated_cost,
                    estimated_tokens,
                ),
            )
        return reservation_id

    def settle(self, reservation: UUID, event: UsageEvent) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "SELECT pg_advisory_xact_lock(%s::bigint)", (self._LOCK_ID,)
            )
            reserved = self._connection.execute(
                "DELETE FROM llm_budget_reservations WHERE id = %s RETURNING id",
                (reservation,),
            ).fetchone()
            if reserved is None:
                raise RuntimeError("LLM budget reservation is missing or expired")
            self._connection.execute(
                """
                INSERT INTO llm_usage
                    (id, harness_id, employee_id, provider, model, prompt_tokens,
                     completion_tokens, total_tokens, cost_usd)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    uuid4(),
                    event.context.harness_id,
                    event.context.employee_id,
                    event.provider,
                    event.model,
                    event.usage.prompt_tokens,
                    event.usage.completion_tokens,
                    event.usage.prompt_tokens + event.usage.completion_tokens,
                    event.cost,
                ),
            )

    def cancel(self, reservation: UUID) -> None:
        with self._connection.transaction():
            self._connection.execute(
                "DELETE FROM llm_budget_reservations WHERE id = %s", (reservation,)
            )

    def _spent(self, context: LLMContext, day_start: datetime) -> dict[str, Decimal | int]:
        row = self._connection.execute(
            """
            SELECT
                (SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage),
                (SELECT COALESCE(SUM(estimated_cost_usd), 0)
                   FROM llm_budget_reservations),
                (SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage
                  WHERE created_at >= %s AND harness_id = %s),
                (SELECT COALESCE(SUM(estimated_cost_usd), 0)
                   FROM llm_budget_reservations
                  WHERE harness_id = %s),
                (SELECT COALESCE(SUM(cost_usd), 0) FROM llm_usage
                  WHERE created_at >= %s AND harness_id = %s AND employee_id = %s),
                (SELECT COALESCE(SUM(estimated_cost_usd), 0)
                   FROM llm_budget_reservations
                  WHERE harness_id = %s AND employee_id = %s),
                (SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage
                  WHERE created_at >= %s AND harness_id = %s),
                (SELECT COALESCE(SUM(estimated_tokens), 0)
                   FROM llm_budget_reservations
                  WHERE harness_id = %s),
                (SELECT COALESCE(SUM(total_tokens), 0) FROM llm_usage
                  WHERE created_at >= %s AND harness_id = %s AND employee_id = %s),
                (SELECT COALESCE(SUM(estimated_tokens), 0)
                   FROM llm_budget_reservations
                  WHERE harness_id = %s AND employee_id = %s)
            """,
            (
                day_start,
                context.harness_id,
                context.harness_id,
                day_start,
                context.harness_id,
                context.employee_id,
                context.harness_id,
                context.employee_id,
                day_start,
                context.harness_id,
                context.harness_id,
                day_start,
                context.harness_id,
                context.employee_id,
                context.harness_id,
                context.employee_id,
            ),
        ).fetchone()
        return {
            "installation_cost": Decimal(row[0]) + Decimal(row[1]),
            "harness_daily_cost": Decimal(row[2]) + Decimal(row[3]),
            "employee_daily_cost": Decimal(row[4]) + Decimal(row[5]),
            "harness_daily_tokens": row[6] + row[7],
            "employee_daily_tokens": row[8] + row[9],
        }

    @staticmethod
    def _check_limits(
        limits: Mapping[str, Any],
        spent: Mapping[str, Decimal | int],
        estimated_cost: Decimal,
        estimated_tokens: int,
    ) -> None:
        cost_limits = limits["budget_usd_total"]
        daily_cost_limits = limits["budget_usd_per_day"]
        token_limits = limits["tokens_per_day"]
        checks = (
            (
                "budget_usd_total.installation",
                cost_limits["installation"],
                spent["installation_cost"],
                estimated_cost,
            ),
            (
                "budget_usd_per_day.harness",
                daily_cost_limits["harness"],
                spent["harness_daily_cost"],
                estimated_cost,
            ),
            (
                "budget_usd_per_day.employee",
                daily_cost_limits["employee"],
                spent["employee_daily_cost"],
                estimated_cost,
            ),
            (
                "tokens_per_day.harness",
                token_limits["harness"],
                spent["harness_daily_tokens"],
                estimated_tokens,
            ),
            (
                "tokens_per_day.employee",
                token_limits["employee"],
                spent["employee_daily_tokens"],
                estimated_tokens,
            ),
        )
        for name, raw_limit, raw_spent, requested in checks:
            limit = _configured_limit(raw_limit, name)
            if limit is None:
                continue
            used = Decimal(raw_spent)
            estimate = Decimal(requested)
            remaining = limit - used
            if estimate > remaining:
                raise BudgetExceededError(
                    f"{name}: limit {limit}; remaining {remaining}; requested {estimate}",
                    limit=limit,
                    limit_name=name,
                    remaining=remaining,
                )


def _configured_limit(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        limit = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"invalid policy limit {name}") from error
    if not limit.is_finite() or limit <= 0:
        raise ValueError(f"invalid policy limit {name}")
    return limit
