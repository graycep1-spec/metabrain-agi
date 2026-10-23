from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
import json
import re
import time
from typing import Any, Literal, Protocol
from uuid import UUID

import httpx

from src.common.secrets import EnvSecretStore


SubRole = Literal["analyst", "developer", "critic", "researcher"]
JsonObject = dict[str, Any]
FACT_PLACEHOLDER = re.compile(r"\{\{fact:([^{}.]+)\.([^{}]+)\}\}")


class RoutingError(ValueError):
    pass


class BudgetExceededError(RuntimeError):
    code = "budget_exceeded"

    def __init__(
        self,
        message: str,
        *,
        limit: Decimal | None = None,
        limit_name: str | None = None,
        remaining: Decimal | None = None,
    ):
        super().__init__(message)
        self.limit = limit
        self.limit_name = limit_name
        self.remaining = remaining


class ProviderResponseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    base_url: str
    api_key_ref: str | None
    model: str
    family: str
    max_tokens: int
    temperature: float
    external: bool = True

    def __post_init__(self):
        if not self.name.strip():
            raise ValueError("provider name is required")
        if not self.model.strip():
            raise ValueError("provider model is required")
        if not self.family.strip():
            raise ValueError("provider family is required")
        if self.max_tokens < 1:
            raise ValueError("provider max_tokens must be positive")
        try:
            parsed = httpx.URL(self.base_url)
        except (TypeError, httpx.InvalidURL) as error:
            raise ValueError("provider base_url must be an absolute HTTP(S) URL") from error
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise ValueError("provider base_url must be an absolute HTTP(S) URL")


@dataclass(frozen=True)
class LLMContext:
    company_id: str
    harness_id: str
    employee_id: str
    job_id: str
    sub_role: SubRole

    def __post_init__(self):
        for name in ("company_id", "harness_id", "employee_id", "job_id"):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} is required")
        if self.sub_role not in {"analyst", "developer", "critic", "researcher", "debugger"}:
            raise ValueError("unsupported LLM sub_role")


@dataclass(frozen=True)
class ChatRequest:
    messages: Sequence[Mapping[str, Any]]
    facts: Sequence[Mapping[str, Any]] = ()
    field_sensitivity: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class UsageEvent:
    context: LLMContext
    provider: str
    model: str
    usage: TokenUsage
    cost: Decimal
    latency_ms: float


@dataclass(frozen=True)
class LLMResult:
    response: JsonObject
    event: UsageEvent


class BudgetGuard(Protocol):
    def check(
        self, context: LLMContext, provider: ProviderConfig, request: Any
    ) -> UUID | None: ...

    def settle(self, reservation: UUID | None, event: UsageEvent) -> None: ...

    def cancel(self, reservation: UUID | None) -> None: ...


