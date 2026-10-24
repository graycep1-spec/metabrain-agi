"""Преднастроенные сценарии (ADR-036): роль + процессы + прикрепления + первые задачи одной кнопкой.

Flow связывает три части кабинета в одну работающую цепочку:
роль из каталога (через валидатор и гейт куратора) → процессы из каталога →
прикрепление сотрудника к каждому процессу → первые задачи.

Дальше цепочка замыкается сама: сотрудник работает → телеметрия копится →
улучшатель (T-37) находит повторяющиеся проблемы и приносит улучшение куратору.
Разворачивание идемпотентно: процесс с таким же именем переиспользуется, а не дублируется.
"""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.store import Registration
from src.workforce.catalog import flow_template, process_template, role_template
from src.workforce.planning import PlanningConflict, PlanningService
from src.workforce.service import WorkforceService


class FlowService:
    def __init__(self, connection: Connection, *, workforce: WorkforceService,
                 planning: PlanningService, journal: JournalStore,
                 clock: Callable[[], datetime] | None = None):
        self._c = connection
        self._workforce = workforce
        self._planning = planning
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def start(self, identity: Registration, *, template_id: str,
              org: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Развернуть сценарий. Паспорт роли уходит куратору предложением — работа начнётся после согласия."""
        flow = flow_template(template_id)
        if flow is None:
            raise ValueError(f"unknown flow template: {template_id!r}")

        drafted = self._workforce.draft_from_template(identity, template_id=flow["role"], org=org)
        if not drafted.get("proposal_id"):
            return {"status": "rejected", "reasons": drafted.get("reasons", []), "flow": flow["id"]}
        employee_id = UUID(drafted["employee_id"])

        existing = {p["name"]: p for p in self._planning.list_processes(identity)}
        processes, assignments, tasks = [], [], []
        for step in flow["steps"]:
            template = process_template(step["process"])
            if template is None:
                continue
            reused = template["name"] in existing
            process = existing.get(template["name"]) or self._planning.create_process(
                identity, name=template["name"], purpose=template["purpose"],
                inputs=template["inputs"], outputs=template["outputs"],
            )
            existing[template["name"]] = process
            processes.append({"process_id": str(process["process_id"]), "name": template["name"],
                              "reused": reused})
            try:
                assignment = self._planning.assign(
                    identity, employee_id=employee_id, target_type="process",
                    target_id=process["process_id"], role=step.get("role_in_process", "исполнитель"),
                )
            except PlanningConflict:
                continue   # сотрудник уже прикреплён к этому процессу
            assignments.append(str(assignment["assignment_id"]))
            for title in step["tasks"]:
                task = self._planning.create_task(
                    identity, assignment_id=assignment["assignment_id"], title=title)
                tasks.append(str(task["task_id"]))

        result = {
            "status": "started", "flow": flow["id"], "name": flow["name"],
            "employee_id": str(employee_id), "proposal_id": drafted["proposal_id"],
            "role_title": (role_template(flow["role"]) or {}).get("title", flow["role"]),
            "processes": [p["name"] for p in processes],
            "assignments": len(assignments), "tasks": len(tasks),
        }
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="flow.started", employee_id=employee_id,
            payload={"flow": flow["id"], "name": flow["name"], "proposal_id": drafted["proposal_id"],
                     "processes": result["processes"], "tasks": len(tasks)},
        )
        return result

    def status(self, identity: Registration) -> list[dict[str, Any]]:
        """Развёрнутые сценарии и состояние их петли: ждёт ли гейта, что нашёл улучшатель."""
        with self._c.transaction():
            rows = self._c.execute(
                """
                SELECT j.payload->>'flow', j.payload->>'name', j.employee_id,
                       j.payload->>'proposal_id', max(j.created_at)
                FROM journal_entries j
                WHERE j.harness_id = %s AND j.kind = 'flow.started'
                GROUP BY 1, 2, 3, 4
                ORDER BY max(j.created_at) DESC
                """,
                (identity.harness_id,),
            ).fetchall()
        started = []
        for flow_id, name, employee_id, proposal_id, at in rows:
            with self._c.transaction():
                employee = self._c.execute(
                    "SELECT title, status FROM wf_employees WHERE employee_id = %s AND harness_id = %s",
                    (employee_id, identity.harness_id),
                ).fetchone()
                pending = self._c.execute(
                    "SELECT count(*) FROM proposals WHERE harness_id = %s AND status = 'pending' "
                    "AND subject->>'employee_id' = %s",
                    (identity.harness_id, str(employee_id)),
                ).fetchone()[0]
                improvements = self._c.execute(
                    "SELECT count(*) FROM journal_entries WHERE harness_id = %s AND employee_id = %s "
                    "AND kind = 'self_improvement.proposed'",
                    (identity.harness_id, employee_id),
                ).fetchone()[0]
            if employee is None:
                continue
            started.append({
                "flow": flow_id, "name": name, "employee_id": str(employee_id),
                "role_title": employee[0], "employee_status": employee[1],
                "proposal_id": proposal_id, "pending": pending, "improvements": improvements,
                "started_at": at.isoformat(),
            })
        return started
