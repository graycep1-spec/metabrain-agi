"""HTTP transport for the C-1 harness operations."""

from collections.abc import Callable, Collection, Mapping
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import logging
from pathlib import Path
import re
import secrets
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from psycopg import Connection, errors
from starlette.concurrency import run_in_threadpool

from src.common.config import Settings
from src.common.secrets import EnvSecretStore, SecretNotFoundError
from src.harness.journal import JournalStore
from src.harness.proposals import (
    ConsentError,
    ProposalConflict,
    ProposalStore,
)
from src.harness.store import (
    CompanyNotFoundError,
    HarnessAccessDenied,
    HarnessStore,
    Registration,
    RegistrationError,
)
from src.harness.web_auth import (
    READ_OPERATIONS,
    CsrfRequired,
    WebAuth,
    WebAuthError,
    WebAuthNotConfigured,
)
from src.harness.vault import SERVICE_SECRET, VAULT_REF, HarnessVault, SecretStore, VaultUnavailable
from src.harness.organization import INDUSTRIES, SIZES, OrgStore, role_context
from src.workforce import diagnosis
from src.workforce.catalog import listing as catalog_listing, process_template
from src.harness.llm_settings import (
    DEFAULT_MODEL_JUDGE,
    DEFAULT_MODEL_MAIN,
    LLMSettingsStore,
    default_api_base,
    env_key_ref,
)
from src.harness.llm_settings import validate as validate_llm_settings
from src.landscape.service import LandscapeUnavailable, ResearcherService
from src.llm.connector import BudgetExceededError
from src.workforce.runtime import Runtime
from src.workforce.planning import (
    PlanningConflict,
    PlanningInvalid,
    PlanningNotFound,
    PlanningService,
)
from src.workforce.skills import SkillFlow, SkillNotFound
from src.workforce.service import (
    EmployeeBusy,
    EmployeeNotFound,
    PassportConflict,
    WorkforceService,
    WorkforceUnavailable,
)


class ExternalLLMConsentRequired(PermissionError):
    pass


class InvalidInvite(ValueError):
    pass


class InvalidConnectRequest(ValueError):
    pass