class LLMConnector:
    def __init__(
        self,
        *,
        providers: Mapping[str, ProviderConfig],
        routes: Mapping[str, str],
        secret_store: EnvSecretStore,
        http_client: httpx.Client,
        budget_guard: BudgetGuard,
        cost_calculator: Callable[[ProviderConfig, TokenUsage], Decimal],
        usage_recorder: Callable[[UsageEvent], None],
        dlp_rules: Mapping[str, Literal["mask", "drop"]],
        dlp_enabled: bool,
    ):
        self._providers = dict(providers)
        self._routes = dict(routes)
        self._secret_store = secret_store
        self._http_client = http_client
        self._budget_guard = budget_guard
        self._cost_calculator = cost_calculator
        self._usage_recorder = usage_recorder
        self._dlp_rules = dict(dlp_rules)
        self._dlp_enabled = dlp_enabled

        if set(self._dlp_rules.values()) - {"mask", "drop"}:
            raise ValueError("DLP actions must be mask or drop")
        unknown = set(self._routes.values()) - self._providers.keys()
        if unknown:
            raise RoutingError(f"routes reference unknown providers: {sorted(unknown)}")
        for role in ("analyst", "developer", "critic", "researcher"):
            if role not in self._routes:
                raise RoutingError(f"missing provider route for {role}")
        if not self._dlp_enabled and any(
            self.provider_for(role).external
            for role in ("analyst", "developer", "critic", "researcher")
        ):
            raise ValueError("DLP cannot be disabled when an external provider is routed")
        developer = self.provider_for("developer")
        critic = self.provider_for("critic")
        if developer.family == critic.family:
            raise RoutingError("critic and developer must use different model families")

    def provider_for(self, sub_role: str) -> ProviderConfig:
        try:
            provider_name = self._routes[sub_role]
        except KeyError as error:
            raise RoutingError(f"unsupported LLM sub_role: {sub_role}") from error
        try:
            return self._providers[provider_name]
        except KeyError as error:
            raise RoutingError(f"unknown LLM provider: {provider_name}") from error

    def chat_completions(self, context: LLMContext, request: ChatRequest) -> LLMResult:
        provider = self.provider_for(context.sub_role)
        if not isinstance(request.field_sensitivity, Mapping):
            raise ValueError("field_sensitivity must be an object")
        facts = self._prepare_facts(request.facts)
        messages = deepcopy(list(request.messages))
        if self._dlp_enabled:
            messages = self._apply_dlp(messages, request.field_sensitivity)
        messages = self._resolve_fact_placeholders(messages, facts)
        payload = {
            "model": provider.model,
            "messages": messages,
            "max_tokens": provider.max_tokens,
            "temperature": provider.temperature,
        }
        return self._send(context, provider, request, "chat/completions", payload)

    def embeddings(
        self, context: LLMContext, request: Mapping[str, Any]
    ) -> LLMResult:
        provider = self.provider_for(context.sub_role)
        payload = deepcopy(dict(request))
        payload["model"] = provider.model
        sensitivity = payload.pop("field_sensitivity", {})
        if self._dlp_enabled:
            if not isinstance(sensitivity, Mapping):
                raise ValueError("field_sensitivity must be an object")
            payload = self._apply_dlp(payload, sensitivity)
        return self._send(context, provider, request, "embeddings", payload)

    def _send(
        self,
        context: LLMContext,
        provider: ProviderConfig,
        original_request: Any,
        endpoint: str,
        payload: JsonObject,
    ) -> LLMResult:
        reservation = self._budget_guard.check(context, provider, original_request)
        headers = {}
        if provider.api_key_ref is not None:
            headers["Authorization"] = f"Bearer {self._secret_store.resolve(provider.api_key_ref)}"

        request_dispatched = False
        try:
            started = time.perf_counter()
            request_dispatched = True
            response = self._http_client.post(
                f"{provider.base_url.rstrip('/')}/{endpoint}",
                json=payload,
                headers=headers,
            )
            latency_ms = (time.perf_counter() - started) * 1000
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ProviderResponseError("LLM provider response must be a JSON object")
            usage = self._parse_usage(body.get("usage"))
            cost = self._cost_calculator(provider, usage)
            event = UsageEvent(
                context=context,
                provider=provider.name,
                model=str(body.get("model", provider.model)),
                usage=usage,
                cost=cost,
                latency_ms=latency_ms,
            )
            self._budget_guard.settle(reservation, event)
            self._usage_recorder(event)
            return LLMResult(response=body, event=event)
        finally:
            if not request_dispatched:
                self._budget_guard.cancel(reservation)

    def _parse_usage(self, value: Any) -> TokenUsage:
        if not isinstance(value, Mapping):
            raise ProviderResponseError("LLM provider response is missing usage data")
        prompt = value.get("prompt_tokens", 0)
        completion = value.get("completion_tokens", 0)
        if any(
            type(token_count) is not int or token_count < 0
            for token_count in (prompt, completion)
        ):
            raise ProviderResponseError("LLM provider returned invalid token usage")
        total = value.get("total_tokens", prompt + completion)
        if type(total) is not int or total < 0:
            raise ProviderResponseError("LLM provider returned invalid token usage")
        return TokenUsage(prompt, completion, total)

    def _apply_dlp(self, value: Any, sensitivity: Mapping[str, str]) -> Any:
        if isinstance(value, list):
            return [self._apply_dlp(item, sensitivity) for item in value]
        if isinstance(value, tuple):
            return [self._apply_dlp(item, sensitivity) for item in value]
        if not isinstance(value, Mapping):
            return value

        filtered = {}
        for key, item in value.items():
            label = sensitivity.get(str(key))
            if label is None:
                filtered[key] = self._apply_dlp(item, sensitivity)
                continue
            try:
                action = self._dlp_rules[label]
            except KeyError as error:
                raise ValueError(f"no DLP action configured for sensitivity {label}") from error
            if action == "mask":
                filtered[key] = f"[REDACTED:{label}]"
            elif action != "drop":
                raise ValueError(f"unsupported DLP action for sensitivity {label}")
        return filtered

    def _prepare_facts(
        self, facts: Sequence[Mapping[str, Any]]
    ) -> dict[str, tuple[Mapping[str, Any], set[str]]]:
        if not isinstance(facts, Sequence) or isinstance(facts, (str, bytes)):
            raise ValueError("facts must be an array")
        prepared = {}
        for fact in facts:
            if not isinstance(fact, Mapping):
                raise ValueError("fact must be an object")
            ref = fact.get("ref")
            data = fact.get("data")
            sensitivity = fact.get("sensitivity", {})
            if not isinstance(ref, str) or not ref.strip():
                raise ValueError("fact ref must be a non-empty string")
            if ref in prepared:
                raise ValueError(f"duplicate fact ref {ref!r}")
            if not isinstance(data, Mapping):
                raise ValueError(f"fact {ref!r} data must be an object")
            if not isinstance(sensitivity, Mapping) or any(
                not isinstance(key, str) or not isinstance(label, str)
                for key, label in sensitivity.items()
            ):
                raise ValueError(f"fact {ref!r} sensitivity must be an object")
            dropped = {
                key
                for key, label in sensitivity.items()
                if self._dlp_enabled
                and self._dlp_rules.get(label) == "drop"
                and key in data
            }
            filtered = (
                self._apply_dlp(data, sensitivity)
                if self._dlp_enabled
                else deepcopy(dict(data))
            )
            prepared[ref] = (filtered, dropped)
        return prepared

    def _resolve_fact_placeholders(
        self, value: Any, facts: Mapping[str, tuple[Mapping[str, Any], set[str]]]
    ) -> Any:
        if isinstance(value, list):
            return [self._resolve_fact_placeholders(item, facts) for item in value]
        if isinstance(value, tuple):
            return [self._resolve_fact_placeholders(item, facts) for item in value]
        if isinstance(value, Mapping):
            return {
                key: self._resolve_fact_placeholders(item, facts)
                for key, item in value.items()
            }
        if not isinstance(value, str):
            return value

        def replace(match: re.Match[str]) -> str:
            ref, field_name = match.groups()
            if ref not in facts:
                raise ValueError(f"placeholder refers to unknown fact {ref!r}")
            data, dropped = facts[ref]
            if field_name not in data:
                if field_name in dropped:
                    return ""
                raise ValueError(
                    f"placeholder refers to unknown field {field_name!r} in fact {ref!r}"
                )
            field_value = data[field_name]
            if isinstance(field_value, str):
                return field_value
            return json.dumps(field_value, ensure_ascii=False, separators=(",", ":"))

        resolved = FACT_PLACEHOLDER.sub(replace, value)
        if "{{fact:" in resolved:
            raise ValueError("malformed fact placeholder")
        return resolved
