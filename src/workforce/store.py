"""Postgres-хранилище цифровых сотрудников и работ (C-6, ADR-024)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
OPEN_STATUSES = ("queued", "running", "waiting_curator")


def migrate(connection: Connection) -> None:
    with connection.transaction():
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"), prepare=False)


@dataclass(frozen=True)
class Employee:
    employee_id: UUID
    company_id: UUID
    harness_id: UUID
    kind: str
    title: str
    status: str
    active_version: int | None


@dataclass(frozen=True)
class Job:
    job_id: UUID
    company_id: UUID
    harness_id: UUID
    employee_id: UUID
    duty_id: str
    trigger: str
    slot: datetime | None
    input: dict[str, Any]
    attempts: int


EMPLOYEE_COLUMNS = "employee_id, company_id, harness_id, kind, title, status, active_version"
JOB_COLUMNS = "job_id, company_id, harness_id, employee_id, duty_id, trigger, slot, input, attempts"


class WorkforceStore:
    def __init__(self, connection: Connection):
        self._c = connection

    # --- сотрудники и паспорта ---------------------------------------------------

    def ensure_researcher(self, *, company_id: UUID, harness_id: UUID) -> Employee:
        self._c.execute(
            """
            INSERT INTO wf_employees (employee_id, company_id, harness_id, kind, title, status)
            VALUES (%s, %s, %s, 'researcher', 'Исследователь', 'active')
            ON CONFLICT (harness_id) WHERE kind = 'researcher' DO NOTHING
            """,
            (uuid4(), company_id, harness_id),
        )
        row = self._c.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM wf_employees WHERE harness_id = %s AND kind = 'researcher'",
            (harness_id,),
        ).fetchone()
        return Employee(*row)

    def create_employee(self, *, employee_id: UUID, company_id: UUID, harness_id: UUID, kind: str, title: str) -> None:
        self._c.execute(
            """
            INSERT INTO wf_employees (employee_id, company_id, harness_id, kind, title, status)
            VALUES (%s, %s, %s, %s, %s, 'draft')
            """,
            (employee_id, company_id, harness_id, kind, title),
        )

    def get_employee(self, *, employee_id: UUID, harness_id: UUID) -> Employee | None:
        row = self._c.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM wf_employees WHERE employee_id = %s AND harness_id = %s",
            (employee_id, harness_id),
        ).fetchone()
        return Employee(*row) if row else None

    def list_employees(self, *, harness_id: UUID) -> list[Employee]:
        rows = self._c.execute(
            f"SELECT {EMPLOYEE_COLUMNS} FROM wf_employees WHERE harness_id = %s ORDER BY created_at, employee_id",
            (harness_id,),
        ).fetchall()
        return [Employee(*row) for row in rows]

    def add_passport(self, *, employee_id: UUID, version: int, passport: Mapping[str, Any],
                     artifact: Mapping[str, Any], scorecard: Mapping[str, Any]) -> None:
        self._c.execute(
            """
            INSERT INTO wf_passports (employee_id, version, passport, artifact, scorecard, status)
            VALUES (%s, %s, %s, %s, %s, 'proposed')
            """,
            (employee_id, version, Jsonb(dict(passport)), Jsonb(dict(artifact)), Jsonb(dict(scorecard))),
        )

    def set_passport_proposal(self, *, employee_id: UUID, version: int, proposal_id: UUID) -> None:
        self._c.execute(
            "UPDATE wf_passports SET proposal_id = %s WHERE employee_id = %s AND version = %s",
            (proposal_id, employee_id, version),
        )

    def passport(self, *, employee_id: UUID, version: int) -> dict[str, Any] | None:
        row = self._c.execute(
            """
            SELECT passport, artifact, scorecard, status, proposal_id
            FROM wf_passports WHERE employee_id = %s AND version = %s
            """,
            (employee_id, version),
        ).fetchone()
        if row is None:
            return None
        return {"passport": row[0], "artifact": row[1], "scorecard": row[2], "status": row[3], "proposal_id": row[4]}

    def versions(self, *, employee_id: UUID) -> list[dict[str, Any]]:
        rows = self._c.execute(
            """
            SELECT version, status, proposal_id, created_at, scorecard->>'scorecard_id'
            FROM wf_passports WHERE employee_id = %s ORDER BY version DESC
            """,
            (employee_id,),
        ).fetchall()
        return [{"version": v, "status": s, "proposal_id": str(p) if p else None, "created_at": c.isoformat(),
                 "score_ref": ref} for v, s, p, c, ref in rows]

    def pending_versions(self, *, employee_id: UUID) -> list[dict[str, Any]]:
        """Версии, ждущие решения куратора, — новые первыми."""
        rows = self._c.execute(
            """
            SELECT version, passport, proposal_id FROM wf_passports
            WHERE employee_id = %s AND status = 'proposed' ORDER BY version DESC
            """,
            (employee_id,),
        ).fetchall()
        return [{"version": v, "passport": p, "proposal_id": pid} for v, p, pid in rows]

    def next_version(self, employee_id: UUID) -> int:
        row = self._c.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM wf_passports WHERE employee_id = %s", (employee_id,)
        ).fetchone()
        return int(row[0])

    def activate(self, *, employee_id: UUID, version: int) -> None:
        self._c.execute(
            "UPDATE wf_passports SET status = 'superseded' WHERE employee_id = %s AND status = 'active'",
            (employee_id,),
        )
        self._c.execute(
            "UPDATE wf_passports SET status = 'active' WHERE employee_id = %s AND version = %s",
            (employee_id, version),
        )
        self._c.execute(
            """
            UPDATE wf_employees
            SET status = CASE WHEN status = 'paused' THEN 'paused' ELSE 'active' END,  -- новая версия не снимает паузу
                active_version = %s, updated_at = clock_timestamp()
            WHERE employee_id = %s
            """,
            (version, employee_id),
        )

    def set_status(self, *, employee_id: UUID, harness_id: UUID, source: str, target: str) -> bool:
        row = self._c.execute(
            """
            UPDATE wf_employees SET status = %s, updated_at = clock_timestamp()
            WHERE employee_id = %s AND harness_id = %s AND status = %s
            RETURNING employee_id
            """,
            (target, employee_id, harness_id, source),
        ).fetchone()
        return row is not None

    def mark_returned(self, *, employee_id: UUID, version: int) -> None:
        self._c.execute(
            "UPDATE wf_passports SET status = 'returned' WHERE employee_id = %s AND version = %s AND status = 'proposed'",
            (employee_id, version),
        )

    def scheduled_employees(self) -> list[tuple[Employee, dict[str, Any]]]:
        """Активные ЦС с активным паспортом — кандидаты для планировщика."""
        rows = self._c.execute(
            f"""
            SELECT {", ".join("e." + c.strip() for c in EMPLOYEE_COLUMNS.split(","))}, p.passport
            FROM wf_employees e
            JOIN wf_passports p ON p.employee_id = e.employee_id AND p.version = e.active_version AND p.status = 'active'
            WHERE e.status = 'active' AND e.kind <> 'researcher'
            """
        ).fetchall()
        return [(Employee(*row[:7]), row[7]) for row in rows]

    # --- работы ------------------------------------------------------------------

    def enqueue(self, *, employee: Employee, duty_id: str, trigger: str, slot: datetime | None = None,
                input: Mapping[str, Any] | None = None) -> UUID | None:
        """None — слот уже есть или обязанность занята незавершённой работой (C-6)."""
        row = self._c.execute(
            """
            INSERT INTO wf_jobs (job_id, company_id, harness_id, employee_id, duty_id, trigger, slot, input, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'queued')
            ON CONFLICT DO NOTHING
            RETURNING job_id
            """,
            (uuid4(), employee.company_id, employee.harness_id, employee.employee_id, duty_id, trigger,
             slot, Jsonb(dict(input or {}))),
        ).fetchone()
        return row[0] if row else None

    def claim(self, *, now: datetime, lease: timedelta) -> Job | None:
        row = self._c.execute(
            f"""
            UPDATE wf_jobs SET status = 'running', lease_until = %s, attempts = attempts + 1,
                               updated_at = clock_timestamp()
            WHERE job_id = (
                SELECT job_id FROM wf_jobs
                WHERE status = 'queued' OR (status = 'running' AND lease_until < %s)
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING {JOB_COLUMNS}
            """,
            (now + lease, now),
        ).fetchone()
        return Job(*row) if row else None

    def claim_job(self, *, job_id: UUID, now: datetime, lease: timedelta) -> Job | None:
        row = self._c.execute(
            f"""
            UPDATE wf_jobs SET status = 'running', lease_until = %s, attempts = attempts + 1,
                               updated_at = clock_timestamp()
            WHERE job_id = %s AND status = 'queued'
            RETURNING {JOB_COLUMNS}
            """,
            (now + lease, job_id),
        ).fetchone()
        return Job(*row) if row else None

    def step(self, *, job_id: UUID, step_no: int) -> tuple[str, Any] | None:
        row = self._c.execute(
            "SELECT status, result FROM wf_job_steps WHERE job_id = %s AND step_no = %s", (job_id, step_no)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def start_step(self, *, job_id: UUID, step_no: int, name: str, idempotency_key: str) -> None:
        self._c.execute(
            """
            INSERT INTO wf_job_steps (job_id, step_no, name, status, idempotency_key)
            VALUES (%s, %s, %s, 'started', %s)
            ON CONFLICT (job_id, step_no) DO UPDATE SET started_at = clock_timestamp()
            """,
            (job_id, step_no, name, idempotency_key),
        )

    def finish_step(self, *, job_id: UUID, step_no: int, result: Any) -> None:
        self._c.execute(
            """
            UPDATE wf_job_steps SET status = 'done', result = %s, finished_at = clock_timestamp()
            WHERE job_id = %s AND step_no = %s
            """,
            (Jsonb(result), job_id, step_no),
        )

    def finish_job(self, *, job_id: UUID, report: Mapping[str, Any]) -> None:
        self._c.execute(
            """
            UPDATE wf_jobs SET status = 'done', report = %s, lease_until = NULL, updated_at = clock_timestamp()
            WHERE job_id = %s
            """,
            (Jsonb(dict(report)), job_id),
        )

    def fail_job(self, *, job_id: UUID, error: str) -> None:
        self._c.execute(
            """
            UPDATE wf_jobs SET status = 'failed', error = %s, lease_until = NULL, updated_at = clock_timestamp()
            WHERE job_id = %s
            """,
            (error[:1000], job_id),
        )

    def job_state(self, *, job_id: UUID) -> dict[str, Any] | None:
        row = self._c.execute(
            "SELECT status, attempts, report, error FROM wf_jobs WHERE job_id = %s", (job_id,)
        ).fetchone()
        if row is None:
            return None
        steps = self._c.execute(
            "SELECT step_no, name, status FROM wf_job_steps WHERE job_id = %s ORDER BY step_no", (job_id,)
        ).fetchall()
        return {
            "status": row[0], "attempts": row[1], "report": row[2], "error": row[3],
            "steps": [{"step_no": n, "name": name, "status": s} for n, name, s in steps],
        }
