"""Сборка сервисов T-13/T-19/T-23 для процесса харнесса: LLM-коннектор из политики, валидатор, реестр систем, мета-мозг."""

from collections.abc import Mapping
from datetime import timedelta
from functools import partial
import os
from typing import Any

import httpx
import psycopg
from psycopg import Connection

from src.common.config import Settings
from src.common.secrets import EnvSecretStore
from src.harness.journal import JournalStore
from src.harness.llm_settings import overlay_settings
from src.harness.llm_usage import PostgresBudgetLedger
from src.harness.proposals import ProposalStore
from src.harness.store import HarnessStore
from src.harness.vault import SecretStore, build_vault
from src.landscape.registry import parse_registry
from src.landscape.service import ResearcherService
from src.metabrain.client import HttpMetabrainClient
from src.llm.budget import PolicyBudgetGuard, PolicyCostCalculator
from src.llm.connector import LLMConnector, ProviderConfig
from src.metabrain.policy import load_policy
from src.validator.client import HttpValidatorClient
from src.workforce.passport import LLMPassportDrafter
from src.workforce.reports import HarnessReportSink
from src.workforce.runtime import Runtime, RuntimeThread
from src.workforce.service import WorkforceService
from src.workforce.improvement import ImprovementFlow
from src.workforce.skills import SkillFlow
from src.workforce.swarm import SwarmHandler, visible_skills


def build_connector(policy: Mapping[str, Any], settings: Settings, connection: Connection,
                    secret_store: Any, http_client: httpx.Client | None = None) -> LLMConnector:
    """Маршруты, семейства, цены, лимиты и DLP — из политики (ADR-021); адрес, ключ и имена
    моделей — из конфига (env + кабинет, ADR-033). `secret_store` должен понимать vault:-ссылки."""
    judge_provider = policy["llm_routing"]["routes"].get("critic")

    def model_for(name: str, spec: Mapping[str, Any]) -> str:
        # Имя модели, которое уходит провайдеру: судья (critic) — llm_model_judge, прочие — llm_model_main.
        # Семейство (critic != developer) и цены по-прежнему берутся из политики по имени провайдера.
        if name == judge_provider and settings.llm_model_judge:
            return settings.llm_model_judge
        if name != judge_provider and settings.llm_model_main:
            return settings.llm_model_main
        return spec["model"]

    providers = {
        name: ProviderConfig(
            name=name, base_url=settings.llm_api_base, api_key_ref=settings.llm_api_key_ref,
            model=model_for(name, spec), family=spec["family"], max_tokens=int(spec["max_tokens"]),
            temperature=float(spec["temperature"]), external=settings.llm_provider != "local",
        )
        for name, spec in policy["llm_routing"]["providers"].items()
    }
    return LLMConnector(
        providers=providers,
        routes=policy["llm_routing"]["routes"],
        secret_store=secret_store,
        http_client=http_client or httpx.Client(timeout=120),
        budget_guard=PolicyBudgetGuard(policy=policy, ledger=PostgresBudgetLedger(connection)),
        cost_calculator=PolicyCostCalculator(policy),
        usage_recorder=lambda event: None,  # расход пишет PostgresBudgetLedger.settle в llm_usage
        dlp_rules=policy["dlp_rules"],
        dlp_enabled=settings.dlp_enabled,
    )


