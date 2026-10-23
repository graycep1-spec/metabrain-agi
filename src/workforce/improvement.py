"""T-37 · Модуль самосовершенствования (ADR-029): отладчик читает телеметрию, предлагает улучшения.

Петля над C-006, не автономная самоправка:
находки телеметрии → саброль `debugger` ставит диагноз и драфтит патч паспорта →
валидатор (профиль Passport) → предложение `self_improvement` куратору → согласие →
применение штатным механизмом версий паспорта. Код сервиса и собственные права ЦС
модуль не меняет (C-010); телеметрия наружу не уходит.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.store import Registration
from src.llm.connector import ChatRequest, LLMConnector, LLMContext
from src.workforce.service import WorkforceService
from src.workforce.swarm import _json


WINDOW = timedelta(days=7)
MIN_REPEATS = 2
MAX_PROPOSALS_PER_RUN = 2

DEBUGGER_PROMPT = """Ты — отладчик цифрового сотрудника «{title}». Телеметрия показывает проблему:
{finding}

Паспорт сотрудника сейчас (редактируемые секции):
{form}

Журнал последних событий:
{digest}

Поставь диагноз и предложи ОДНО улучшение паспорта, которое чинит причину.
Ответь ТОЛЬКО JSON: {{"diagnosis": "...", "patch": {{...}}}}.
В patch — только изменяемые секции из паспорта выше (duties, goals, decision_bounds,
escalation, schedule, role) в том же формате, целиком заменяя секцию. Чаще всего чинит:
escalation (когда спрашивать куратора), schedule (cron 5 полей UTC, duty — номер
обязанности с 1), duties. Пиши по-русски, без имён, идентификаторов и секретов."""


class ImprovementFlow:
    def __init__(self, connection: Connection, *, connector: LLMConnector,
                 workforce: WorkforceService, journal: JournalStore,
                 clock: Callable[[], datetime] | None = None):
        self._c = connection
        self._connector = connector
        self._workforce = workforce
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- находки телеметрии (детерминированно, без LLM) --------------------------------

    def scan(self, identity: Registration) -> list[dict[str, Any]]:
        """Паттерны проблем за окно: повторные падения работ и заблокированные ответы (GT T-37)."""
        since = self._clock() - WINDOW
        findings: list[dict[str, Any]] = []
        with self._c.transaction():
            failed = self._c.execute(
                """
                SELECT j.employee_id, e.title, j.duty_id, split_part(coalesce(j.error, ''), ':', 1), count(*)
                FROM wf_jobs j JOIN wf_employees e ON e.employee_id = j.employee_id
                WHERE j.harness_id = %s AND j.status = 'failed' AND j.created_at > %s
                  AND e.kind <> 'researcher' AND coalesce(j.input->>'diagnosis', '') <> 'true'
                GROUP BY j.employee_id, e.title, j.duty_id, split_part(coalesce(j.error, ''), ':', 1)
                HAVING count(*) >= %s
                """,
                (identity.harness_id, since, MIN_REPEATS),
            ).fetchall()
            blocked = self._c.execute(
                """
                SELECT j.employee_id, e.title, count(*)
                FROM journal_entries j JOIN wf_employees e ON e.employee_id = j.employee_id
                WHERE j.harness_id = %s AND j.kind = 'answer.blocked' AND j.created_at > %s
                GROUP BY j.employee_id, e.title
                HAVING count(*) >= %s
                """,
                (identity.harness_id, since, MIN_REPEATS),
            ).fetchall()
        for employee_id, title, duty, error, count in failed:
            findings.append({
                "kind": "failed_jobs", "employee_id": str(employee_id), "title": title,
                "detail": f"обязанность «{duty}» падает повторно ({count}×, ошибка {error or 'unknown'})",
                "count": count,
            })
        for employee_id, title, count in blocked:
            findings.append({
                "kind": "answers_blocked", "employee_id": str(employee_id), "title": title,
                "detail": f"ответы блокируются валидатором повторно ({count}×)", "count": count,
            })
        return sorted(findings, key=lambda f: -f["count"])

    # --- запуск петли -------------------------------------------------------------------

    def run(self, identity: Registration) -> dict[str, Any]:
        """Находки → драфт улучшения отладчиком → предложение куратору. Не больше
        MAX_PROPOSALS_PER_RUN за запуск; сотрудник с ожидающим `self_improvement` пропускается."""
        findings = self.scan(identity)
        proposals, skipped = [], []
        for finding in findings:
            if len(proposals) >= MAX_PROPOSALS_PER_RUN:
                break
            employee_id = UUID(finding["employee_id"])
            if self._has_pending(identity, employee_id):
                skipped.append({**finding, "reason": "already_pending"})
                continue
            result = self._propose(identity, employee_id, finding)
            (proposals if result.get("proposal_id") else skipped).append({**finding, **result})
        return {"findings": findings, "proposals": proposals, "skipped": skipped}

    def _has_pending(self, identity: Registration, employee_id: UUID) -> bool:
        row = self._c.execute(
            "SELECT 1 FROM proposals WHERE harness_id = %s AND kind = 'self_improvement' "
            "AND status = 'pending' AND subject->>'employee_id' = %s LIMIT 1",
            (identity.harness_id, str(employee_id)),
        ).fetchone()
        return row is not None

    def _propose(self, identity: Registration, employee_id: UUID, finding: Mapping[str, Any]) -> dict[str, Any]:
        from src.workforce.passport import draft_fields
        import json

        employee = self._workforce.get(identity, employee_id=employee_id)
        passport = employee.get("passport")
        if not passport:
            return {"status": "skipped", "reason": "no_passport"}
        form = {k: v for k, v in draft_fields(passport).items()}
        page = self._journal.list_entries(person_id=identity.person_id, harness_id=identity.harness_id,
                                          employee_id=employee_id, page_size=12)
        from src.workforce import diagnosis as dg
        context = LLMContext(company_id=str(identity.company_id), harness_id=str(identity.harness_id),
                             employee_id=str(employee_id), job_id=f"self-improvement:{employee_id}",
                             sub_role="debugger")
        result = self._connector.chat_completions(context, ChatRequest(messages=[{
            "role": "user",
            "content": DEBUGGER_PROMPT.format(title=finding.get("title", "Сотрудник"),
                                              finding=finding["detail"],
                                              form=json.dumps(form, ensure_ascii=False),
                                              digest=dg.digest(page.entries)),
        }]))
        try:
            raw = _json(result.response["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as error:
            raise ValueError("debugger answer is malformed") from error
        if not isinstance(raw, Mapping) or not isinstance(raw.get("patch"), Mapping) or not raw["patch"]:
            return {"status": "skipped", "reason": "no_patch"}
        diagnosis_text = (raw.get("diagnosis") or "").strip()[:500] if isinstance(raw.get("diagnosis"), str) else ""
        try:
            proposed = self._workforce.propose_edit(
                identity, employee_id=employee_id, patch=dict(raw["patch"]),
                kind="self_improvement",
                summary=f"Самосовершенствование: «{finding.get('title', '')}» — {diagnosis_text[:90] or finding['detail'][:90]}",
                extra_subject={"diagnosis": diagnosis_text, "finding": dict(finding)},
                drafted_by="debugger",
            )
        except ValueError as error:   # кривой патч отладчика — пропускаем, не роняя запуск
            return {"status": "skipped", "reason": f"invalid_patch: {error}"[:200]}
        if proposed.get("proposal_id"):
            self._journal.append_entry(
                person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
                kind="self_improvement.proposed", employee_id=employee_id,
                payload={"proposal_id": proposed["proposal_id"], "finding": dict(finding),
                         "diagnosis": diagnosis_text},
            )
        return proposed
