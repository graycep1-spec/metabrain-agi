from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb


class RegistrationError(ValueError):
    pass


class CompanyNotFoundError(LookupError):
    pass


class HarnessAccessDenied(PermissionError):
    status_code = 403
    code = "forbidden_harness"

    def __init__(self):
        super().__init__("caller does not have access to the requested harness")


@dataclass(frozen=True)
class Registration:
    person_id: UUID
    company_id: UUID
    harness_id: UUID


@dataclass(frozen=True)
class HarnessRecord:
    id: UUID
    company_id: UUID
    harness_id: UUID
    kind: str
    payload: dict[str, Any]


class HarnessStore:
    def __init__(self, connection: Connection):
        self._connection = connection
        schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        with connection.transaction():
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)

    def register_person(
        self,
        *,
        channel: str,
        channel_user_id: str,
        company_name: str | None = None,
        trusted_company_id: UUID | None = None,
    ) -> Registration:
        if channel != "telegram":
            raise RegistrationError("channel must be telegram")
        if not channel_user_id.strip():
            raise RegistrationError("channel_user_id is required")
        if (company_name is None) == (trusted_company_id is None):
            raise RegistrationError(
                "provide either company_name or a trusted company identifier"
            )
        if company_name is not None and not company_name.strip():
            raise RegistrationError("company_name is required")

        company_id = trusted_company_id
        person_id = uuid4()
        harness_id = uuid4()
        with self._connection.transaction():
            if company_name is not None:
                company_id = uuid4()
                self._connection.execute(
                    "INSERT INTO companies (id, name) VALUES (%s, %s)",
                    (company_id, company_name.strip()),
                )
            else:
                company = self._connection.execute(
                    "SELECT id FROM companies WHERE id = %s",
                    (trusted_company_id,),
                ).fetchone()
                if company is None:
                    raise CompanyNotFoundError("company does not exist")

            if company_id is None:
                raise RegistrationError("company could not be resolved")
            self._connection.execute(
                "INSERT INTO people (id, company_id) VALUES (%s, %s)",
                (person_id, company_id),
            )
            self._connection.execute(
                """
                INSERT INTO channel_identities (channel, channel_user_id, person_id)
                VALUES (%s, %s, %s)
                """,
                (channel, channel_user_id, person_id),
            )
            self._connection.execute(
                """
                INSERT INTO harnesses (id, company_id, owner_person_id)
                VALUES (%s, %s, %s)
                """,
                (harness_id, company_id, person_id),
            )
            self._connection.execute(
                """
                INSERT INTO harness_records
                    (id, company_id, harness_id, kind, payload)
                VALUES (%s, %s, %s, 'employee', %s)
                """,
                (
                    uuid4(),
                    company_id,
                    harness_id,
                    Jsonb(
                        {
                            "kind": "researcher",
                            "title": "Исследователь",
                            "status": "active",
                            "autonomy": "L0",
                        }
                    ),
                ),
            )

        return Registration(person_id, company_id, harness_id)

    def append_harness_record(
        self,
        *,
        person_id: UUID,
        company_id: UUID,
        harness_id: UUID,
        kind: str,
        payload: Mapping[str, Any],
    ) -> HarnessRecord:
        if not kind.strip():
            raise ValueError("record kind is required")
        record_id = uuid4()
        with self._connection.transaction():
            self.require_harness_access(person_id, harness_id, company_id)
            self._connection.execute(
                """
                INSERT INTO harness_records
                    (id, company_id, harness_id, kind, payload)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (record_id, company_id, harness_id, kind, Jsonb(dict(payload))),
            )
        return HarnessRecord(record_id, company_id, harness_id, kind, dict(payload))

    def list_harness_records(
        self,
        *,
        person_id: UUID,
        harness_id: UUID,
        kind: str,
    ) -> list[HarnessRecord]:
        with self._connection.transaction():
            company_id = self.require_harness_access(person_id, harness_id)
            rows = self._connection.execute(
                """
                SELECT id, company_id, harness_id, kind, payload
                FROM harness_records
                WHERE company_id = %s AND harness_id = %s AND kind = %s
                ORDER BY created_at, id
                """,
                (company_id, harness_id, kind),
            ).fetchall()
        return [
            HarnessRecord(
                id=row[0],
                company_id=row[1],
                harness_id=row[2],
                kind=row[3],
                payload=row[4],
            )
            for row in rows
        ]

    def require_harness_access(
        self,
        person_id: UUID,
        harness_id: UUID,
        company_id: UUID | None = None,
    ) -> UUID:
        query = """
            SELECT company_id
            FROM harnesses
            WHERE owner_person_id = %s AND id = %s
        """
        params: tuple[UUID, ...] = (person_id, harness_id)
        if company_id is not None:
            query += " AND company_id = %s"
            params += (company_id,)
        row = self._connection.execute(query, params).fetchone()
        if row is None:
            raise HarnessAccessDenied()
        return row[0]

    def identity_for_channel_user(
        self,
        *,
        channel: str,
        channel_user_id: str,
    ) -> Registration | None:
        row = self._connection.execute(
            """
            SELECT p.id, p.company_id, h.id
            FROM channel_identities AS ci
            JOIN people AS p ON p.id = ci.person_id
            JOIN harnesses AS h
              ON h.owner_person_id = p.id AND h.company_id = p.company_id
            WHERE ci.channel = %s AND ci.channel_user_id = %s
            """,
            (channel, channel_user_id),
        ).fetchone()
        if row is None:
            return None
        return Registration(person_id=row[0], company_id=row[1], harness_id=row[2])
