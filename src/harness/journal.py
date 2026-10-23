import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from src.harness.store import HarnessStore


@dataclass(frozen=True)
class JournalEntry:
    id: UUID
    company_id: UUID
    harness_id: UUID
    employee_id: UUID | None
    kind: str
    payload: dict[str, Any]
    sources: list[Any]
    score_ref: str | None
    created_at: datetime


@dataclass(frozen=True)
class JournalPage:
    entries: tuple[JournalEntry, ...]
    next_cursor: str | None


class JournalStore:
    def __init__(self, connection: Connection):
        self._connection = connection
        self._harness_store = HarnessStore(connection)
        schema = Path(__file__).with_name("journal.sql").read_text(encoding="utf-8")
        with connection.transaction():
            connection.execute(schema, prepare=False)

    def append_entry(
        self,
        *,
        person_id: UUID,
        company_id: UUID,
        harness_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
        employee_id: UUID | None = None,
        sources: Sequence[Any] = (),
        score_ref: str | None = None,
    ) -> JournalEntry:
        if not kind.strip():
            raise ValueError("journal entry kind is required")
        entry_id = uuid4()
        with self._connection.transaction():
            self._harness_store.require_harness_access(
                person_id, harness_id, company_id
            )
            row = self._connection.execute(
                """
                INSERT INTO journal_entries
                    (id, company_id, harness_id, employee_id, kind, payload,
                     sources, score_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    entry_id,
                    company_id,
                    harness_id,
                    employee_id,
                    kind,
                    Jsonb(dict(payload)),
                    Jsonb(list(sources)),
                    score_ref,
                ),
            ).fetchone()
        return JournalEntry(
            id=entry_id,
            company_id=company_id,
            harness_id=harness_id,
            employee_id=employee_id,
            kind=kind,
            payload=dict(payload),
            sources=list(sources),
            score_ref=score_ref,
            created_at=row[0],
        )

    def list_entries(
        self,
        *,
        person_id: UUID,
        harness_id: UUID,
        employee_id: UUID | None = None,
        kind: str | None = None,
        from_time: datetime | None = None,
        to_time: datetime | None = None,
        cursor: str | None = None,
        page_size: int = 50,
    ) -> JournalPage:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        if from_time is not None and to_time is not None and from_time > to_time:
            raise ValueError("from_time must be before or equal to to_time")

        cursor_value = self._decode_cursor(cursor) if cursor is not None else None
        with self._connection.transaction():
            company_id = self._harness_store.require_harness_access(
                person_id, harness_id
            )
            conditions = [
                "company_id = %s",
                "harness_id = %s",
            ]
            params: list[Any] = [company_id, harness_id]
            if employee_id is not None:
                conditions.append("employee_id = %s")
                params.append(employee_id)
            if kind is not None:
                conditions.append("kind = %s")
                params.append(kind)
            if from_time is not None:
                conditions.append("created_at >= %s")
                params.append(from_time)
            if to_time is not None:
                conditions.append("created_at <= %s")
                params.append(to_time)
            if cursor_value is not None:
                conditions.append("(created_at, id) < (%s, %s)")
                params.extend(cursor_value)

            rows = self._connection.execute(
                f"""
                SELECT id, company_id, harness_id, employee_id, kind, payload,
                       sources, score_ref, created_at
                FROM journal_entries
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (*params, page_size + 1),
            ).fetchall()

        has_more = len(rows) > page_size
        entries = tuple(self._entry_from_row(row) for row in rows[:page_size])
        next_cursor = self._encode_cursor(entries[-1]) if has_more else None
        return JournalPage(entries=entries, next_cursor=next_cursor)

    def _entry_from_row(self, row: tuple[Any, ...]) -> JournalEntry:
        return JournalEntry(
            id=row[0],
            company_id=row[1],
            harness_id=row[2],
            employee_id=row[3],
            kind=row[4],
            payload=row[5],
            sources=row[6],
            score_ref=row[7],
            created_at=row[8],
        )

    def _encode_cursor(self, entry: JournalEntry) -> str:
        encoded = f"{entry.created_at.isoformat()}|{entry.id}".encode("utf-8")
        return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")

    def _decode_cursor(self, cursor: str) -> tuple[datetime, UUID]:
        try:
            padding = "=" * (-len(cursor) % 4)
            decoded = base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            ).decode("utf-8")
            timestamp, entry_id = decoded.rsplit("|", 1)
            created_at = datetime.fromisoformat(timestamp)
            if created_at.utcoffset() is None:
                raise ValueError
            return created_at, UUID(entry_id)
        except (ValueError, UnicodeDecodeError) as error:
            raise ValueError("invalid journal cursor") from error
