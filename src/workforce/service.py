"""Цифровые сотрудники харнесса: паспорт, активация по согласию (C-6, T-13)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import httpx
from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.proposals import ConsentToken, ProposalStore
from src.harness.store import Registration
from src.common.secrets import SecretNotFoundError
from src.llm.connector import LLMContext, ProviderResponseError
from src.metabrain.policy import load_policy
from src.validator.client import ValidatorClient, ValidatorUnavailable
from src.validator.scorecard import ScoreCardRejected, verify_scorecard
from src.workforce.passport import (
    DraftError,
    PassportDrafter,
    apply_patch,
    build_passport,
    draft_fields,
    parse_draft,
    passport_diff,
)
from src.workforce.store import WorkforceStore, migrate


# Подключённые системы харнесса: (connectors, read_scopes).
ToolsProvider = Callable[[UUID], tuple[Sequence[str], Sequence[str]]]


class PassportRejected(ValueError):
    """Паспорт не прошёл валидатор (профиль Passport) — предложение не создаётся."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons) or "passport rejected")
        self.reasons = reasons


class EmployeeNotFound(LookupError):
    """Нет такого сотрудника в харнессе вызывающего — 404."""


class EmployeeBusy(RuntimeError):
    """Сотрудник ещё отвечает на прошлый вопрос — 409."""

    code = "employee_busy"


class PassportConflict(RuntimeError):
    """Править нечего: у сотрудника нет действующего паспорта — 409."""

    code = "no_active_passport"


class WorkforceUnavailable(RuntimeError):
    code = "workforce_unavailable"


SEED_TWIN_DRAFT = """{
  "role": {"title": "Менеджер по продажам", "mission": "Готовить КП по новым заявкам"},
  "duties": [{"text": "Разбирать новые заявки"}, {"text": "Сверять наличие и сроки"}, {"text": "Готовить черновик КП"}],
  "goals": [{"text": "КП по заявке за 2 часа", "metric": "quote_hours", "target": "2", "period": "day"}],
  "decision_bounds": {"may": ["предлагать аналоги из каталога"], "must_not": ["давать скидку ниже минимальной маржи"]},
  "escalation": [{"condition": "нет позиции и подходящего аналога"}],
  "schedule": [{"cron": "0 9 * * 1-5", "duty": 1}]
}"""


