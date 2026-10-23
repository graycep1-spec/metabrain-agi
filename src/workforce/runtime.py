"""Runtime работ цифровых сотрудников (C-6, «Runtime работ»; T-15).

Планировщик создаёт работы по расписанию паспорта, исполнитель выполняет их шагами с арендой.
Сами шаги делает обработчик обязанности — точка расширения, в которую T-14 подключает рой ЦС.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
import threading
from typing import Any, Protocol

from psycopg import Connection

from src.workforce.cron import Cron, CronError
from src.workforce.store import Employee, Job, WorkforceStore


TICK = timedelta(seconds=30)
CATCH_UP = timedelta(minutes=15)
AUTO_DIAGNOSE_WINDOW = timedelta(hours=6)
LEASE = timedelta(minutes=5)
MAX_JOBS_PER_TICK = 20

log = logging.getLogger("metabrain.runtime")


class Steps:
    """Шаги работы: результат каждого шага пишется в БД; при повторе работы шаг не выполняется заново."""

    def __init__(self, store: WorkforceStore, connection: Connection, job: Job):
        self._store, self._c, self._job = store, connection, job
        self._next = 1

    def run(self, name: str, fn: Callable[[str], Any]) -> Any:
        """`fn(idempotency_key)`; ключ одинаков при повторе — для шагов с записью (C-3 `execute`)."""
        step_no, self._next = self._next, self._next + 1
        with self._c.transaction():
            done = self._store.step(job_id=self._job.job_id, step_no=step_no)
        if done is not None and done[0] == "done":
            return done[1]
        key = sha256(f"{self._job.job_id}:{step_no}".encode()).hexdigest()
        with self._c.transaction():
            self._store.start_step(job_id=self._job.job_id, step_no=step_no, name=name, idempotency_key=key)
        result = fn(key)
        with self._c.transaction():
            self._store.finish_step(job_id=self._job.job_id, step_no=step_no, result=result)
        return result


class DutyHandler(Protocol):
    def run(self, job: Job, passport: Mapping[str, Any], steps: Steps) -> dict[str, Any]:
        """Отчёт: {text, sources[], score_ref, proposal_ids[]} (C-1 `report`)."""


class ReportSink(Protocol):
    def deliver(self, job: Job, report: Mapping[str, Any]) -> None: ...


@dataclass
class TickResult:
    enqueued: int = 0
    finished: int = 0
    failed: int = 0


class Runtime:
    """Фоновому runtime нужно отдельное подключение в режиме autocommit (ADR-024): иначе запросы
    обработчика вне transaction() открывают неявную транзакцию, и шаги перестают фиксироваться.
    Для `ask` runtime работает внутри транзакции запроса API — там это допустимо."""

    def __init__(
        self,
        connection: Connection,
        *,
        handler: DutyHandler | None,
        sink: ReportSink,
        clock: Callable[[], datetime] | None = None,
        lease: timedelta = LEASE,
        auto_diagnose: bool = True,
    ):
        self._c = connection
        self._store = WorkforceStore(connection)
        self._handler = handler
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lease = lease
        # Авто-диагност (ADR-034): упавшая работа сама зовёт помощь — рой разбирает журнал,
        # отчёт уходит куратору, рекомендация — предложением. Не чаще раза в окно на сотрудника.
        self._auto_diagnose = auto_diagnose

    # --- планировщик ----------------------------------------------------------------

    def schedule(self) -> int:
        """Создать работы за наступившие слоты; без обработчика работ не создаём (C-6)."""
        if self._handler is None:
            return 0
        now = self._clock()
        created = 0
        with self._c.transaction():
            candidates = self._store.scheduled_employees()
        for employee, passport in candidates:
            for trigger in passport.get("triggers", {}).get("schedule", []):
                try:
                    slots = Cron(trigger["cron"]).slots(now - CATCH_UP, now)
                except (CronError, KeyError):
                    continue
                for slot in slots:
                    with self._c.transaction():
                        if self._store.enqueue(employee=employee, duty_id=trigger["duty_id"],
                                               trigger="schedule", slot=slot):
                            created += 1
        return created

    def enqueue(self, employee: Employee, *, duty_id: str, trigger: str, input: Mapping[str, Any]) -> str | None:
        """Работа по вопросу человека или событию (`ask`, `event`)."""
        with self._c.transaction():
            job_id = self._store.enqueue(employee=employee, duty_id=duty_id, trigger=trigger, input=input)
        return str(job_id) if job_id else None

    # --- исполнитель ------------------------------------------------------------------

    def run_one(self) -> str | None:
        """Взять одну работу и выполнить. Возвращает исход: done / failed / None (работ нет)."""
        if self._handler is None:
            return None
        with self._c.transaction():
            job = self._store.claim(now=self._clock(), lease=self._lease)
        if job is None:
            return None
        return self._execute(job)

    def run_job(self, job_id) -> str | None:
        """Выполнить конкретную работу сразу (`ask`, C-6): тот же путь шагов и журнала."""
        if self._handler is None:
            return None
        with self._c.transaction():
            job = self._store.claim_job(job_id=job_id, now=self._clock(), lease=self._lease)
        return self._execute(job) if job else None

    def report(self, job_id) -> dict[str, Any] | None:
        with self._c.transaction():
            return self._store.job_state(job_id=job_id)

    def _execute(self, job: Job) -> str:
        with self._c.transaction():
            employee = self._store.get_employee(employee_id=job.employee_id, harness_id=job.harness_id)
            active = (
                self._store.passport(employee_id=employee.employee_id, version=employee.active_version)
                if employee and employee.active_version else None
            )
        if employee is None or employee.status != "active" or active is None or active["status"] != "active":
            with self._c.transaction():
                self._store.fail_job(job_id=job.job_id, error="employee is not active")
            return "failed"
        try:
            report = self._handler.run(job, active["passport"], Steps(self._store, self._c, job))
        except Exception as error:  # сбой обработчика — работа завершена с ошибкой
            log.exception("job %s failed", job.job_id)
            with self._c.transaction():
                self._store.fail_job(job_id=job.job_id, error=f"{type(error).__name__}: {error}")
            self._maybe_auto_diagnose(job, employee, f"{type(error).__name__}: {error}")
            return "failed"
        with self._c.transaction():
            self._store.finish_job(job_id=job.job_id, report=report)
        self._sink.deliver(job, report)
        return "done"

    def _maybe_auto_diagnose(self, job: Job, employee: Employee, error_text: str) -> None:
        """Упавшая работа зовёт диагноста сама (ADR-034): отдельная работа тем же роем.

        Защита от зацикливания: упавшую диагностику не диагностируем; на сотрудника —
        не чаще раза в AUTO_DIAGNOSE_WINDOW. Бюджет и DLP — те же, что у обычного `ask`.
        """
        if not self._auto_diagnose or (job.input or {}).get("diagnosis"):
            return
        try:
            from src.harness.journal import JournalStore
            from src.workforce import diagnosis

            now = self._clock()
            with self._c.transaction():
                owner = self._c.execute(
                    "SELECT owner_person_id FROM harnesses WHERE id = %s", (job.harness_id,)
                ).fetchone()
                recent = self._c.execute(
                    "SELECT count(*) FROM journal_entries WHERE harness_id = %s AND employee_id = %s "
                    "AND kind = 'help.requested' AND payload->>'auto' = 'true' AND created_at > %s",
                    (job.harness_id, job.employee_id, now - AUTO_DIAGNOSE_WINDOW),
                ).fetchone()
            if owner is None or recent[0] > 0:
                return
            journal = JournalStore(self._c)
            page = journal.list_entries(person_id=owner[0], harness_id=job.harness_id,
                                        employee_id=job.employee_id, page_size=15)
            question = f"работа «{job.duty_id}» упала с ошибкой: {error_text[:200]} — найди причину"
            journal.append_entry(
                person_id=owner[0], company_id=job.company_id, harness_id=job.harness_id,
                kind="help.requested", employee_id=job.employee_id,
                payload={"question": question, "auto": "true", "job_id": str(job.job_id)},
            )
            with self._c.transaction():
                self._store.enqueue(
                    employee=employee, duty_id="ask", trigger="ask",
                    input={"text": diagnosis.prompt(question, diagnosis.digest(page.entries))[:4000],
                           "diagnosis": True},
                )
        except Exception:  # noqa: BLE001 — авто-помощь не должна ронять runtime
            log.exception("auto-diagnose for job %s skipped", job.job_id)

    def tick(self) -> TickResult:
        result = TickResult(enqueued=self.schedule())
        for _ in range(MAX_JOBS_PER_TICK):
            outcome = self.run_one()
            if outcome is None:
                break
            if outcome == "done":
                result.finished += 1
            else:
                result.failed += 1
        return result


class RuntimeThread:
    """Фоновый цикл runtime в процессе харнесса (ADR-024)."""

    def __init__(self, runtime: Runtime, interval: timedelta = TICK):
        self._runtime = runtime
        self._interval = interval.total_seconds()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="metabrain-runtime", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._runtime.tick()
            except Exception:  # ошибка тика не должна останавливать runtime
                log.exception("runtime tick failed")
            self._stop.wait(self._interval)
