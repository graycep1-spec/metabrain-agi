"""Policy-as-code (C-5, ADR-018): версионируемый файл, одна политика на установку."""

from collections.abc import Mapping
import copy
import json
from pathlib import Path
from typing import Any


DEFAULT_POLICY_PATH = Path(__file__).with_name("default.json")
SUB_ROLES = ("analyst", "developer", "critic", "researcher", "debugger")
LEVEL_KEYS = ("L0_to_L1", "L1_to_L2")
MCP_TOOLS = ("skills_resolve", "retrieval_search", "policy_get")


class PolicyError(ValueError):
    pass


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    validate_policy(policy)
    return policy


def validate_policy(policy: Mapping[str, Any]) -> None:
    """Инварианты, которые нельзя нарушить правкой файла."""
    try:
        routing = policy["llm_routing"]
        routes, providers = routing["routes"], routing["providers"]
        missing = [role for role in SUB_ROLES if role not in routes]
        if missing:
            raise PolicyError(f"llm_routing.routes misses {missing}")
        unknown = sorted(set(routes.values()) - set(providers))
        if unknown:
            raise PolicyError(f"llm_routing.routes reference unknown providers {unknown}")
        if providers[routes["critic"]]["family"] == providers[routes["developer"]]["family"]:
            raise PolicyError("critic must use another model family than developer (C-2)")
        prices = policy["pricing"]["per_million_tokens"]
        unpriced = sorted(set(routes.values()) - set(prices))
        if unpriced:
            raise PolicyError(f"pricing misses routed providers {unpriced}: cost cannot be counted")
        for name, price in prices.items():
            for field in ("input_cache_hit", "input_cache_miss", "output"):
                if not _non_negative(price[field]):
                    raise PolicyError(f"pricing.{name}.{field} must be a non-negative number")
        _validate_limits(policy["limits"])
        if set(policy["dlp_rules"].values()) - {"mask", "drop"}:
            raise PolicyError("dlp_rules actions must be mask or drop")
        thresholds = policy["autonomy"]["promotion_thresholds"]
        if set(thresholds) != set(LEVEL_KEYS):
            raise PolicyError("autonomy.promotion_thresholds must define L0_to_L1 and L1_to_L2")
        if not 0 <= float(policy["grounding_threshold"]) <= 1:
            raise PolicyError("grounding_threshold must be within [0, 1]")
        for client_id, tools in policy["mcp_scopes"].items():
            unknown_tools = sorted(set(tools) - set(MCP_TOOLS))
            if unknown_tools:
                raise PolicyError(f"mcp_scopes.{client_id} has unknown tools {unknown_tools}")
        ttl = policy["web"]["session_ttl_hours"]
        if not _non_negative(ttl) or not 1 <= ttl <= 24 * 30:
            raise PolicyError("web.session_ttl_hours must be within 1..720")
        if not str(policy["version"]).strip():
            raise PolicyError("policy version is required")
    except (KeyError, TypeError, AttributeError) as error:
        raise PolicyError(f"malformed policy: {error}") from error


def _non_negative(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _cap(value: Any, name: str) -> float | None:
    """Лимит: положительное число или null (не задан)."""
    if value is None:
        return None
    if not _non_negative(value) or value == 0:
        raise PolicyError(f"limits.{name} must be a positive number or null")
    return float(value)


def _validate_limits(limits: Mapping[str, Any]) -> None:
    total = _cap(limits["budget_usd_total"]["installation"], "budget_usd_total.installation")
    harness = _cap(limits["budget_usd_per_day"]["harness"], "budget_usd_per_day.harness")
    employee = _cap(limits["budget_usd_per_day"]["employee"], "budget_usd_per_day.employee")
    for name in ("harness", "employee"):
        _cap(limits["tokens_per_day"][name], f"tokens_per_day.{name}")
    _cap(limits["actions_per_hour"]["employee"], "actions_per_hour.employee")
    if harness is not None and employee is not None and employee > harness:
        raise PolicyError("limits: employee daily budget exceeds harness daily budget")
    if total is not None and harness is not None and harness > total:
        raise PolicyError("limits: harness daily budget exceeds installation total")


def resolve_policy(
    policy: Mapping[str, Any],
    *,
    employee_id: str | None = None,
    sub_role: str | None = None,
) -> dict[str, Any]:
    """Ответ `policy.get`: сужение L2-классов до ЦС, провайдер роли в поле `llm`."""
    resolved = copy.deepcopy(dict(policy))
    if employee_id is not None:
        allowed = policy["autonomy"]["allowed_l2_classes"].get(employee_id, [])
        resolved["autonomy"]["allowed_l2_classes"] = {employee_id: list(allowed)}
    if sub_role is not None:
        if sub_role not in SUB_ROLES:
            raise PolicyError(f"unknown sub_role {sub_role!r}")
        name = policy["llm_routing"]["routes"][sub_role]
        resolved["llm"] = {"provider": name, **policy["llm_routing"]["providers"][name]}
    return resolved