class C1Service:
    def __init__(
        self,
        connection: Connection,
        *,
        app_base_url: str,
        telegram_bot_username: str,
        llm_provider: str,
        secret_store: EnvSecretStore,
        consent_signing_key_ref: str,
        consent_public_key_ref: str = "env:CONSENT_PUBLIC_KEY",
        consent_token_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] | None = None,
        workforce: WorkforceService | None = None,
        researcher: ResearcherService | None = None,
        web_auth: WebAuth | None = None,
        ask_runtime: Runtime | None = None,
        skills: SkillFlow | None = None,
        vault: HarnessVault | None = None,
        system_registry: Collection[str] | None = None,
        settings: Settings | None = None,
        llm_settings_editable: bool = False,
        llm_http_client: httpx.Client | None = None,
    ):
        self._connection = connection
        self._llm_http = llm_http_client   # для проверки подключения; в бою создаётся на лету
        # Хранилище секретов харнесса (ADR-028): доступ, введённый в кабинете, → ссылка vault:<id>.
        self._vault = vault
        # Навыки из работы (T-23): одобренная рекомендация → навык; согласие на публикацию — в мета-мозг.
        self._skills = skills
        # Рой ЦС для `ask` (T-14): работа runtime с триггером ask, исполняется в запросе.
        self._ask_runtime = ask_runtime
        # Вход в кабинет и сессии (T-31, ADR-025); без него операции принимают только channel_user_id.
        self._web_auth = web_auth
        # Цифровые сотрудники (T-13) и Исследователь (T-19); без них — поведение T-35.
        self._workforce = workforce
        self._researcher = researcher
        self._system_registry = (
            frozenset(system_registry) if system_registry is not None else None
        )
        self._base_url = app_base_url.rstrip("/")
        self._bot_username = telegram_bot_username.lstrip("@")
        self._llm_provider = llm_provider
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._harness = HarnessStore(connection)
        self._proposals = ProposalStore(
            connection,
            secret_store=secret_store,
            signing_key_ref=consent_signing_key_ref,
            verification_key_ref=consent_public_key_ref,
            token_ttl=consent_token_ttl,
        )
        self._journal = JournalStore(connection)
        self._planning = PlanningService(connection, journal=self._journal, clock=self._clock)
        # Настройки LLM из кабинета (ADR-033): чтение/запись строки + ключ через vault.
        self._settings = settings
        self._settings_editable = bool(llm_settings_editable)
        self._secret_store = SecretStore(secret_store, vault)
        self._llm_settings = LLMSettingsStore(connection)
        # Профиль организации (ADR-035): отрасль/размер/KPI, наследуется ролями.
        self._org = OrgStore(connection)
        schema = Path(__file__).with_name("api.sql").read_text(encoding="utf-8")
        with connection.transaction():
            connection.execute(schema, prepare=False)

    def call(self, operation: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        handlers = {
            "person.register": self.register_person,
            "company.invite": self.create_invite,
            "employee.draft": self.draft_employee,
            "employee.list": self.list_employees,
            "employee.get": self.get_employee,
            "employee.passport.propose": self.propose_passport,
            "employee.pause": self.pause_employee,
            "employee.autonomy": self.employee_autonomy,
            "employee.resume": self.resume_employee,
            "proposal.list": self.list_proposals,
            "proposal.decide": self.decide_proposal,
            "journal.list": self.list_journal,
            "ask": self.ask,
            "catalog.list": self.list_catalog,
            "org.get": self.get_org,
            "org.put": self.put_org,
            "employee.diagnose": self.diagnose_employee,
            "landscape.connect.start": self.start_connect,
            "landscape.connect": self.connect_landscape,
            "landscape.secret.put": self.put_secret,
            "skills.list": self.list_skills,
            "skill.share": self.share_skill,
            "process.list": self.list_processes,
            "process.get": self.get_process,
            "process.create": self.create_process,
            "project.list": self.list_projects,
            "project.get": self.get_project,
            "project.create": self.create_project,
            "assignment.create": self.create_assignment,
            "task.list": self.list_tasks,
            "task.create": self.create_task,
            "task.update": self.update_task,
            "planning.summary": self.planning_summary,
            "planning.seed_examples": self.planning_seed_examples,
            "landscape.map": self.get_landscape_map,
            "auth.web.start": self.web_start,
            "auth.web.callback": self.web_callback,
            "auth.web.bootstrap": self.web_bootstrap,
            "auth.web.logout": self.web_logout,
            "settings.llm.get": self.get_llm_settings,
            "settings.llm.put": self.put_llm_settings,
            "settings.llm.test": self.test_llm_settings,
        }
        handler = handlers.get(operation)
        if handler is None:
            raise NotImplementedError(f"unsupported C-1 operation: {operation}")
        payload = {k: v for k, v in payload.items() if k != "_session_identity"}
        if "session_token" in payload and not operation.startswith("auth.web."):
            # Операция из кабинета: идентичность — из сессии, для изменений нужен CSRF (C-1).
            identity, csrf_hash = self._require_web_auth().resolve(payload["session_token"])
            if operation not in READ_OPERATIONS:
                WebAuth.check_csrf(csrf_hash, payload.get("csrf_token"))
            payload["_session_identity"] = identity
        return handler(payload)

    def web_start(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, self._require_web_auth().start()

    def web_callback(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, self._require_web_auth().callback(
            code=self._required_text(payload, "code"), state=self._required_text(payload, "state"),
        )

    def web_bootstrap(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        result = self._require_web_auth().bootstrap(
            ticket=payload.get("ticket"),
            company_name=payload.get("company_name"),
            invite=payload.get("invite"),
        )
        if result.get("created") and not result.get("joined"):
            # Шаблон новой организации (ADR-030): примеры процессов и проектов сразу.
            # При joined=True орг уже существует — её настройки не трогаем.
            identity = Registration(person_id=UUID(result["person_id"]), company_id=UUID(result["company_id"]),
                                    harness_id=UUID(result["harness_id"]))
            try:
                if self._workforce is not None:
                    self._workforce.seed_twin(identity)   # пример-сотрудник (черновик на согласование)
                self._planning.seed_examples(identity)    # примеры процессов/проектов (+ назначения, если есть ЦС)
            except Exception:  # noqa: BLE001 — наполнение шаблона не должно ломать вход
                pass
        return 200, result

    def web_logout(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return 200, self._require_web_auth().logout(
            session_token=payload.get("session_token"), csrf_token=payload.get("csrf_token"),
        )

    def _require_web_auth(self) -> WebAuth:
        if self._web_auth is None:
            raise WebAuthNotConfigured("web login is not configured")
        return self._web_auth

    def register_person(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        if payload.get("external_llm_consent") is not True:
            raise ExternalLLMConsentRequired()

        channel = payload.get("channel")
        channel_user_id = self._required_text(payload, "channel_user_id")
        if channel != "telegram":
            raise RegistrationError("channel must be telegram")

        invite = payload.get("company_invite")
        company_name = payload.get("company_name")
        if invite is not None:
            if not isinstance(invite, str) or not invite.strip() or company_name is not None:
                raise InvalidInvite()
            company_id = self._company_for_invite(invite.strip())
            registration = self._harness.register_person(
                channel="telegram",
                channel_user_id=channel_user_id,
                trusted_company_id=company_id,
            )
            invite_link = None
        else:
            if not isinstance(company_name, str) or not company_name.strip():
                raise RegistrationError("company_name is required without an invite")
            registration = self._harness.register_person(
                channel="telegram",
                channel_user_id=channel_user_id,
                company_name=company_name,
            )
            invite_link, _ = self._issue_invite(
                registration.company_id,
                registration.person_id,
            )

        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO external_llm_consents
                    (person_id, provider, policy_version, consented_at)
                VALUES (%s, %s, 'C-004-v1', %s)
                """,
                (registration.person_id, self._llm_provider, self._clock()),
            )
            self._journal.append_entry(
                person_id=registration.person_id,
                company_id=registration.company_id,
                harness_id=registration.harness_id,
                kind="consent.external_llm",
                payload={
                    "provider": self._llm_provider,
                    "policy_version": "C-004-v1",
                    "consented": True,
                },
            )

        result: dict[str, Any] = {
            "person_id": str(registration.person_id),
            "company_id": str(registration.company_id),
            "harness_id": str(registration.harness_id),
            "consent": {
                "external_llm": True,
                "provider": self._llm_provider,
                "policy_version": "C-004-v1",
            },
        }
        if invite_link is not None:
            result["invite_link"] = invite_link
        return 201, result

    def create_invite(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._optional_uuid(payload, "harness_id") or identity.harness_id
        if harness_id != identity.harness_id:
            raise HarnessAccessDenied()
        invite_link, exp = self._issue_invite(
            identity.company_id,
            identity.person_id,
        )
        return 200, {"invite_link": invite_link, "exp": exp.isoformat()}

    def draft_employee(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        kind = payload.get("kind")
        if kind not in {"twin", "new"}:
            raise ValueError("kind must be twin or new")
        org = self._org.get(identity.company_id)
        template_id = self._optional_text(payload, "template_id")
        if template_id and self._workforce is not None:
            # Готовая роль из каталога: без LLM, но тем же конвейером (валидатор → гейт).
            result = self._workforce.draft_from_template(identity, template_id=template_id, org=org)
            return (422 if result.get("status") == "rejected" else 201), result
        role_text = self._required_text(payload, "role_text")
        if self._workforce is not None:
            # Профиль организации (ADR-035) — контекстом модели: цели роли наследуют KPI.
            result = self._workforce.draft(identity, kind=kind, role_text=role_text + role_context(org))
            return (422 if result.get("status") == "rejected" else 201), result
        employee_id = uuid4()
        passport_draft = {"kind": kind, "role_text": role_text, "status": "draft"}
        self._harness.append_harness_record(
            person_id=identity.person_id,
            company_id=identity.company_id,
            harness_id=harness_id,
            kind="employee",
            payload={
                "employee_id": str(employee_id),
                "kind": kind,
                "title": role_text[:120],
                "status": "pending",
                "autonomy": "L0",
                "passport_draft": passport_draft,
            },
        )
        proposal = self._proposals.create_proposal(
            person_id=identity.person_id,
            company_id=identity.company_id,
            harness_id=harness_id,
            kind="passport",
            summary=f"Создать паспорт роли: {role_text[:100]}",
            subject={
                "employee_id": str(employee_id),
                "kind": kind,
                "passport_draft": passport_draft,
            },
        )
        return 201, {
            "employee_id": str(employee_id),
            "passport_draft": passport_draft,
            "proposal_id": str(proposal.id),
            "status": proposal.status,
        }

    def list_employees(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        if self._workforce is not None:
            return 200, {"employees": self._workforce.list(identity)}
        records = self._harness.list_harness_records(
            person_id=identity.person_id,
            harness_id=harness_id,
            kind="employee",
        )
        employees = []
        for record in records:
            item = dict(record.payload)
            item.setdefault("employee_id", str(record.id))
            item.setdefault("kind", "new")
            item.setdefault("title", item.get("kind", "Сотрудник"))
            item.setdefault("status", "active")
            item.setdefault("autonomy", "L0")
            employees.append(item)
        return 200, {"employees": employees}

    def get_employee(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        workforce = self._require_workforce()
        return 200, workforce.get(identity, employee_id=self._required_uuid(payload, "employee_id"))

    def propose_passport(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        result = self._require_workforce().propose_edit(
            identity, employee_id=self._required_uuid(payload, "employee_id"), patch=payload.get("passport_patch"),
        )
        return (422 if result.get("status") == "rejected" else 201), result

    def employee_autonomy(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        return 200, self._require_workforce().autonomy(identity, employee_id=self._required_uuid(payload, "employee_id"))

    def pause_employee(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._set_paused(payload, paused=True)

    def resume_employee(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return self._set_paused(payload, paused=False)

    def _set_paused(self, payload: Mapping[str, Any], *, paused: bool) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        employee_id = self._required_uuid(payload, "employee_id")
        return 200, self._require_workforce().set_paused(identity, employee_id=employee_id, paused=paused)

    def _require_workforce(self) -> WorkforceService:
        if self._workforce is None:
            raise NotImplementedError("digital employees are not configured")
        return self._workforce

    # --- процессы, проекты, задачи (C-6) ------------------------------------------------

    def list_processes(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, {"processes": self._planning.list_processes(identity)}

    def get_process(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, self._planning.get_process(identity, self._required_text(payload, "process_id"))

    def create_process(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        template = process_template(self._optional_text(payload, "template_id"))
        if template is not None:
            # Процесс из каталога: поля — из шаблона, создание — обычным путём.
            return 201, self._planning.create_process(
                identity, name=template["name"], purpose=template["purpose"],
                inputs=template["inputs"], outputs=template["outputs"],
            )
        return 201, self._planning.create_process(
            identity, name=payload.get("name"), purpose=payload.get("purpose", ""),
            inputs=payload.get("inputs", []), outputs=payload.get("outputs", []),
            status=payload.get("status", "active"),
        )

    def list_projects(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, {"projects": self._planning.list_projects(identity)}

    def get_project(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, self._planning.get_project(identity, self._required_text(payload, "project_id"))

    def create_project(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 201, self._planning.create_project(
            identity, name=payload.get("name"), goal=payload.get("goal", ""),
            starts_on=payload.get("starts_on"), ends_on=payload.get("ends_on"),
            budget_usd=payload.get("budget_usd", 0), status=payload.get("status", "planned"),
            result=payload.get("result", ""),
        )

    def create_assignment(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 201, self._planning.assign(
            identity, employee_id=payload.get("employee_id"), target_type=payload.get("target_type"),
            target_id=payload.get("target_id"), role=payload.get("role", ""),
        )

    def list_tasks(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, {"tasks": self._planning.list_tasks(
            identity, assignment_id=payload.get("assignment_id"), status=payload.get("status"),
        )}

    def create_task(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 201, self._planning.create_task(
            identity, assignment_id=payload.get("assignment_id"), title=payload.get("title"),
            description=payload.get("description", ""), due_at=payload.get("due_at"),
        )

    def update_task(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, self._planning.update_task(
            identity, task_id=self._required_text(payload, "task_id"), status=payload.get("status"),
            title=payload.get("title"), description=payload.get("description"), due_at=payload.get("due_at"),
        )

    def planning_summary(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, self._planning.summary(identity)

    def planning_seed_examples(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        return 200, self._planning.seed_examples(identity)

    def list_proposals(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        proposals = self._proposals.list_proposals(
            person_id=identity.person_id,
            harness_id=harness_id,
            status=self._optional_text(payload, "status"),
            kind=self._optional_text(payload, "kind"),
            employee_id=self._optional_uuid(payload, "employee_id"),
        )
        return 200, {
            "proposals": [
                {
                    "proposal_id": str(item.id),
                    "kind": item.kind,
                    "employee_id": item.subject.get("employee_id"),
                    "summary": item.summary,
                    "payload": item.subject,
                    "change_plan": item.subject.get("change_plan"),
                    "sources": item.sources,
                    "score_ref": item.score_ref,
                    "status": item.status,
                    "created_at": item.created_at.isoformat(),
                }
                for item in proposals
            ]
        }

    def decide_proposal(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        proposal_id = self._required_uuid(payload, "proposal_id")
        decision = payload.get("decision")
        if not isinstance(decision, str) or decision not in {
            "approve",
            "return",
            "incident",
        }:
            raise ValueError("decision must be approve, return, or incident")
        # Поверхность — по входу, а не по полю запроса (C-1): сессия кабинета — web, бот — telegram.
        via_web = isinstance(payload.get("_session_identity"), Registration)
        surface = "web" if via_web else "telegram"
        if payload.get("surface", surface) != surface:
            raise ValueError("surface does not match how the curator signed in")
        try:
            result = self._proposals.decide(
                person_id=identity.person_id,
                harness_id=harness_id,
                proposal_id=proposal_id,
                decision=decision,
                actor_person_id=identity.person_id,
                surface=surface,
                channel_user_id=None if via_web else payload["channel_user_id"],
                comment=self._optional_text(payload, "comment"),
            )
        except ConsentError as error:
            if error.code != "not_curator":
                raise
            self._journal.append_entry(
                person_id=identity.person_id,
                company_id=identity.company_id,
                harness_id=harness_id,
                kind="proposal.decision_denied",
                payload={
                    "proposal_id": str(proposal_id),
                    "decision": decision,
                    "reason": error.code,
                    "surface": surface,
                },
            )
            return error.status_code, {"error": error.code}
        body: dict[str, Any] = {"status": result.status}
        if result.consent_token is not None:
            token = result.consent_token
            body["consent_token"] = self._proposals.token_document(token)
            applied = self._apply_approved(identity, proposal_id, token)
            if applied is not None:
                body["applied"] = applied
        elif self._workforce is not None:
            returned = self._workforce.on_passport_returned(identity, proposal_id=proposal_id)
            if returned is not None:
                body["applied"] = returned
        return 200, body

    def _apply_approved(self, identity, proposal_id: UUID, token) -> dict[str, Any] | None:
        """Согласие применяется сразу после решения куратора (C-6, C-3; ADR-024)."""
        handlers = {}
        if self._workforce is not None:
            handlers["passport"] = self._workforce.on_passport_approved
        if self._researcher is not None:
            handlers["connect"] = self._researcher.on_connect_approved
            handlers["adapter"] = self._researcher.on_adapter_approved
        if self._skills is not None:
            handlers["recommendation"] = self._skills.on_recommendation_approved
            handlers["skill_publish"] = self._skills.on_skill_publish_approved
        handler = handlers.get(token.scope)
        if handler is None:
            return None
        return handler(identity, proposal_id=proposal_id, token=token)

    def list_journal(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        page = self._journal.list_entries(
            person_id=identity.person_id,
            harness_id=harness_id,
            employee_id=self._optional_uuid(payload, "employee_id"),
            kind=self._optional_text(payload, "kind"),
            from_time=self._optional_datetime(payload, "from"),
            to_time=self._optional_datetime(payload, "to"),
            cursor=self._optional_text(payload, "cursor"),
        )
        return 200, {
            "entries": [
                {
                    "entry_id": str(entry.id),
                    "employee_id": (
                        str(entry.employee_id) if entry.employee_id else None
                    ),
                    "kind": entry.kind,
                    "payload": entry.payload,
                    "sources": entry.sources,
                    "score_ref": entry.score_ref,
                    "created_at": entry.created_at.isoformat(),
                }
                for entry in page.entries
            ],
            "next_cursor": page.next_cursor,
        }

    def start_connect(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        system = self._system(payload)
        now = self._clock()
        request_id = uuid4()
        exp = now + timedelta(minutes=30)
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO connect_requests (id, harness_id, system, created_at, exp)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (request_id, harness_id, system, now, exp),
            )
        cabinet_url = (
            f"{self._base_url}/connections?"
            f"{urlencode({'system': system, 'connect_request_id': str(request_id)})}"
        )
        return 201, {
            "connect_request_id": str(request_id),
            "cabinet_url": cabinet_url,
            "exp": exp.isoformat(),
        }

    def connect_landscape(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        system = self._system(payload)
        secret_ref = self._required_text(payload, "secret_ref")
        env_ref = re.fullmatch(r"env:([A-Z][A-Z0-9_]*)", secret_ref)
        if env_ref is not None:
            if SERVICE_SECRET.fullmatch(env_ref.group(1)):
                raise ValueError("secret_ref must not point at the harness's own secrets")
        elif VAULT_REF.fullmatch(secret_ref):
            owner = self._vault.owner(secret_ref) if self._vault is not None else None
            if owner != (harness_id, system):
                raise ValueError("secret_ref is not stored for this harness and system")
        else:
            raise ValueError("secret_ref must reference the secret store")

        raw_request_id = payload.get("connect_request_id")
        if raw_request_id is not None:
            request_id = self._required_uuid(payload, "connect_request_id")
            now = self._clock()
            with self._connection.transaction():
                request = self._connection.execute(
                    """
                    UPDATE connect_requests
                    SET used_at = %s
                    WHERE id = %s AND harness_id = %s AND system = %s
                      AND exp > %s AND used_at IS NULL
                    RETURNING id
                    """,
                    (now, request_id, harness_id, system, now),
                ).fetchone()
            if request is None:
                raise InvalidConnectRequest()

        proposal = self._proposals.create_proposal(
            person_id=identity.person_id,
            company_id=identity.company_id,
            harness_id=harness_id,
            kind="connect",
            summary=f"Подключить систему {system}",
            subject={"system": system, "secret_ref": secret_ref},
        )
        return 201, {"proposal_id": str(proposal.id)}

    def put_secret(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Доступ из формы кабинета → хранилище; наружу и в журнал — только ссылка (GT-c1-13)."""
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        system = self._system(payload)
        if self._vault is None:
            raise VaultUnavailable("secret store is not configured")
        secret = payload.get("secret")
        if not isinstance(secret, str):
            raise ValueError("secret must be a string")
        secret_ref = self._vault.put(identity, system=system, secret=secret)
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=harness_id,
            kind="secret.stored", payload={"system": system, "secret_ref": secret_ref},
        )
        return 201, {"secret_ref": secret_ref}

    # --- настройки LLM в кабинете (ADR-033) -------------------------------------------------------

    @staticmethod
    def _as_bool(value: Any) -> bool:
        return str(value).strip().lower() in {"true", "1", "on", "yes"} if value is not None else False

    def _effective_llm(self) -> dict[str, Any]:
        """Действующий конфиг LLM: строка из кабинета поверх env; сам ключ наружу не отдаём."""
        row = self._llm_settings.get()
        if row is not None:
            provider, api_base = row["provider"], row["api_base"]
            model_main, model_judge, dlp_enabled = row["model_main"], row["model_judge"], row["dlp_enabled"]
            api_key_ref = row["api_key_ref"] or env_key_ref(provider)
            source = "cabinet"
        elif self._settings is not None:
            s = self._settings
            provider, api_base = s.llm_provider, s.llm_api_base
            model_main, model_judge, dlp_enabled = s.llm_model_main, s.llm_model_judge, s.dlp_enabled
            api_key_ref, source = s.llm_api_key_ref, "env"
        else:
            provider = self._llm_provider
            api_base, model_main, model_judge = default_api_base(provider), DEFAULT_MODEL_MAIN, DEFAULT_MODEL_JUDGE
            dlp_enabled, api_key_ref, source = True, env_key_ref(provider), "env"
        try:
            key_present = bool(self._secret_store.resolve(api_key_ref).strip())
        except Exception:  # noqa: BLE001 — нет ключа/хранилища: просто «ключ не задан»
            key_present = False
        return {
            "provider": provider, "api_base": api_base, "model_main": model_main, "model_judge": model_judge,
            "dlp_enabled": dlp_enabled, "key_present": key_present,
            "key_source": "vault" if isinstance(api_key_ref, str) and api_key_ref.startswith("vault:") else "env",
            "source": source, "editable": self._settings_editable, "_api_key_ref": api_key_ref,
        }

    def get_llm_settings(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        self._identity(payload)                       # только для залогиненного куратора
        config = self._effective_llm()
        config.pop("_api_key_ref", None)
        return 200, config

    def put_llm_settings(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        if not self._settings_editable:
            raise ConsentError("settings_locked")     # редактирование включается флагом установки
        provider = self._required_text(payload, "provider").lower()
        api_base = self._required_text(payload, "api_base")
        model_main = self._required_text(payload, "model_main")
        model_judge = self._optional_text(payload, "model_judge")
        dlp_enabled = self._as_bool(payload.get("dlp_enabled"))
        validate_llm_settings(provider, api_base, model_main, model_judge, dlp_enabled)  # ValueError → 400
        # Ключ: новый — в vault; пустой — оставляем прежнюю ссылку (меняем только адрес/модель).
        current = self._llm_settings.get()
        api_key_ref = current["api_key_ref"] if current else None
        key_state = "unchanged" if api_key_ref else "env"
        new_key = payload.get("api_key")
        if isinstance(new_key, str) and new_key.strip():
            if self._vault is None:
                raise VaultUnavailable("secret store is not configured")
            api_key_ref = self._vault.put(identity, system="llm", secret=new_key)
            key_state = "set"
        self._llm_settings.put(
            provider=provider, api_base=api_base, model_main=model_main, model_judge=model_judge,
            api_key_ref=api_key_ref, dlp_enabled=dlp_enabled, updated_by=identity.person_id,
        )
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="llm_settings.updated",
            payload={"provider": provider, "api_base": api_base, "model_main": model_main,
                     "model_judge": model_judge, "dlp_enabled": dlp_enabled, "key": key_state},
        )
        config = self._effective_llm()
        config.pop("_api_key_ref", None)
        config["applies_on_restart"] = True           # коннектор собран на старте — изменения после перезапуска
        return 200, config

    def test_llm_settings(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        self._identity(payload)
        if not self._settings_editable:
            raise ConsentError("settings_locked")
        config = self._effective_llm()
        provider, api_base = config["provider"], config["api_base"]
        try:
            key = self._secret_store.resolve(config["_api_key_ref"]).strip()
        except Exception:  # noqa: BLE001
            key = None
        if provider != "local" and not key:
            return 200, {"ok": False, "detail": "Ключ не задан"}
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        http = self._llm_http or httpx.Client(timeout=15)
        try:
            resp = http.get(f"{api_base.rstrip('/')}/models", headers=headers)
        except httpx.HTTPError as error:
            logging.getLogger("metabrain.harness").warning("llm test failed: %s", type(error).__name__)
            return 200, {"ok": False, "detail": "Нет связи с провайдером"}
        finally:
            if self._llm_http is None:
                http.close()
        if resp.status_code != 200:
            return 200, {"ok": False, "detail": f"Провайдер ответил HTTP {resp.status_code}"}
        try:
            ids = {m.get("id") for m in resp.json().get("data", [])}
        except ValueError:
            ids = set()
        wanted = {config["model_main"]} | ({config["model_judge"]} if config["model_judge"] else set())
        missing = {m for m in wanted if m and ids and m not in ids}
        if missing and provider == "deepseek":
            return 200, {"ok": False, "detail": "Модели не найдены у провайдера: " + ", ".join(sorted(missing))}
        tail = f", моделей доступно: {len(ids)}" if ids else ""
        return 200, {"ok": True, "detail": "Подключение работает" + tail}

    def list_skills(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        return 200, {"skills": self._require_skills().list_skills(identity, scope=self._optional_text(payload, "scope"))}

    def share_skill(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Личный навык → предложение `skill_publish` (C-1 `skill.share`); не прошёл Skill — 422 с причинами."""
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        outcome = self._require_skills().share(identity, skill_id=self._required_text(payload, "skill_id"))
        if outcome["status"] != "proposed":
            return 422, {"error": "skill_rejected", "reasons": outcome.get("reasons", [])}
        return 201, {"proposal_id": outcome["proposal_id"]}

    def _require_skills(self) -> SkillFlow:
        if self._skills is None:
            raise NotImplementedError("skills require the metabrain (METABRAIN_URL)")
        return self._skills

    def _system(self, payload: Mapping[str, Any]) -> str:
        """Идентификатор системы; если Исследователь подключён — только из реестра LANDSCAPE_SYSTEMS."""
        system = self._required_text(payload, "system")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", system):
            raise ValueError("invalid system identifier")
        allowed_systems = (
            self._system_registry
            if self._system_registry is not None
            else self._researcher.systems() if self._researcher is not None else None
        )
        if allowed_systems is not None and system not in allowed_systems:
            raise ValueError(f"unknown system {system!r}: not in the landscape registry")
        return system

    def get_landscape_map(
        self, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        harness_id = self._requested_harness(payload, identity.harness_id)
        records = self._harness.list_harness_records(
            person_id=identity.person_id,
            harness_id=harness_id,
            kind="landscape",
        )
        return 200, {
            "map": {
                str(record.payload.get("system", record.id)): record.payload
                for record in records
            },
            "systems": self._researcher.statuses(harness_id) if self._researcher is not None else [],
            "sources": [],
        }

    def ask(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        if self._ask_runtime is None or self._workforce is None:
            raise NotImplementedError("ask requires the T-14 digital employee runtime")
        text = self._required_text(payload, "text")
        employee = self._workforce.employee_for_ask(identity, employee_id=self._optional_uuid(payload, "employee_id"))
        job_id = self._ask_runtime.enqueue(employee, duty_id="ask", trigger="ask", input={"text": text[:2000]})
        if job_id is None:
            raise EmployeeBusy()
        outcome = self._ask_runtime.run_job(job_id)
        state = self._ask_runtime.report(job_id) or {}
        report = state.get("report")
        if outcome != "done" or not report:
            error = str(state.get("error") or "")
            if error.startswith("BudgetExceededError"):
                raise BudgetExceededError(error)
            raise WorkforceUnavailable(f"employee could not answer: {error.split(':', 1)[0] or 'unknown'}")
        if report.get("blocked"):
            return 422, {"error": "answer_blocked", "message": report["text"], "reasons": report.get("reasons", [])}
        return 200, {"answer": report["text"], "sources": report["sources"], "score_ref": report["score_ref"],
                     "proposal_ids": report.get("proposal_ids", []), "employee_id": str(employee.employee_id)}

    def get_org(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Профиль организации (ADR-035) + словари для формы кабинета."""
        identity = self._identity(payload)
        return 200, {**self._org.get(identity.company_id),
                     "industries": INDUSTRIES, "sizes": SIZES}

    def put_org(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        identity = self._identity(payload)
        org = self._org.put(identity.company_id, industry=payload.get("industry"),
                            size=payload.get("size"), kpi=payload.get("kpi"),
                            updated_by=identity.person_id)   # ValueError -> 400
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="org.updated", payload={"industry": org["industry"], "size": org["size"],
                                         "kpi_set": bool(org["kpi"])},
        )
        return 200, org

    def list_catalog(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Каталог преднастроенных ролей и процессов (витрина, без полных черновиков)."""
        self._identity(payload)
        return 200, catalog_listing()

    def diagnose_employee(self, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Помощь/диагностика (ADR-029, гейт): рой сотрудника разбирает журнал и вопрос куратора.

        Диагноз возвращается куратору; рекомендация из ответа идёт обычным путём —
        предложение → согласие куратора → навык (SkillFlow). Автономной правки нет.
        """
        identity = self._identity(payload)
        self._requested_harness(payload, identity.harness_id)
        employee_id = self._optional_uuid(payload, "employee_id")
        if employee_id is None:
            raise ValueError("employee_id is required")
        question = (self._optional_text(payload, "question") or "").strip()[:500]
        page = self._journal.list_entries(person_id=identity.person_id, harness_id=identity.harness_id,
                                          employee_id=employee_id, page_size=15)
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="help.requested", employee_id=employee_id,
            payload={"question": question or "(без вопроса)"},
        )
        text = diagnosis.prompt(question, diagnosis.digest(page.entries))
        status, body = self.ask({**dict(payload), "text": text, "employee_id": str(employee_id)})
        if status == 200:
            body["diagnosis"] = True
        return status, body

    def _identity(self, payload: Mapping[str, Any]):
        session_identity = payload.get("_session_identity")
        if isinstance(session_identity, Registration):
            return session_identity
        channel_user_id = self._required_text(payload, "channel_user_id")
        identity = self._harness.identity_for_channel_user(
            channel="telegram",
            channel_user_id=channel_user_id,
        )
        if identity is None:
            raise HTTPException(401, detail={"error": "registration_required"})
        return identity

    def _requested_harness(self, payload: Mapping[str, Any], actual: UUID) -> UUID:
        value = payload.get("harness_id")
        if value is None:
            return actual
        try:
            requested = UUID(str(value))
        except ValueError as error:
            raise ValueError("harness_id must be a UUID") from error
        if requested != actual:
            raise HarnessAccessDenied()
        return requested

    def _company_for_invite(self, invite: str) -> UUID:
        token_hash = sha256(invite.encode("utf-8")).hexdigest()
        row = self._connection.execute(
            """
            SELECT company_id FROM company_invites
            WHERE token_hash = %s AND exp > %s
            """,
            (token_hash, self._clock()),
        ).fetchone()
        if row is None:
            raise InvalidInvite()
        return row[0]

    def _issue_invite(self, company_id: UUID, person_id: UUID) -> tuple[str, datetime]:
        if not self._bot_username:
            raise RuntimeError("TELEGRAM_BOT_USERNAME is required to create invite links")
        token = secrets.token_urlsafe(32)
        exp = self._clock() + timedelta(days=7)
        with self._connection.transaction():
            self._connection.execute(
                """
                INSERT INTO company_invites
                    (token_hash, company_id, created_by, created_at, exp)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    sha256(token.encode("utf-8")).hexdigest(),
                    company_id,
                    person_id,
                    self._clock(),
                    exp,
                ),
            )
        link = f"https://t.me/{self._bot_username}?{urlencode({'start': token})}"
        return link, exp

    @staticmethod
    def _required_text(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{key} is required")
        return value.strip()

    @staticmethod
    def _optional_text(payload: Mapping[str, Any], key: str) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        return value.strip() or None

    @staticmethod
    def _required_uuid(payload: Mapping[str, Any], key: str) -> UUID:
        try:
            return UUID(str(payload[key]))
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError(f"{key} must be a UUID") from error

    @classmethod
    def _optional_uuid(cls, payload: Mapping[str, Any], key: str) -> UUID | None:
        value = payload.get(key)
        if value is None:
            return None
        return cls._required_uuid(payload, key)

    @classmethod
    def _optional_datetime(
        cls, payload: Mapping[str, Any], key: str
    ) -> datetime | None:
        value = cls._optional_text(payload, key)
        if value is None:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError(f"{key} must be an ISO-8601 datetime") from error
        if parsed.utcoffset() is None:
            raise ValueError(f"{key} must include a timezone")
        return parsed


def create_app(
    connection: Connection,
    *,
    api_token: str,
    app_base_url: str,
    telegram_bot_username: str = "metabrain_bot",
    llm_provider: str = "deepseek",
    secret_store: EnvSecretStore | None = None,
    consent_signing_key_ref: str = "env:CONSENT_SIGNING_KEY",
    consent_public_key_ref: str = "env:CONSENT_PUBLIC_KEY",
    consent_token_ttl: timedelta = timedelta(minutes=15),
    telegram_bot_token: str | None = None,
    telegram_api_base_url: str = "http://127.0.0.1:8080",
    clock: Callable[[], datetime] | None = None,
    workforce: WorkforceService | None = None,
    researcher: ResearcherService | None = None,
    web_auth: WebAuth | None = None,
    ask_runtime: Runtime | None = None,
    skills: SkillFlow | None = None,
    vault: HarnessVault | None = None,
    system_registry: Collection[str] | None = None,
    settings: Settings | None = None,
    llm_settings_editable: bool = False,
    llm_http_client: httpx.Client | None = None,
) -> FastAPI:
    if not api_token.strip():
        raise ValueError("harness API token is required")
    service = C1Service(
        connection,
        app_base_url=app_base_url,
        telegram_bot_username=telegram_bot_username,
        llm_provider=llm_provider,
        secret_store=secret_store or EnvSecretStore(),
        settings=settings,
        llm_settings_editable=llm_settings_editable,
        llm_http_client=llm_http_client,
        consent_signing_key_ref=consent_signing_key_ref,
        consent_public_key_ref=consent_public_key_ref,
        consent_token_ttl=consent_token_ttl,
        clock=clock,
        workforce=workforce,
        researcher=researcher,
        web_auth=web_auth,
        ask_runtime=ask_runtime,
        skills=skills,
        vault=vault,
        system_registry=system_registry,
    )
    app = FastAPI(title="metabrain-harness", docs_url=None, redoc_url=None)
    app.state.telegram_task = None
    app.state.telegram_bot = None
    app.state.telegram_api = None

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "harness"}

    @app.post("/v1/operations/{operation}")
    async def operation(
        operation: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        expected = f"Bearer {api_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, detail={"error": "unauthorized"})
        try:
            payload = await request.json()
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            def execute_operation() -> tuple[int, dict[str, Any]]:
                with connection.transaction():
                    return service.call(operation, payload)

            status, result = await run_in_threadpool(execute_operation)
            return JSONResponse(result, status_code=status)
        except HTTPException:
            raise
        except ExternalLLMConsentRequired:
            return JSONResponse(
                {"error": "external_llm_consent_required"},
                status_code=403,
            )
        except InvalidInvite:
            return JSONResponse({"error": "invalid_invite"}, status_code=400)
        except InvalidConnectRequest:
            return JSONResponse(
                {"error": "invalid_connect_request"},
                status_code=400,
            )
        except (HarnessAccessDenied, ConsentError) as error:
            return JSONResponse(
                {"error": getattr(error, "code", "forbidden_harness")},
                status_code=403,
            )
        except ProposalConflict as error:
            return JSONResponse({"error": error.code}, status_code=409)
        except PlanningNotFound:
            return JSONResponse({"error": "not_found"}, status_code=404)
        except PlanningConflict as error:
            return JSONResponse({"error": error.code}, status_code=409)
        except PlanningInvalid as error:
            return JSONResponse({"error": error.code, "detail": str(error)}, status_code=400)
        except EmployeeBusy as error:
            return JSONResponse({"error": error.code}, status_code=409)
        except PassportConflict as error:
            return JSONResponse({"error": error.code}, status_code=409)
        except (EmployeeNotFound, SkillNotFound):
            return JSONResponse({"error": "not_found"}, status_code=404)
        except BudgetExceededError as error:
            return JSONResponse(
                {
                    "error": "budget_exceeded",
                    "limit": getattr(error, "limit_name", None),
                    "remaining": str(getattr(error, "remaining", "")) or None,
                },
                status_code=429,
            )
        except WebAuthError as error:
            return JSONResponse({"error": error.code}, status_code=401)
        except CsrfRequired:
            return JSONResponse({"error": "csrf_required"}, status_code=403)
        except WebAuthNotConfigured as error:
            return JSONResponse({"error": error.code}, status_code=503)
        except (WorkforceUnavailable, LandscapeUnavailable, VaultUnavailable) as error:
            return JSONResponse({"error": error.code, "detail": str(error)}, status_code=503)
        except (RegistrationError, CompanyNotFoundError, ValueError) as error:
            return JSONResponse(
                {"error": "invalid_request", "detail": str(error)},
                status_code=400,
            )
        except errors.UniqueViolation:
            return JSONResponse({"error": "already_registered"}, status_code=409)
        except NotImplementedError as error:
            return JSONResponse(
                {"error": "not_implemented", "detail": str(error)},
                status_code=501,
            )

    if telegram_bot_token:
        import asyncio

        from aiogram import Bot, Dispatcher

        from src.harness.telegram import HTTPC1API, TelegramAdapter, create_router

        bot = Bot(telegram_bot_token)
        dispatcher = Dispatcher()
        api_client = HTTPC1API(
            telegram_api_base_url,
            api_token=api_token,
        )
        dispatcher.include_router(create_router(TelegramAdapter(api_client)))
        app.state.telegram_bot = bot
        app.state.telegram_api = api_client

        @app.on_event("startup")
        async def start_telegram_polling() -> None:
            await bot.get_me()
            webhook = await bot.get_webhook_info()
            if webhook.url:
                # Токен уже обслуживает вебхук (другой бот или сервис на этом сервере) — не перехватываем его.
                import logging

                logging.getLogger("metabrain.harness").error(
                    "TELEGRAM_BOT_TOKEN belongs to a bot with a webhook; polling disabled to keep that bot working"
                )
                app.state.telegram_polling = "webhook_conflict"
                return
            app.state.telegram_polling = "polling"
            app.state.telegram_task = asyncio.create_task(
                dispatcher.start_polling(bot, handle_signals=False)
            )

        @app.on_event("shutdown")
        async def stop_telegram_polling() -> None:
            task = app.state.telegram_task
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await api_client.aclose()
            await bot.session.close()

    return app
