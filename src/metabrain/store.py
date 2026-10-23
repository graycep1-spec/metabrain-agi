"""Postgres-хранилище мета-мозга: навыки, кандидаты, согласия, ретривал, журнал (C-5, ADR-018)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import re
from typing import Any
from uuid import UUID, uuid4

from psycopg import Connection, errors as pg_errors
from psycopg.types.json import Jsonb


SCHEMA_PATH = Path(__file__).with_name("schema.sql")
WORD = re.compile(r"[\w-]{2,}")
MAX_QUERY_WORDS = 20

# Видимость навыка для харнесса: свои личные + навыки своей компании (C-5, «Правила»).
VISIBLE = """
    company_id = %(company_id)s
    AND (scope = 'company' OR (scope = 'personal' AND owner_harness_id = %(harness_id)s))
"""
SKILL_COLUMNS = """
    skill_id, version, scope, company_id, owner_harness_id, author_person_id, title,
    task_class, role_tags, body, examples, supersedes, provenance, created_at
"""


class ConsentReused(PermissionError):
    code = "consent_reused"


def migrate(connection: Connection) -> None:
    with connection.transaction():
        connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"), prepare=False)


def or_query(text: str) -> str | None:
    """Слова запроса через OR для websearch_to_tsquery; None — фильтр не нужен."""
    words = [w.lower() for w in WORD.findall(text or "")][:MAX_QUERY_WORDS]
    return " or ".join(words) if words else None


@dataclass(frozen=True)
class Candidate:
    candidate_id: UUID
    company_id: UUID
    harness_id: UUID
    artifact: dict[str, Any]
    artifact_hash: str
    scorecard: dict[str, Any]
    verdict: str
    published_skill_id: UUID | None
    published_version: str | None


def _skill(row: Sequence[Any]) -> dict[str, Any]:
    (skill_id, version, scope, company_id, owner, author, title, task_class, role_tags,
     body, examples, supersedes, provenance, created_at) = row
    skill = {
        "skill_id": str(skill_id),
        "version": version,
        "scope": scope,
        "company_id": str(company_id),
        "title": title,
        "applies_to": {"role_tags": list(role_tags), "task_class": task_class},
        "body": body,
        "examples": examples,
        "provenance": provenance,
        "created_at": created_at.isoformat(),
    }
    if owner is not None:
        skill["owner_harness_id"] = str(owner)
    if author is not None:
        skill["author_person_id"] = author
    if supersedes is not None:
        skill["supersedes"] = str(supersedes)
    return skill


class MetabrainStore:
    def __init__(self, connection: Connection):
        self._c = connection

    # --- кандидаты и публикация -------------------------------------------------

    def save_candidate(
        self, *, company_id: UUID, harness_id: UUID, artifact: Mapping[str, Any],
        artifact_hash: str, scorecard: Mapping[str, Any], verdict: str,
    ) -> UUID:
        candidate_id = uuid4()
        self._c.execute(
            """
            INSERT INTO mb_skill_candidates
                (candidate_id, company_id, harness_id, artifact, artifact_hash, scorecard, verdict)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (candidate_id, company_id, harness_id, Jsonb(dict(artifact)), artifact_hash, Jsonb(dict(scorecard)), verdict),
        )
        return candidate_id

    def lock_candidate(self, *, candidate_id: UUID, company_id: UUID, harness_id: UUID) -> Candidate | None:
        """Внутри транзакции: кандидат этого харнесса, заблокированный от параллельной публикации."""
        row = self._c.execute(
            """
            SELECT candidate_id, company_id, harness_id, artifact, artifact_hash, scorecard, verdict,
                   published_skill_id, published_version
            FROM mb_skill_candidates
            WHERE candidate_id = %s AND company_id = %s AND harness_id = %s
            FOR UPDATE
            """,
            (candidate_id, company_id, harness_id),
        ).fetchone()
        return Candidate(*row) if row else None

    def use_consent(self, *, token_id: UUID, harness_id: UUID, candidate_id: UUID) -> None:
        try:
            with self._c.transaction():
                self._c.execute(
                    "INSERT INTO mb_used_consents (token_id, harness_id, candidate_id) VALUES (%s, %s, %s)",
                    (token_id, harness_id, candidate_id),
                )
        except pg_errors.UniqueViolation as error:
            raise ConsentReused("consent_reused") from error

    def latest_version(self, *, skill_id: UUID, company_id: UUID, harness_id: UUID) -> dict[str, Any] | None:
        row = self._c.execute(
            f"""
            SELECT {SKILL_COLUMNS} FROM mb_skills
            WHERE skill_id = %(skill_id)s AND {VISIBLE}
            ORDER BY major DESC, minor DESC, patch DESC
            LIMIT 1
            """,
            {"skill_id": skill_id, "company_id": company_id, "harness_id": harness_id},
        ).fetchone()
        return _skill(row) if row else None

    def insert_skill(
        self, *, skill_id: UUID, version: tuple[int, int, int], candidate: Candidate,
        provenance: Mapping[str, Any], supersedes: UUID | None, created_at: datetime,
    ) -> str:
        data = candidate.artifact["candidate"]
        scope = data["scope"]
        version_text = ".".join(map(str, version))
        self._c.execute(
            """
            INSERT INTO mb_skills
                (skill_id, version, major, minor, patch, scope, company_id, owner_harness_id,
                 author_person_id, title, task_class, role_tags, body, examples, supersedes,
                 provenance, candidate_id, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                skill_id, version_text, *version, scope, candidate.company_id,
                candidate.harness_id if scope == "personal" else None,
                data.get("author_person_id") if scope == "company" else None,
                data["title"], data["applies_to"]["task_class"], list(data["applies_to"]["role_tags"]),
                data["body"], Jsonb(list(data.get("examples") or [])), supersedes,
                Jsonb(dict(provenance)), candidate.candidate_id, created_at,
            ),
        )
        self._c.execute(
            "UPDATE mb_skill_candidates SET published_skill_id = %s, published_version = %s WHERE candidate_id = %s",
            (skill_id, version_text, candidate.candidate_id),
        )
        return version_text

    # --- чтение навыков --------------------------------------------------------

    def resolve(
        self, *, company_id: UUID, harness_id: UUID, lock: tuple[UUID, str] | None = None,
        query: str | None = None, task_class: str | None = None,
        role_tags: Sequence[str] | None = None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"company_id": company_id, "harness_id": harness_id, "limit": limit}
        if lock is not None:
            params["skill_id"], params["version"] = lock
            rows = self._c.execute(
                f"SELECT {SKILL_COLUMNS} FROM mb_skills WHERE skill_id = %(skill_id)s AND version = %(version)s AND {VISIBLE}",
                params,
            ).fetchall()
            return [_skill(row) for row in rows]

        filters, order = [], "created_at DESC"
        if task_class:
            filters.append("task_class = %(task_class)s")
            params["task_class"] = task_class
        if role_tags:
            filters.append("role_tags && %(role_tags)s")
            params["role_tags"] = list(role_tags)
        text = or_query(query or "")
        if text:
            params["q"] = text
            filters.append("search @@ (websearch_to_tsquery('russian', %(q)s) || websearch_to_tsquery('simple', %(q)s))")
            order = "ts_rank(search, websearch_to_tsquery('russian', %(q)s) || websearch_to_tsquery('simple', %(q)s)) DESC, created_at DESC"
        where = " AND ".join(filters) or "TRUE"
        rows = self._c.execute(
            f"""
            WITH latest AS (
                SELECT DISTINCT ON (skill_id) * FROM mb_skills
                WHERE {VISIBLE}
                ORDER BY skill_id, major DESC, minor DESC, patch DESC
            )
            SELECT {SKILL_COLUMNS} FROM latest
            WHERE {where}
            ORDER BY {order}
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        return [_skill(row) for row in rows]

    # --- ретривал --------------------------------------------------------------

    def index_docs(self, *, company_id: UUID, harness_id: UUID, docs: Sequence[Mapping[str, Any]]) -> int:
        for doc in docs:
            self._c.execute(
                """
                INSERT INTO mb_retrieval_docs (company_id, harness_id, doc_id, kind, text, source, sensitivity)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (harness_id, doc_id) DO UPDATE
                SET kind = EXCLUDED.kind, text = EXCLUDED.text, source = EXCLUDED.source,
                    sensitivity = EXCLUDED.sensitivity, updated_at = clock_timestamp()
                WHERE mb_retrieval_docs.company_id = EXCLUDED.company_id
                """,
                (
                    company_id, harness_id, doc["doc_id"], doc["kind"], doc["text"],
                    Jsonb(doc.get("source")) if doc.get("source") is not None else None,
                    Jsonb(dict(doc.get("sensitivity") or {})),
                ),
            )
        return len(docs)

    def search_docs(
        self, *, company_id: UUID, harness_id: UUID, query: str,
        kinds: Sequence[str] | None = None, limit: int = 10,
    ) -> list[dict[str, Any]]:
        text = or_query(query)
        if not text:
            return []
        params: dict[str, Any] = {"company_id": company_id, "harness_id": harness_id, "q": text, "limit": limit}
        kind_filter = ""
        if kinds:
            kind_filter = "AND kind = ANY(%(kinds)s)"
            params["kinds"] = list(kinds)
        rows = self._c.execute(
            f"""
            WITH q AS (SELECT websearch_to_tsquery('russian', %(q)s) || websearch_to_tsquery('simple', %(q)s) AS q)
            SELECT doc_id, kind, text, source, sensitivity, ts_rank(search, q.q) AS score
            FROM mb_retrieval_docs, q
            WHERE company_id = %(company_id)s AND harness_id = %(harness_id)s {kind_filter}
              AND search @@ q.q
            ORDER BY score DESC, updated_at DESC
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        return [
            {"doc_id": d, "kind": k, "text": t, "source": s, "sensitivity": sens, "score": round(float(score), 6)}
            for d, k, t, s, sens, score in rows
        ]

    # --- журнал ----------------------------------------------------------------

    def audit(self, *, company_id: UUID | None, harness_id: UUID | None, operation: str,
              outcome: str, detail: Mapping[str, Any] | None = None) -> None:
        self._c.execute(
            "INSERT INTO mb_audit (company_id, harness_id, operation, outcome, detail) VALUES (%s, %s, %s, %s, %s)",
            (company_id, harness_id, operation, outcome, Jsonb(dict(detail or {}))),
        )
