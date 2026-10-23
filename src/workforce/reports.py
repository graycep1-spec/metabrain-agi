"""Доставка отчётов работ куратору: журнал харнесса и Telegram (C-1 `report`, C-6)."""

from collections.abc import Callable, Mapping
import asyncio
from typing import Any

from psycopg import Connection

from src.harness.journal import JournalStore
from src.workforce.store import Job


class HarnessReportSink:
    def __init__(self, connection: Connection, *, send: Callable[[str, str], None] | None = None):
        self._c = connection
        self._journal = JournalStore(connection)
        self._send = send

    def deliver(self, job: Job, report: Mapping[str, Any]) -> None:
        with self._c.transaction():
            owner, channel_user_id = self._c.execute(
                """
                SELECT h.owner_person_id, ci.channel_user_id
                FROM harnesses h
                LEFT JOIN channel_identities ci ON ci.person_id = h.owner_person_id AND ci.channel = 'telegram'
                WHERE h.id = %s AND h.company_id = %s
                """,
                (job.harness_id, job.company_id),
            ).fetchone()
            self._journal.append_entry(
                person_id=owner, company_id=job.company_id, harness_id=job.harness_id, kind="report",
                employee_id=job.employee_id,
                payload={"job_id": str(job.job_id), "duty_id": job.duty_id, "trigger": job.trigger,
                         "text": report.get("text", ""), "proposal_ids": list(report.get("proposal_ids", []))},
                sources=list(report.get("sources", [])), score_ref=report.get("score_ref"),
            )
        if self._send and channel_user_id and report.get("text"):
            self._send(channel_user_id, str(report["text"]))


def telegram_sender(bot: Any, loop: asyncio.AbstractEventLoop) -> Callable[[str, str], None]:
    """Отправка из потока runtime в цикл событий aiogram."""

    def send(channel_user_id: str, text: str) -> None:
        asyncio.run_coroutine_threadsafe(bot.send_message(int(channel_user_id), text[:4000]), loop).result(timeout=30)

    return send
