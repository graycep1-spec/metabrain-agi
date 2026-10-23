"""Процессы, проекты, назначения сотрудников и задачи (C-6, планирование).

Всё принадлежит харнессу: выборки и изменения фильтруются по `company_id` и `harness_id`.
Процесс — повторяемое преобразование входов в выходы; проект — ограниченный сроком и
бюджетом результат. Сотрудник прикрепляется к процессу/проекту назначением, задачи
висят на назначении.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import Connection
from psycopg.types.json import Jsonb

from src.harness.journal import JournalStore
from src.harness.store import Registration
from src.workforce.store import migrate


PROCESS_STATUSES = frozenset({"active", "paused", "archived"})
PROJECT_STATUSES = frozenset({"planned", "active", "paused", "done"})
TASK_STATUSES = frozenset({"todo", "doing", "done", "blocked"})
TARGETS = frozenset({"process", "project"})


class PlanningError(ValueError):
    code = "invalid_planning_request"


class PlanningInvalid(PlanningError):
    code = "invalid_planning_request"


class PlanningNotFound(PlanningError):
    code = "not_found"


class PlanningConflict(PlanningError):
    code = "conflict"


def _text(value: Any, field: str, *, required: bool = True, limit: int = 2000) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PlanningInvalid(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise PlanningInvalid(f"{field} is required")
    if len(value) > limit:
        raise PlanningInvalid(f"{field} is too long")
    return value


def _text_list(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [line.strip() for line in value.replace(",", "\n").splitlines() if line.strip()]
    elif isinstance(value, tuple):
        value = list(value)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise PlanningInvalid(f"{field} must be a list of strings")
    return [item.strip()[:300] for item in value if item.strip()][:30]


def _date(value: Any, field: str) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise PlanningInvalid(f"{field} must be an ISO date") from error


def _money(value: Any) -> Decimal:
    if value is None or value == "":
        return Decimal("0")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise PlanningInvalid("budget_usd must be a number") from error
    if not amount.is_finite() or amount < 0:
        raise PlanningInvalid("budget_usd must be a non-negative number")
    return amount.quantize(Decimal("0.01"))


def _uuid(value: Any, field: str) -> UUID:
    try:
        return UUID(str(value))
    except (ValueError, TypeError) as error:
        raise PlanningInvalid(f"{field} must be a UUID") from error


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class PlanningService:
    def __init__(self, connection: Connection, *, journal: JournalStore,
                 clock: Any | None = None):
        self._c = connection
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        migrate(connection)

    # --- процессы ---------------------------------------------------------------------

    def list_processes(self, identity: Registration) -> list[dict[str, Any]]:
        rows = self._c.execute(
            """
            SELECT p.process_id, p.name, p.purpose, p.inputs, p.outputs, p.status,
                   p.created_at, p.updated_at,
                   (SELECT count(*) FROM wf_assignments a
                     WHERE a.target_type = 'process' AND a.target_id = p.process_id) AS assignments,
                   (SELECT count(*) FROM wf_tasks t
                     JOIN wf_assignments a ON a.assignment_id = t.assignment_id
                     WHERE a.target_type = 'process' AND a.target_id = p.process_id) AS tasks
            FROM wf_processes p
            WHERE p.harness_id = %s AND p.company_id = %s
            ORDER BY p.created_at DESC
            """,
            (identity.harness_id, identity.company_id),
        ).fetchall()
        return [self._process(row) for row in rows]

    def get_process(self, identity: Registration, process_id: Any) -> dict[str, Any]:
        process = self._process_by_id(identity, _uuid(process_id, "process_id"))
        process["assignments"] = self._assignments(identity, "process", process["process_id"])
        return process

    def create_process(self, identity: Registration, *, name: Any, purpose: Any = "",
                       inputs: Any = None, outputs: Any = None, status: Any = "active") -> dict[str, Any]:
        name = _text(name, "name", limit=200)
        purpose = _text(purpose, "purpose", required=False, limit=2000)
        if status not in PROCESS_STATUSES:
            raise PlanningInvalid("status must be active, paused or archived")
        process_id = uuid4()
        now = self._clock()
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO wf_processes
                    (process_id, company_id, harness_id, name, purpose, inputs, outputs, status,
                     created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (process_id, identity.company_id, identity.harness_id, name, purpose,
                 Jsonb(_text_list(inputs, "inputs")), Jsonb(_text_list(outputs, "outputs")),
                 status, now, now),
            )
        self._record(identity, "planning.process.created",
                     {"process_id": str(process_id), "name": name, "status": status})
        return self._process_by_id(identity, process_id, with_counts=False)

    # --- проекты ----------------------------------------------------------------------

    def list_projects(self, identity: Registration) -> list[dict[str, Any]]:
        rows = self._c.execute(
            """
            SELECT p.project_id, p.name, p.goal, p.starts_on, p.ends_on, p.budget_usd,
                   p.status, p.result, p.created_at, p.updated_at,
                   (SELECT count(*) FROM wf_assignments a
                     WHERE a.target_type = 'project' AND a.target_id = p.project_id) AS assignments,
                   (SELECT count(*) FROM wf_tasks t
                     JOIN wf_assignments a ON a.assignment_id = t.assignment_id
                     WHERE a.target_type = 'project' AND a.target_id = p.project_id) AS tasks
            FROM wf_projects p
            WHERE p.harness_id = %s AND p.company_id = %s
            ORDER BY p.created_at DESC
            """,
            (identity.harness_id, identity.company_id),
        ).fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, identity: Registration, project_id: Any) -> dict[str, Any]:
        project = self._project_by_id(identity, _uuid(project_id, "project_id"))
        project["assignments"] = self._assignments(identity, "project", project["project_id"])
        return project

    def create_project(self, identity: Registration, *, name: Any, goal: Any = "",
                       starts_on: Any = None, ends_on: Any = None, budget_usd: Any = 0,
                       status: Any = "planned", result: Any = "") -> dict[str, Any]:
        name = _text(name, "name", limit=200)
        goal = _text(goal, "goal", required=False, limit=2000)
        result = _text(result, "result", required=False, limit=2000)
        if status not in PROJECT_STATUSES:
            raise PlanningInvalid("status must be planned, active, paused or done")
        start, end = _date(starts_on, "starts_on"), _date(ends_on, "ends_on")
        if start and end and end < start:
            raise PlanningInvalid("ends_on must be on or after starts_on")
        project_id = uuid4()
        now = self._clock()
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO wf_projects
                    (project_id, company_id, harness_id, name, goal, starts_on, ends_on,
                     budget_usd, status, result, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (project_id, identity.company_id, identity.harness_id, name, goal, start, end,
                 _money(budget_usd), status, result, now, now),
            )
        self._record(identity, "planning.project.created",
                     {"project_id": str(project_id), "name": name, "status": status})
        return self._project_by_id(identity, project_id, with_counts=False)

    # --- назначения и задачи ------------------------------------------------------------

    def assign(self, identity: Registration, *, employee_id: Any, target_type: Any,
               target_id: Any, role: Any = "") -> dict[str, Any]:
        employee = self._employee(identity, _uuid(employee_id, "employee_id"))
        target = self._target(identity, target_type, _uuid(target_id, "target_id"))
        role = _text(role, "role", required=False, limit=200)
        assignment_id = uuid4()
        with self._c.transaction():
            try:
                self._c.execute(
                    """
                    INSERT INTO wf_assignments
                        (assignment_id, company_id, harness_id, employee_id, target_type,
                         target_id, role, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (assignment_id, identity.company_id, identity.harness_id, employee["employee_id"],
                     target_type, target["id"], role, self._clock()),
                )
            except psycopg.errors.UniqueViolation as error:
                raise PlanningConflict("employee_already_assigned") from error
        self._record(identity, "planning.assignment.created",
                     {"assignment_id": str(assignment_id), "employee_id": str(employee["employee_id"]),
                      "target_type": target_type, "target_id": str(target["id"]), "role": role})
        return self._assignment_by_id(identity, assignment_id)

    def list_tasks(self, identity: Registration, *, assignment_id: Any = None,
                   status: Any = None) -> list[dict[str, Any]]:
        clauses = ["t.harness_id = %s", "t.company_id = %s"]
        params: list[Any] = [identity.harness_id, identity.company_id]
        if assignment_id is not None:
            clauses.append("t.assignment_id = %s")
            params.append(_uuid(assignment_id, "assignment_id"))
        if status is not None:
            if status not in TASK_STATUSES:
                raise PlanningInvalid("unknown task status")
            clauses.append("t.status = %s")
            params.append(status)
        rows = self._c.execute(
            f"""
            SELECT t.task_id, t.assignment_id, t.employee_id, t.title, t.description,
                   t.status, t.due_at, t.created_at, t.updated_at,
                   e.title, a.target_type, a.target_id,
                   COALESCE(p.name, pr.name) AS target_name
            FROM wf_tasks t
            JOIN wf_assignments a ON a.assignment_id = t.assignment_id
            JOIN wf_employees e ON e.employee_id = t.employee_id
            LEFT JOIN wf_processes p ON a.target_type = 'process' AND p.process_id = a.target_id
            LEFT JOIN wf_projects pr ON a.target_type = 'project' AND pr.project_id = a.target_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.created_at DESC
            """,
            params,
        ).fetchall()
        return [self._task(row) for row in rows]

    def create_task(self, identity: Registration, *, assignment_id: Any, title: Any,
                    description: Any = "", due_at: Any = None) -> dict[str, Any]:
        assignment_uuid = _uuid(assignment_id, "assignment_id")
        assignment = self._assignment_by_id(identity, assignment_uuid)
        employee_uuid = _uuid(assignment["employee_id"], "employee_id")
        title = _text(title, "title", limit=300)
        description = _text(description, "description", required=False, limit=2000)
        due = None
        if due_at not in (None, ""):
            try:
                due = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
            except ValueError as error:
                raise PlanningInvalid("due_at must be an ISO datetime") from error
            if due.tzinfo is None:
                raise PlanningInvalid("due_at must include a timezone")
        task_id = uuid4()
        now = self._clock()
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO wf_tasks
                    (task_id, company_id, harness_id, assignment_id, employee_id, title,
                     description, status, due_at, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'todo', %s, %s, %s)
                """,
                (task_id, identity.company_id, identity.harness_id, assignment_uuid,
                 employee_uuid, title, description, due, now, now),
            )
        self._record(identity, "planning.task.created",
                     {"task_id": str(task_id), "assignment_id": str(assignment_uuid),
                      "title": title})
        return self._task_by_id(identity, task_id)

    def update_task(self, identity: Registration, *, task_id: Any, status: Any = None,
                    title: Any = None, description: Any = None, due_at: Any = None) -> dict[str, Any]:
        task_uuid = _uuid(task_id, "task_id")
        self._task_by_id(identity, task_uuid)
        changes: dict[str, Any] = {}
        if status is not None:
            if status not in TASK_STATUSES:
                raise PlanningInvalid("unknown task status")
            changes["status"] = status
        if title is not None:
            changes["title"] = _text(title, "title", limit=300)
        if description is not None:
            changes["description"] = _text(description, "description", required=False, limit=2000)
        if due_at is not None:
            try:
                due = datetime.fromisoformat(str(due_at).replace("Z", "+00:00"))
            except ValueError as error:
                raise PlanningInvalid("due_at must be an ISO datetime") from error
            if due.tzinfo is None:
                raise PlanningInvalid("due_at must include a timezone")
            changes["due_at"] = due
        if not changes:
            raise PlanningInvalid("nothing to update")
        changes["updated_at"] = self._clock()
        assignments = ", ".join(f"{key} = %s" for key in changes)
        with self._c.transaction():
            self._c.execute(
                f"UPDATE wf_tasks SET {assignments} WHERE task_id = %s AND harness_id = %s AND company_id = %s",
                (*changes.values(), task_uuid, identity.harness_id, identity.company_id),
            )
        self._record(identity, "planning.task.updated", {"task_id": str(task_uuid), "fields": sorted(changes)})
        return self._task_by_id(identity, task_uuid)

    # --- примеры и сводка ---------------------------------------------------------------

    def summary(self, identity: Registration) -> dict[str, int]:
        row = self._c.execute(
            """
            SELECT
              (SELECT count(*) FROM wf_processes WHERE harness_id = %s AND company_id = %s),
              (SELECT count(*) FROM wf_projects WHERE harness_id = %s AND company_id = %s),
              (SELECT count(*) FROM wf_assignments WHERE harness_id = %s AND company_id = %s),
              (SELECT count(*) FROM wf_tasks WHERE harness_id = %s AND company_id = %s)
            """,
            (identity.harness_id, identity.company_id) * 4,
        ).fetchone()
        return {"processes": row[0], "projects": row[1], "assignments": row[2], "tasks": row[3]}

    def seed_examples(self, identity: Registration) -> dict[str, int]:
        """Идемпотентно наполняет пустой харнесс примерами процессов/проектов и задач."""
        current = self.summary(identity)
        if current["processes"] or current["projects"]:
            return current
        process_mail = self.create_process(
            identity, name="Разбор входящей почты",
            purpose="Превращать письма в суть, поручения и следующие шаги.",
            inputs=["входящие письма"], outputs=["суть", "поручения", "следующие шаги"],
        )
        process_quote = self.create_process(
            identity, name="Подготовка КП по заявке",
            purpose="Собрать заявку, склад и цены в проверяемый черновик КП.",
            inputs=["заявка", "склад", "прайс"], outputs=["черновик КП", "источники"],
        )
        project = self.create_project(
            identity, name="Пилот MetaBrain в продажах",
            goal="Сократить время подготовки КП и проверить навык на реальных заявках.",
            starts_on=date.today(), ends_on=date.today() + timedelta(days=30),
            budget_usd=500, status="active",
        )
        employee = self._c.execute(
            "SELECT employee_id FROM wf_employees WHERE harness_id = %s AND company_id = %s "
            "AND kind <> 'researcher' ORDER BY created_at LIMIT 1",
            (identity.harness_id, identity.company_id),
        ).fetchone()
        tasks = 0
        if employee:
            mail = self.assign(identity, employee_id=employee[0], target_type="process",
                               target_id=process_mail["process_id"], role="исполнитель")
            quote = self.assign(identity, employee_id=employee[0], target_type="process",
                                target_id=process_quote["process_id"], role="исполнитель")
            pilot = self.assign(identity, employee_id=employee[0], target_type="project",
                                target_id=project["project_id"], role="владелец")
            for assignment, title in ((mail, "Разобрать 10 входящих писем"),
                                      (quote, "Подготовить КП по заявке 1042"),
                                      (pilot, "Согласовать план пилота")):
                self.create_task(identity, assignment_id=assignment["assignment_id"], title=title)
                tasks += 1
        return {"processes": 2, "projects": 1, "assignments": 3 if employee else 0, "tasks": tasks}

    # --- внутреннее ---------------------------------------------------------------------

    def _record(self, identity: Registration, kind: str, payload: Mapping[str, Any]) -> None:
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind=kind, payload=dict(payload),
        )

    def _employee(self, identity: Registration, employee_id: UUID) -> dict[str, Any]:
        row = self._c.execute(
            "SELECT employee_id, title, kind, status FROM wf_employees "
            "WHERE employee_id = %s AND harness_id = %s AND company_id = %s",
            (employee_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound("employee_not_found")
        return {"employee_id": row[0], "title": row[1], "kind": row[2], "status": row[3]}

    def _target(self, identity: Registration, target_type: Any, target_id: UUID) -> dict[str, Any]:
        if target_type not in TARGETS:
            raise PlanningInvalid("target_type must be process or project")
        table, id_column = (("wf_processes", "process_id") if target_type == "process"
                            else ("wf_projects", "project_id"))
        row = self._c.execute(
            f"SELECT {id_column}, name FROM {table} "
            "WHERE " + id_column + " = %s AND harness_id = %s AND company_id = %s",
            (target_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound(f"{target_type}_not_found")
        return {"id": row[0], "name": row[1]}

    def _process_by_id(self, identity: Registration, process_id: UUID,
                       *, with_counts: bool = True) -> dict[str, Any]:
        row = self._c.execute(
            """
            SELECT process_id, name, purpose, inputs, outputs, status, created_at, updated_at
            FROM wf_processes WHERE process_id = %s AND harness_id = %s AND company_id = %s
            """,
            (process_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound("process_not_found")
        return self._process(row, with_counts=with_counts)

    def _project_by_id(self, identity: Registration, project_id: UUID,
                       *, with_counts: bool = True) -> dict[str, Any]:
        row = self._c.execute(
            """
            SELECT project_id, name, goal, starts_on, ends_on, budget_usd, status, result,
                   created_at, updated_at
            FROM wf_projects WHERE project_id = %s AND harness_id = %s AND company_id = %s
            """,
            (project_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound("project_not_found")
        return self._project(row, with_counts=with_counts)

    def _assignments(self, identity: Registration, target_type: str, target_id: Any) -> list[dict[str, Any]]:
        rows = self._c.execute(
            """
            SELECT a.assignment_id, a.employee_id, a.role, a.created_at,
                   e.title, e.kind, e.status
            FROM wf_assignments a
            JOIN wf_employees e ON e.employee_id = a.employee_id
            WHERE a.target_type = %s AND a.target_id = %s AND a.harness_id = %s AND a.company_id = %s
            ORDER BY a.created_at
            """,
            (target_type, target_id, identity.harness_id, identity.company_id),
        ).fetchall()
        result = []
        for row in rows:
            tasks = self._c.execute(
                "SELECT task_id, title, description, status, due_at, created_at, updated_at "
                "FROM wf_tasks WHERE assignment_id = %s ORDER BY created_at",
                (row[0],),
            ).fetchall()
            result.append({
                "assignment_id": str(row[0]), "employee_id": str(row[1]), "role": row[2],
                "created_at": _iso(row[3]), "employee_title": row[4], "employee_kind": row[5],
                "employee_status": row[6],
                "tasks": [{"task_id": str(t[0]), "title": t[1], "description": t[2], "status": t[3],
                           "due_at": t[4].isoformat() if t[4] else None,
                           "created_at": _iso(t[5]), "updated_at": _iso(t[6])} for t in tasks],
            })
        return result

    def _assignment_by_id(self, identity: Registration, assignment_id: UUID) -> dict[str, Any]:
        row = self._c.execute(
            """
            SELECT a.assignment_id, a.employee_id, a.role, a.target_type, a.target_id, a.created_at,
                   e.title, e.kind, e.status
            FROM wf_assignments a JOIN wf_employees e ON e.employee_id = a.employee_id
            WHERE a.assignment_id = %s AND a.harness_id = %s AND a.company_id = %s
            """,
            (assignment_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound("assignment_not_found")
        return {"assignment_id": str(row[0]), "employee_id": str(row[1]), "role": row[2],
                "target_type": row[3], "target_id": str(row[4]), "created_at": _iso(row[5]),
                "employee_title": row[6], "employee_kind": row[7], "employee_status": row[8]}

    def _task_by_id(self, identity: Registration, task_id: UUID) -> dict[str, Any]:
        row = self._c.execute(
            """
            SELECT t.task_id, t.assignment_id, t.employee_id, t.title, t.description, t.status,
                   t.due_at, t.created_at, t.updated_at, e.title, a.target_type, a.target_id,
                   COALESCE(p.name, pr.name)
            FROM wf_tasks t
            JOIN wf_assignments a ON a.assignment_id = t.assignment_id
            JOIN wf_employees e ON e.employee_id = t.employee_id
            LEFT JOIN wf_processes p ON a.target_type = 'process' AND p.process_id = a.target_id
            LEFT JOIN wf_projects pr ON a.target_type = 'project' AND pr.project_id = a.target_id
            WHERE t.task_id = %s AND t.harness_id = %s AND t.company_id = %s
            """,
            (task_id, identity.harness_id, identity.company_id),
        ).fetchone()
        if row is None:
            raise PlanningNotFound("task_not_found")
        return self._task(row)

    @staticmethod
    def _process(row: Any, *, with_counts: bool = True) -> dict[str, Any]:
        result = {
            "process_id": str(row[0]), "name": row[1], "purpose": row[2],
            "inputs": row[3] or [], "outputs": row[4] or [], "status": row[5],
            "created_at": _iso(row[6]), "updated_at": _iso(row[7]),
        }
        if len(row) > 8:
            result.update({"assignments": row[8], "tasks": row[9]})
        return result

    @staticmethod
    def _project(row: Any, *, with_counts: bool = True) -> dict[str, Any]:
        result = {
            "project_id": str(row[0]), "name": row[1], "goal": row[2],
            "starts_on": row[3].isoformat() if row[3] else None,
            "ends_on": row[4].isoformat() if row[4] else None,
            "budget_usd": float(row[5]), "status": row[6], "result": row[7],
            "created_at": _iso(row[8]), "updated_at": _iso(row[9]),
        }
        if len(row) > 10:
            result.update({"assignments": row[10], "tasks": row[11]})
        return result

    @staticmethod
    def _task(row: Any) -> dict[str, Any]:
        return {
            "task_id": str(row[0]), "assignment_id": str(row[1]), "employee_id": str(row[2]),
            "title": row[3], "description": row[4], "status": row[5],
            "due_at": row[6].isoformat() if row[6] else None,
            "created_at": _iso(row[7]), "updated_at": _iso(row[8]),
            "employee_title": row[9], "target_type": row[10], "target_id": str(row[11]),
            "target_name": row[12],
        }