def build_extensions(connection: Connection, settings: Settings, secret_store: EnvSecretStore,
                     environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """`workforce` и `researcher` для `create_app`; без VALIDATOR_URL — не подключаются (поведение T-35)."""
    source = os.environ if environ is None else environ
    validator_url = source.get("VALIDATOR_URL", "").strip()
    if not validator_url:
        return {}
    policy = load_policy()
    # Настройки LLM из кабинета (ADR-033) поверх env; применяются при старте процесса.
    settings = overlay_settings(connection, settings)
    validator = HttpValidatorClient(validator_url)
    proposals = ProposalStore(
        connection,
        secret_store=secret_store,
        signing_key_ref=settings.consent_signing_key_ref,
        verification_key_ref=settings.consent_public_key_ref,
        token_ttl=timedelta(seconds=settings.consent_token_ttl_seconds),
    )
    journal = JournalStore(connection)
    vault = build_vault(connection, secret_store)
    # Коннектор должен уметь ключ vault:<id> из кабинета, не только env: — поэтому vault-aware store.
    llm_secrets = SecretStore(secret_store, vault)
    holder: dict[str, ResearcherService] = {}
    workforce = WorkforceService(
        connection, proposals=proposals, journal=journal,
        drafter=LLMPassportDrafter(build_connector(policy, settings, connection, llm_secrets)),
        validator=validator, tools=lambda harness_id: holder["researcher"].tools(harness_id),
    )
    researcher = ResearcherService(
        connection, harness=HarnessStore(connection), proposals=proposals, journal=journal, validator=validator,
        registry=parse_registry(source.get("LANDSCAPE_SYSTEMS")), secret_store=SecretStore(secret_store, vault),
        http_client=httpx.Client(timeout=30),
        on_new_system=lambda identity, system: workforce.propose_tool_extension(identity, system=system),
    )
    holder["researcher"] = researcher
    # T-37: отладчик читает телеметрию и предлагает улучшения через гейт (ADR-029).
    extensions_improvement = ImprovementFlow(
        connection, connector=build_connector(policy, settings, connection, llm_secrets),
        workforce=workforce, journal=journal,
    )
    metabrain = build_metabrain(source)
    swarm = build_swarm(connection, settings, llm_secrets, policy=policy, validator=validator,
                        proposals=proposals, journal=journal, researcher=researcher, metabrain=metabrain)
    # `ask` (T-14): отчёт — в журнал; ответ вернёт сам вызов, поэтому в Telegram отдельно не шлём.
    ask_runtime = Runtime(connection, handler=swarm, sink=HarnessReportSink(connection, send=None))
    extensions = {"workforce": workforce, "researcher": researcher, "ask_runtime": ask_runtime, "vault": vault,
                  "improvement": extensions_improvement}
    if metabrain is not None:
        extensions["skills"] = SkillFlow(
            connection, connector=build_connector(policy, settings, connection, llm_secrets), metabrain=metabrain,
            proposals=proposals, journal=journal,
        )
    return extensions


def build_metabrain(source: Mapping[str, str]) -> HttpMetabrainClient | None:
    """Мета-мозг (T-23): без METABRAIN_URL навыков нет — рой работает без них, рекомендации не становятся навыками."""
    url = source.get("METABRAIN_URL", "").strip()
    return HttpMetabrainClient(url) if url else None


def build_swarm(connection: Connection, settings: Settings, secret_store: EnvSecretStore, *, policy, validator,
                proposals: ProposalStore, journal: JournalStore, researcher: ResearcherService,
                metabrain: HttpMetabrainClient | None = None) -> SwarmHandler:
    return SwarmHandler(
        connection, connector=build_connector(policy, settings, connection, secret_store), validator=validator,
        collect=researcher.collect_facts, proposals=proposals, journal=journal,
        skills=partial(visible_skills, metabrain) if metabrain is not None else None,
    )


def build_runtime_thread(database_url: str, settings: Settings, secret_store: EnvSecretStore,
                         send=None, environ: Mapping[str, str] | None = None) -> RuntimeThread | None:
    """Фоновый runtime работ по расписанию (T-15 + T-14) на собственном подключении к БД."""
    source = os.environ if environ is None else environ
    validator_url = source.get("VALIDATOR_URL", "").strip()
    if not validator_url:
        return None
    # autocommit: запрос вне transaction() не оставляет неявную транзакцию (idle in transaction),
    # а transaction() остаются настоящими транзакциями — шаги работы фиксируются сразу (C-6).
    connection = psycopg.connect(database_url, autocommit=True)
    policy = load_policy()
    settings = overlay_settings(connection, settings)   # настройки LLM из кабинета (ADR-033)
    validator = HttpValidatorClient(validator_url)
    proposals = ProposalStore(
        connection,
        secret_store=secret_store,
        signing_key_ref=settings.consent_signing_key_ref,
        verification_key_ref=settings.consent_public_key_ref,
        token_ttl=timedelta(seconds=settings.consent_token_ttl_seconds),
    )
    journal = JournalStore(connection)
    llm_secrets = SecretStore(secret_store, build_vault(connection, secret_store))
    researcher = ResearcherService(
        connection, harness=HarnessStore(connection), proposals=proposals, journal=journal, validator=validator,
        registry=parse_registry(source.get("LANDSCAPE_SYSTEMS")),
        secret_store=llm_secrets,
        http_client=httpx.Client(timeout=30),
    )
    swarm = build_swarm(connection, settings, llm_secrets, policy=policy, validator=validator,
                        proposals=proposals, journal=journal, researcher=researcher, metabrain=build_metabrain(source))
    return RuntimeThread(Runtime(connection, handler=swarm, sink=HarnessReportSink(connection, send=send)))


def build_web_auth(connection: Connection, secret_store: EnvSecretStore,
                   environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Вход в кабинет (T-31, ADR-025). Без Client ID/Secret/адреса кабинета вход отвечает 503."""
    from datetime import timedelta

    from src.common.secrets import SecretNotFoundError
    from src.harness.web_auth import OidcConfig, WebAuth

    source = os.environ if environ is None else environ
    client_id = source.get("TELEGRAM_OIDC_CLIENT_ID", "").strip()
    web_base = source.get("WEB_BASE_URL", "").strip().rstrip("/")
    oidc = None
    if client_id and web_base:
        try:
            secret = secret_store.resolve("env:TELEGRAM_OIDC_CLIENT_SECRET")
        except SecretNotFoundError:
            secret = None
        if secret:
            oidc = OidcConfig(client_id=client_id, client_secret=secret, redirect_uri=f"{web_base}/auth/callback")
    ttl = timedelta(hours=float(load_policy()["web"]["session_ttl_hours"]))
    return {"web_auth": WebAuth(connection, oidc=oidc, session_ttl=ttl)}
