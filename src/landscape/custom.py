"""Кастомные подключения: запись в каталоге + секрет в vault харнесса.

Никакой сетевой проверки здесь нет: для AD/RDP/SSH/WinRM и нестандартных API
нужен отдельный адаптер. Запись показывает, что подключение настроено, кто и
по какой роли получил доступ, а сам секрет наружу не возвращается.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.types.json import Jsonb

from src.harness.journal import JournalStore
from src.harness.store import Registration
from src.harness.vault import HarnessVault, VaultUnavailable


def _text(value: Any, field: str, *, required: bool = True, limit: int = 500) -> str:
    value = "" if value is None else value
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} is required")
    if len(value) > limit or "\n" in value or "\r" in value:
        raise ValueError(f"{field} is too long or contains a newline")
    return value


def _roles(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [part.strip() for part in value.replace(",", "\n").splitlines() if part.strip()]
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValueError("allowed_roles must be a list of strings")
    return [item.strip()[:80] for item in value if item.strip()][:20]


class CustomConnectionStore:
    def __init__(self, connection: Connection, *, vault: HarnessVault | None,
                 journal: JournalStore, clock: Any | None = None):
        self._c = connection
        self._vault = vault
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def list(self, identity: Registration) -> list[dict[str, Any]]:
        rows = self._c.execute(
            """
            SELECT connection_id, name, kind, target, auth_type, allowed_roles, note,
                   status, secret_ref, created_at
            FROM custom_connections
            WHERE harness_id = %s AND company_id = %s
            ORDER BY created_at DESC
            """,
            (identity.harness_id, identity.company_id),
        ).fetchall()
        return [self._row(row) for row in rows]

    def create(self, identity: Registration, *, name: Any, kind: Any = "custom",
               target: Any, auth_type: Any = "token", secret: Any = "",
               allowed_roles: Any = (), note: Any = "") -> dict[str, Any]:
        name = _text(name, "name", limit=200)
        kind = _text(kind, "kind", limit=100) if kind else "custom"
        target = _text(target, "target", limit=500)
        auth_type = _text(auth_type, "auth_type", limit=200)
        note = _text(note, "note", required=False, limit=1000)
        roles = _roles(allowed_roles)
        connection_id = uuid4()
        secret_ref: str | None = None
        raw_secret = secret if isinstance(secret, str) else ""
        if raw_secret.strip():
            if self._vault is None:
                raise VaultUnavailable("secret store is not configured")
            secret_ref = self._vault.put(identity, system=f"custom:{connection_id}", secret=raw_secret)
        status = "configured" if secret_ref else "needs_credentials"
        now = self._clock()
        with self._c.transaction():
            self._c.execute(
                """
                INSERT INTO custom_connections
                    (connection_id, company_id, harness_id, name, kind, target, auth_type,
                     secret_ref, allowed_roles, note, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (connection_id, identity.company_id, identity.harness_id, name, kind, target, auth_type,
                 secret_ref, Jsonb(roles), note, status, now, now),
            )
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="custom_connection.created",
            payload={"connection_id": str(connection_id), "name": name, "kind": kind,
                     "target": target, "auth_type": auth_type, "allowed_roles": roles,
                     "has_secret": bool(secret_ref)},
        )
        row = self._c.execute(
            """
            SELECT connection_id, name, kind, target, auth_type, allowed_roles, note,
                   status, secret_ref, created_at
            FROM custom_connections WHERE connection_id = %s
            """,
            (connection_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError("custom connection was not stored")
        return self._row(row)

    @staticmethod
    def _row(row: Sequence[Any]) -> dict[str, Any]:
        return {
            "connection_id": str(row[0]),
            "name": row[1],
            "kind": row[2],
            "target": row[3],
            "auth_type": row[4],
            "allowed_roles": row[5] or [],
            "note": row[6],
            "status": row[7],
            "has_secret": bool(row[8]),
            "secret_masked": "••••••••" if row[8] else "не задан",
            "created_at": row[9].isoformat() if isinstance(row[9], datetime) else row[9],
        }