class WorkforceService:
    def __init__(
        self,
        connection: Connection,
        *,
        proposals: ProposalStore,
        journal: JournalStore,
        drafter: PassportDrafter,
        validator: ValidatorClient,
        tools: ToolsProvider,
        clock: Callable[[], datetime] | None = None,
        policy: Mapping[str, Any] | None = None,
    ):
        self._c = connection
        # Пороги автономии (C-5 policy.autonomy); по умолчанию — политика из репо.
        self._policy = policy
        self._store = WorkforceStore(connection)
        self._proposals = proposals
        self._journal = journal
        self._drafter = drafter
        self._validator = validator
        self._tools = tools
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        migrate(connection)

    # --- список ------------------------------------------------------------------

    def list(self, identity: Registration) -> list[dict[str, Any]]:
        self._store.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        result = []
        for employee in self._store.list_employees(harness_id=identity.harness_id):
            levels = {}
            if employee.active_version:
                active = self._store.passport(employee_id=employee.employee_id, version=employee.active_version)
                levels = (active or {}).get("passport", {}).get("autonomy", {})
            result.append({
                "employee_id": str(employee.employee_id),
                "kind": employee.kind,
                "title": employee.title,
                "status": employee.status,
                "active_version": employee.active_version,
                "autonomy": max(levels.values(), default="L0"),
            })
        return result

    # --- черновик паспорта ---------------------------------------------------------

    def draft(self, identity: Registration, *, kind: str, role_text: str) -> dict[str, Any]:
        if kind not in ("twin", "new"):
            raise ValueError("kind must be twin or new")
        self._store.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        employee_id = uuid4()
        context = LLMContext(
            company_id=str(identity.company_id), harness_id=str(identity.harness_id),
            employee_id=str(employee_id), job_id=f"passport-draft:{employee_id}", sub_role="developer",
        )
        try:
            draft = self._drafter.draft(role_text, context)
        except DraftError as error:
            raise ValueError(f"passport draft failed: {error}") from error
        except (SecretNotFoundError, ProviderResponseError, httpx.HTTPError) as error:
            raise WorkforceUnavailable(f"LLM is unavailable: {type(error).__name__}") from error

        return self._draft_from(identity, employee_id=employee_id, kind=kind, draft=draft)

    def seed_twin(self, identity: Registration) -> dict[str, Any]:
        """Пример-сотрудник в шаблон новой организации (ADR-030): канонический черновик без LLM, через валидатор, предложением куратору."""
        self._store.ensure_researcher(company_id=identity.company_id, harness_id=identity.harness_id)
        return self._draft_from(identity, employee_id=uuid4(), kind="new", draft=parse_draft(SEED_TWIN_DRAFT))

    def _draft_from(self, identity: Registration, *, employee_id: UUID, kind: str,
                    draft: Mapping[str, Any]) -> dict[str, Any]:
        passport = self._build(identity, employee_id=employee_id, version=1, kind=kind, draft=draft,
                               source="role_text", drafted_by="developer")
        try:
            artifact, scorecard = self._validate(identity, passport, previous=None)
        except PassportRejected as rejected:
            # Не исключение наружу: иначе транзакция API откатит запись причин в журнал.
            return {"status": "rejected", "reasons": rejected.reasons, "passport_draft": passport}

        self._store.create_employee(employee_id=employee_id, company_id=identity.company_id,
                                    harness_id=identity.harness_id, kind=kind, title=passport["role"]["title"])
        self._store.add_passport(employee_id=employee_id, version=1, passport=passport,
                                 artifact=artifact, scorecard=scorecard)
        proposal = self._proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="passport", summary=f"Паспорт роли: {passport['role']['title']}",
            subject={"employee_id": str(employee_id), "version": 1, "passport": passport},
            score_ref=scorecard["scorecard_id"],
        )
        self._store.set_passport_proposal(employee_id=employee_id, version=1, proposal_id=proposal.id)
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="employee.passport.proposed", employee_id=employee_id,
            payload={"version": 1, "proposal_id": str(proposal.id), "kind": kind},
            score_ref=scorecard["scorecard_id"],
        )
        return {"employee_id": str(employee_id), "passport_draft": passport, "proposal_id": str(proposal.id),
                "status": proposal.status}

    # --- карточка сотрудника и правка паспорта (C-1, T-32) ------------------------------

    def get(self, identity: Registration, *, employee_id: UUID) -> dict[str, Any]:
        employee = self._store.get_employee(employee_id=employee_id, harness_id=identity.harness_id)
        if employee is None:
            raise EmployeeNotFound("employee not found")
        versions = self._store.versions(employee_id=employee_id)
        shown = employee.active_version or (versions[0]["version"] if versions else None)
        passport = self._store.passport(employee_id=employee_id, version=shown)["passport"] if shown else None
        return {
            "employee_id": str(employee.employee_id), "kind": employee.kind, "title": employee.title,
            "status": employee.status, "active_version": employee.active_version, "shown_version": shown,
            "passport": passport, "versions": versions,
            "pending_proposals": [v["proposal_id"] for v in versions if v["status"] == "proposed" and v["proposal_id"]],
        }

    def employee_for_ask(self, identity: Registration, *, employee_id: UUID | None = None):
        """Кому адресован вопрос: указанный активный ЦС или по умолчанию двойник (C-1 `ask`)."""
        active = [e for e in self._store.list_employees(harness_id=identity.harness_id)
                  if e.kind != "researcher" and e.status == "active" and e.active_version]
        if employee_id is not None:
            active = [e for e in active if e.employee_id == employee_id]
        active.sort(key=lambda e: e.kind != "twin")
        if not active:
            raise EmployeeNotFound("no active employee to answer")
        return active[0]

    def autonomy(self, identity: Registration, *, employee_id: UUID) -> dict[str, Any]:
        """Уровни по классам действий, метрики против порогов (C-1 `employee.autonomy`). Повышение — T-27."""
        employee = self._store.get_employee(employee_id=employee_id, harness_id=identity.harness_id)
        if employee is None:
            raise EmployeeNotFound("employee not found")
        passport = {}
        if employee.active_version:
            passport = self._store.passport(employee_id=employee_id, version=employee.active_version)["passport"]
        policy = (self._policy or load_policy())["autonomy"]
        thresholds = policy["promotion_thresholds"]
        levels = dict(passport.get("autonomy") or {})
        classes = []
        for action_class in sorted(set((passport.get("tools") or {}).get("write_classes") or []) | set(levels)):
            level = levels.get(action_class, "L0")
            metrics = self._decisions(identity, employee_id, kind="action", action_class=action_class)
            step = {"L0": ("L1", "L0_to_L1"), "L1": ("L2", "L1_to_L2")}.get(level)
            needed = thresholds[step[1]] if step else {}
            unmet = unmet_thresholds(metrics, needed)
            allowed = step is not None and (step[0] != "L2" or action_class in policy["allowed_l2_classes"])
            classes.append({"action_class": action_class, "level": level, "metrics": metrics, "thresholds": needed,
                            "unmet": unmet, "eligible_for": step[0] if allowed and not unmet else None})
        # В демо классов записи нет — ЦС только рекомендует (L0); показываем, как копятся решения к порогу L1.
        recommendations = self._decisions(identity, employee_id, kind="recommendation")
        return {"employee_id": str(employee_id), "classes": classes, "recommendations": recommendations,
                "l1_thresholds": thresholds["L0_to_L1"],
                "l1_unmet": unmet_thresholds(recommendations, thresholds["L0_to_L1"])}

    def _decisions(self, identity: Registration, employee_id: UUID, *, kind: str,
                   action_class: str | None = None) -> dict[str, Any]:
        rows = self._c.execute(
            """
            SELECT status, count(*), max(decided_at) FILTER (WHERE status = 'incident') FROM proposals
            WHERE harness_id = %s AND kind = %s AND subject->>'employee_id' = %s
              AND (%s::text IS NULL OR subject->>'action_class' = %s::text)
            GROUP BY status
            """,
            (identity.harness_id, kind, str(employee_id), action_class, action_class),
        ).fetchall()
        counts = {status: count for status, count, _ in rows}
        decided = sum(count for status, count in counts.items() if status != "pending")
        last_incident = max((at for _, _, at in rows if at is not None), default=None)
        return {
            "decided": decided, "accepted": counts.get("approved", 0), "incidents": counts.get("incident", 0),
            "accepted_ratio": round(counts.get("approved", 0) / decided, 4) if decided else None,
            "incident_free_days": max(0, (self._clock() - last_incident).days) if last_incident else None,
            "eval_score": None,  # оценка по eval-корпусу — с T-12 (полный) и T-27
        }

    def set_paused(self, identity: Registration, *, employee_id: UUID, paused: bool) -> dict[str, Any]:
        """Пауза и возобновление — решение куратора, сразу в журнал (C-1 `employee.pause`/`employee.resume`)."""
        employee = self._store.get_employee(employee_id=employee_id, harness_id=identity.harness_id)
        if employee is None:
            raise EmployeeNotFound("employee not found")
        if employee.kind == "researcher":
            raise ValueError("the researcher is a service employee and cannot be paused")
        source, target = ("active", "paused") if paused else ("paused", "active")
        if employee.status == target:
            return {"employee_id": str(employee_id), "status": target}
        if employee.status != source:
            raise PassportConflict("employee has no active passport")
        with self._c.transaction():
            if not self._store.set_status(employee_id=employee_id, harness_id=identity.harness_id,
                                          source=source, target=target):
                raise PassportConflict("employee status changed concurrently")
            self._journal.append_entry(
                person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
                kind="employee.paused" if paused else "employee.resumed", employee_id=employee_id,
                payload={"actor": str(identity.person_id)},
            )
        return {"employee_id": str(employee_id), "status": target}

    def propose_edit(self, identity: Registration, *, employee_id: UUID, patch: Any) -> dict[str, Any]:
        """Новая версия паспорта по правке куратора — только через предложение (R-24, C-010)."""
        employee = self._store.get_employee(employee_id=employee_id, harness_id=identity.harness_id)
        if employee is None:
            raise EmployeeNotFound("employee not found")
        base = self._base(employee) if employee.kind != "researcher" else None
        if base is None:
            raise PassportConflict("employee has no passport to edit")
        try:
            draft = apply_patch(base, patch)
        except DraftError as error:
            raise ValueError(f"passport_patch: {error}") from error
        version = self._store.next_version(employee_id)
        passport = build_passport(
            draft, employee_id=str(employee_id), version=version, kind=employee.kind,
            harness_id=str(identity.harness_id), owner_person_id=str(identity.person_id),
            connectors=base["tools"]["connectors"], read_scopes=base["tools"]["read_scopes"],
            source="role_text", drafted_by="curator", created_at=self._clock().isoformat(), autonomy=base["autonomy"],
        )
        diff = passport_diff(base, passport)
        if not diff:
            raise ValueError("passport_patch changes nothing")
        active = self._active(employee)
        try:
            artifact, scorecard = self._validate(identity, passport, previous=active)
        except PassportRejected as rejected:
            return {"status": "rejected", "reasons": rejected.reasons, "diff": diff}
        self._supersede(identity, employee.employee_id, reason=f"Заменено правкой куратора (версия {version})")
        self._store.add_passport(employee_id=employee_id, version=version, passport=passport,
                                 artifact=artifact, scorecard=scorecard)
        subject = {"employee_id": str(employee_id), "version": version, "passport": passport,
                   "previous_version": employee.active_version}
        if active is not None:
            subject["diff"] = passport_diff(active, passport)  # что изменится при подтверждении
        proposal = self._proposals.create_proposal(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="passport", summary=f"Изменить паспорт «{employee.title}» (версия {version})",
            subject=subject, score_ref=scorecard["scorecard_id"],
        )
        self._store.set_passport_proposal(employee_id=employee_id, version=version, proposal_id=proposal.id)
        return {"proposal_id": str(proposal.id), "version": version, "diff": diff}

    def on_passport_returned(self, identity: Registration, *, proposal_id: UUID) -> dict[str, Any] | None:
        """«Вернуть» по паспорту: версия помечается возвращённой, действующая не меняется."""
        row = self._c.execute(
            "SELECT subject FROM proposals WHERE id = %s AND harness_id = %s AND kind = 'passport'",
            (proposal_id, identity.harness_id),
        ).fetchone()
        if row is None:
            return None
        subject = row[0]
        self._store.mark_returned(employee_id=UUID(subject["employee_id"]), version=int(subject["version"]))
        return {"employee_id": subject["employee_id"], "version": subject["version"], "status": "returned"}

    # --- новая версия при новом подключении (C-6, «Новые подключения») --------------

    def propose_tool_extension(self, identity: Registration, *, system: str) -> list[str]:
        """Каждому активному ЦС — предложение версии паспорта с новой системой в tools."""
        connectors, scopes = self._tools(identity.harness_id)
        proposal_ids = []
        for employee in self._store.list_employees(harness_id=identity.harness_id):
            if employee.kind == "researcher" or employee.status not in ("active", "paused") or not employee.active_version:
                continue
            base = self._base(employee)
            if set(base["tools"]["connectors"]) >= set(connectors) and set(base["tools"]["read_scopes"]) >= set(scopes):
                continue
            version = self._store.next_version(employee.employee_id)
            passport = build_passport(
                draft_fields(base), employee_id=str(employee.employee_id), version=version, kind=employee.kind,
                harness_id=str(identity.harness_id), owner_person_id=str(identity.person_id),
                connectors=connectors, read_scopes=scopes, source="template", drafted_by="researcher",
                created_at=self._clock().isoformat(), autonomy=base["autonomy"],
            )
            active = self._active(employee)
            try:
                artifact, scorecard = self._validate(identity, passport, previous=active)
            except PassportRejected:
                continue  # причины уже в журнале
            self._supersede(identity, employee.employee_id, reason=f"Заменено версией {version} с доступом к {system}")
            self._store.add_passport(employee_id=employee.employee_id, version=version, passport=passport,
                                     artifact=artifact, scorecard=scorecard)
            proposal = self._proposals.create_proposal(
                person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
                kind="passport",
                summary=f"Дать сотруднику «{employee.title}» доступ к системе {system}",
                subject={"employee_id": str(employee.employee_id), "version": version, "passport": passport,
                         "previous_version": employee.active_version, "diff": passport_diff(active, passport)},
                score_ref=scorecard["scorecard_id"],
            )
            self._store.set_passport_proposal(employee_id=employee.employee_id, version=version, proposal_id=proposal.id)
            proposal_ids.append(str(proposal.id))
        return proposal_ids

    # --- применение согласия ----------------------------------------------------------

    def on_passport_approved(self, identity: Registration, *, proposal_id: UUID, token: ConsentToken) -> dict[str, Any]:
        subject = self._proposals.apply_proposal(
            person_id=identity.person_id, harness_id=identity.harness_id, proposal_id=proposal_id, consent_token=token,
        )
        employee_id, version = UUID(subject["employee_id"]), int(subject["version"])
        stored = self._store.passport(employee_id=employee_id, version=version)
        if stored is None or stored["passport"] != subject["passport"] or stored["status"] != "proposed":
            raise ValueError("passport proposal does not match the stored version")
        self._check_scorecard(stored, identity.harness_id)
        self._store.activate(employee_id=employee_id, version=version)
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="employee.passport.activated", employee_id=employee_id,
            payload={"version": version, "proposal_id": str(proposal_id)},
            score_ref=stored["scorecard"]["scorecard_id"],
        )
        result: dict[str, Any] = {"employee_id": str(employee_id), "version": version, "status": "active"}
        connectors, _ = self._tools(identity.harness_id)
        missing = sorted(set(connectors) - set(subject["passport"]["tools"]["connectors"]))
        if missing and not self._store.pending_versions(employee_id=employee_id):
            # Системы подключили, пока паспорт ждал решения, — сразу предлагаем версию с ними (C-6).
            result["passport_proposals"] = self.propose_tool_extension(identity, system=", ".join(missing))
        return result

    # --- внутреннее --------------------------------------------------------------------

    def _active(self, employee) -> dict[str, Any] | None:
        if not employee.active_version:
            return None
        return self._store.passport(employee_id=employee.employee_id, version=employee.active_version)["passport"]

    def _base(self, employee) -> dict[str, Any] | None:
        """От чего строится новая версия: последняя ждущая решения (черновик, правка, расширение) или действующая."""
        pending = self._store.pending_versions(employee_id=employee.employee_id)
        return pending[0]["passport"] if pending else self._active(employee)

    def _supersede(self, identity: Registration, employee_id: UUID, *, reason: str) -> None:
        """Не больше одного ждущего предложения паспорта на ЦС (C-6): прежнее отзывается, версия — возвращена."""
        for pending in self._store.pending_versions(employee_id=employee_id):
            if pending["proposal_id"] is not None:
                self._proposals.withdraw(person_id=identity.person_id, harness_id=identity.harness_id,
                                         proposal_id=pending["proposal_id"], reason=reason)
            self._store.mark_returned(employee_id=employee_id, version=pending["version"])

    def _build(self, identity: Registration, *, employee_id: UUID, version: int, kind: str,
               draft: Mapping[str, Any], source: str, drafted_by: str) -> dict[str, Any]:
        connectors, scopes = self._tools(identity.harness_id)
        return build_passport(
            draft, employee_id=str(employee_id), version=version, kind=kind,
            harness_id=str(identity.harness_id), owner_person_id=str(identity.person_id),
            connectors=connectors, read_scopes=scopes, source=source, drafted_by=drafted_by,
            created_at=self._clock().isoformat(),
        )

    def _validate(self, identity: Registration, passport: Mapping[str, Any],
                  previous: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        connectors, scopes = self._tools(identity.harness_id)
        artifact: dict[str, Any] = {
            "passport": dict(passport),
            "curator_scopes": {"connectors": sorted(connectors), "read_scopes": sorted(scopes), "write_classes": []},
        }
        if previous is not None:
            artifact["previous_passport"] = dict(previous)
        context = {"company_id": str(identity.company_id), "harness_id": str(identity.harness_id),
                   "employee_id": passport["employee_id"]}
        try:
            scorecard = self._validator.validate(artifact, "passport", context)
        except ValidatorUnavailable as error:
            raise WorkforceUnavailable(str(error)) from error
        if scorecard.get("verdict") != "pass":
            reasons = [f"{c['name']}: {c.get('metrics')}" for c in scorecard.get("checks", []) if c.get("verdict") != "pass"]
            self._journal.append_entry(
                person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
                kind="employee.passport.rejected", payload={"reasons": reasons},
                score_ref=scorecard.get("scorecard_id"),
            )
            raise PassportRejected(reasons)
        return artifact, scorecard

    def _check_scorecard(self, stored: Mapping[str, Any], harness_id: UUID) -> None:
        """Перед применением — подпись ScoreCard на том же артефакте (C-4)."""
        for refresh in (False, True):
            try:
                keys = self._validator.keys(refresh=refresh)
            except ValidatorUnavailable as error:
                raise WorkforceUnavailable(str(error)) from error
            try:
                verify_scorecard(stored["scorecard"], keys, artifact=stored["artifact"], harness_id=harness_id)
                return
            except ScoreCardRejected as error:
                if error.code == "unknown_key" and not refresh:
                    continue
                raise ValueError(f"passport scorecard rejected: {error.code}") from error


def unmet_thresholds(metrics: Mapping[str, Any], thresholds: Mapping[str, Any]) -> list[str]:
    """Невыполненные пороги повышения (C-6): метрика неизвестна или ниже порога."""
    unmet = []
    for name, target in thresholds.items():
        value = metrics.get("decided" if name == "min_decided" else name)
        if value is None or value < target:
            unmet.append(name)
    return unmet
