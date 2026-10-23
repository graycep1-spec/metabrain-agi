"""Операции мета-мозга по C-5: навыки, публикация через гейт, ретривал, политика."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg import Connection

from src.metabrain.consent import ConsentRejected, verify_consent
from src.metabrain.policy import PolicyError, resolve_policy
from src.metabrain.store import ConsentReused, MetabrainStore
from src.metabrain.validator_client import ValidatorClient, ValidatorUnavailable
from src.validator.scorecard import ScoreCardRejected, artifact_hash, verify_scorecard


DOC_KINDS = ("fact", "trajectory", "card")
MAX_DOCS = 500
MAX_TEXT = 20_000


class MetabrainError(Exception):
    status = 500
    code = "error"

    def __init__(self, code: str | None = None, detail: str = ""):
        super().__init__(detail or code or self.code)
        if code:
            self.code = code
        self.detail = detail


class InvalidRequest(MetabrainError):
    status, code = 400, "invalid_request"


class Rejected(MetabrainError):
    status, code = 403, "rejected"


class NotFound(MetabrainError):
    status, code = 404, "not_found"


class Conflict(MetabrainError):
    status, code = 409, "conflict"


class Unavailable(MetabrainError):
    status, code = 503, "unavailable"


def _uuid(value: Any, name: str) -> UUID:
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as error:
        raise InvalidRequest(detail=f"{name} must be a UUID") from error


def _limit(value: Any) -> int:
    if value is None:
        return 10
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 50:
        raise InvalidRequest(detail="limit must be an integer within 1..50")
    return value


def _str_list(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise InvalidRequest(detail=f"{name} must be a list of non-empty strings")
    return value


class Metabrain:
    def __init__(
        self,
        connect: Callable[[], Connection],
        validator: ValidatorClient,
        policy: Mapping[str, Any],
        consent_key: Ed25519PublicKey | None,
        *,
        clock: Callable[[], datetime] | None = None,
    ):
        self._connect = connect
        self._validator = validator
        self._policy = policy
        self._consent_key = consent_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # --- каркас вызова: транзакция + журнал --------------------------------------

    def _call(
        self,
        operation: str,
        request: Mapping[str, Any],
        fn: Callable[[MetabrainStore, UUID, UUID | None], Any],
        via: Mapping[str, Any] | None = None,
        *,
        allow_company_only: bool = False,
    ) -> Any:
        """`via` — кто вызвал не через внутренний HTTP (MCP): префикс операции и детали в журнал."""
        if not isinstance(request, Mapping):
            raise InvalidRequest(detail="request must be an object")
        company_id = _uuid(request.get("company_id"), "company_id")
        if allow_company_only and request.get("harness_id") is None:
            harness_id = None
        else:
            harness_id = _uuid(request.get("harness_id"), "harness_id")
        detail = dict(via or {})
        if via is not None:
            operation = f"{detail.pop('channel', 'external')}.{operation}"
        try:
            connection = self._connect()
        except Exception as error:  # БД недоступна
            raise Unavailable("database_unavailable") from error
        with connection:
            store = MetabrainStore(connection)
            outcome = "ok"
            try:
                with connection.transaction():
                    return fn(store, company_id, harness_id)
            except MetabrainError as error:
                outcome = error.code
                raise
            except Exception:
                outcome = "error"
                raise
            finally:
                with connection.transaction():
                    store.audit(
                        company_id=company_id, harness_id=harness_id, operation=operation,
                        outcome=outcome, detail=detail,
                    )

    def audit_only(
        self, *, operation: str, outcome: str, company_id: str | None = None,
        harness_id: str | None = None, detail: Mapping[str, Any] | None = None,
    ) -> None:
        """Запись в журнал без операции: отказ авторизации или scope во внешнем канале."""
        try:
            connection = self._connect()
        except Exception as error:
            raise Unavailable("database_unavailable") from error
        with connection:
            with connection.transaction():
                MetabrainStore(connection).audit(
                    company_id=UUID(company_id) if company_id else None,
                    harness_id=UUID(harness_id) if harness_id else None,
                    operation=operation, outcome=outcome, detail=detail,
                )

    # --- навыки -----------------------------------------------------------------

    def resolve(self, request: Mapping[str, Any], via: Mapping[str, Any] | None = None) -> dict[str, Any]:
        lock = None
        if request.get("lock") is not None:
            raw = request["lock"]
            skill_id, sep, version = str(raw).partition("@")
            if not sep or not version:
                raise InvalidRequest(detail="lock must be skill_id@version")
            lock = (_uuid(skill_id, "lock.skill_id"), version)
        query = request.get("query")
        task_class = request.get("task_class")
        for name, value in (("query", query), ("task_class", task_class)):
            if value is not None and not isinstance(value, str):
                raise InvalidRequest(detail=f"{name} must be a string")
        role_tags = _str_list(request.get("role_tags"), "role_tags")
        limit = _limit(request.get("limit"))

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            skills = store.resolve(
                company_id=company_id, harness_id=harness_id, lock=lock, query=query,
                task_class=task_class, role_tags=role_tags, limit=limit,
            )
            return {"skills": skills}

        return self._call("skills.resolve", request, run, via, allow_company_only=via is not None)

    def propose(self, request: Mapping[str, Any]) -> dict[str, Any]:
        candidate = request.get("candidate")
        summaries = request.get("source_job_summaries")
        if not isinstance(candidate, Mapping) or not isinstance(summaries, list):
            raise InvalidRequest(detail="candidate must be an object and source_job_summaries a list")
        artifact = {"candidate": dict(candidate), "source_job_summaries": list(summaries)}

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            context = {"company_id": str(company_id), "harness_id": str(harness_id)}
            try:
                scorecard = self._validator.validate(artifact, "skill", context)
            except ValidatorUnavailable as error:
                raise Unavailable(error.code) from error
            expected_hash = artifact_hash(artifact)
            if scorecard.get("artifact_hash") != expected_hash or scorecard.get("profile") != "skill":
                raise Unavailable("validator_mismatch")
            verdict = scorecard.get("verdict")
            if verdict not in ("pass", "fail"):
                raise Unavailable("validator_mismatch")
            candidate_id = store.save_candidate(
                company_id=company_id, harness_id=harness_id, artifact=artifact,
                artifact_hash=expected_hash, scorecard=scorecard, verdict=verdict,
            )
            return {"candidate_id": str(candidate_id), "artifact_hash": expected_hash, "verdict": verdict, "scorecard": scorecard}

        return self._call("skill.propose", request, run)

    def publish(self, request: Mapping[str, Any]) -> dict[str, Any]:
        candidate_id = _uuid(request.get("candidate_id"), "candidate_id")
        token = request.get("consent_token")

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            candidate = store.lock_candidate(candidate_id=candidate_id, company_id=company_id, harness_id=harness_id)
            if candidate is None:
                raise NotFound()
            if candidate.published_skill_id is not None:
                raise Conflict("already_published")
            self._check_scorecard(candidate.scorecard, candidate.artifact, harness_id)

            data = candidate.artifact["candidate"]
            token_id = None
            if data["scope"] == "company":
                try:
                    token_id = verify_consent(
                        token, self._consent_key, harness_id=str(harness_id), scope="skill_publish",
                        subject={"candidate_id": str(candidate_id), "artifact_hash": candidate.artifact_hash},
                        now=self._clock(),
                    )
                    store.use_consent(token_id=UUID(token_id), harness_id=harness_id, candidate_id=candidate_id)
                except (ConsentRejected, ConsentReused) as error:
                    raise Rejected(error.code) from error

            skill_id, version, supersedes = uuid4(), (1, 0, 0), None
            if data.get("supersedes"):
                supersedes = _uuid(data["supersedes"], "candidate.supersedes")
                latest = store.latest_version(skill_id=supersedes, company_id=company_id, harness_id=harness_id)
                if latest is None or latest["scope"] != data["scope"]:
                    raise Rejected("supersedes_not_visible")
                if data["scope"] == "company" and latest.get("author_person_id") != data.get("author_person_id"):
                    raise Rejected("supersedes_foreign_skill")
                major, minor, _ = (int(p) for p in latest["version"].split("."))
                skill_id, version = supersedes, (major, minor + 1, 0)

            provenance = {
                **dict(data.get("provenance") or {}),
                "scorecard_ref": candidate.scorecard["scorecard_id"],
                "consent_ref": token_id,
                "candidate_id": str(candidate_id),
            }
            version_text = store.insert_skill(
                skill_id=skill_id, version=version, candidate=candidate, provenance=provenance,
                supersedes=supersedes, created_at=self._clock(),
            )
            return {"skill_id": str(skill_id), "version": version_text}

        return self._call("skill.publish", request, run)

    def _check_scorecard(self, scorecard: Mapping[str, Any], artifact: Mapping[str, Any], harness_id: UUID) -> None:
        if scorecard.get("profile") != "skill":
            raise Rejected("wrong_profile")
        for refresh in (False, True):
            try:
                keys = self._validator.keys(refresh=refresh)
            except ValidatorUnavailable as error:
                raise Unavailable(error.code) from error
            try:
                verify_scorecard(scorecard, keys, artifact=artifact, harness_id=harness_id)
                return
            except ScoreCardRejected as error:
                if error.code == "unknown_key" and not refresh:
                    continue  # ключ мог смениться: перечитать /v1/keys один раз
                raise Rejected(error.code) from error

    # --- ретривал ---------------------------------------------------------------

    def index(self, request: Mapping[str, Any]) -> dict[str, Any]:
        docs = request.get("docs")
        if not isinstance(docs, list) or not 1 <= len(docs) <= MAX_DOCS:
            raise InvalidRequest(detail=f"docs must be a list of 1..{MAX_DOCS} documents")
        for index, doc in enumerate(docs):
            if not isinstance(doc, Mapping):
                raise InvalidRequest(detail=f"docs[{index}] must be an object")
            if not isinstance(doc.get("doc_id"), str) or not doc["doc_id"].strip() or len(doc["doc_id"]) > 200:
                raise InvalidRequest(detail=f"docs[{index}].doc_id is invalid")
            if doc.get("kind") not in DOC_KINDS:
                raise InvalidRequest(detail=f"docs[{index}].kind must be one of {list(DOC_KINDS)}")
            if not isinstance(doc.get("text"), str) or not doc["text"].strip() or len(doc["text"]) > MAX_TEXT:
                raise InvalidRequest(detail=f"docs[{index}].text is invalid")
            for name in ("source", "sensitivity"):
                if doc.get(name) is not None and not isinstance(doc[name], Mapping):
                    raise InvalidRequest(detail=f"docs[{index}].{name} must be an object")

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            return {"indexed": store.index_docs(company_id=company_id, harness_id=harness_id, docs=docs)}

        return self._call("retrieval.index", request, run)

    def search(self, request: Mapping[str, Any], via: Mapping[str, Any] | None = None) -> dict[str, Any]:
        query = request.get("query")
        if not isinstance(query, str) or not query.strip():
            raise InvalidRequest(detail="query must be a non-empty string")
        kinds = _str_list(request.get("kinds"), "kinds")
        if kinds and set(kinds) - set(DOC_KINDS):
            raise InvalidRequest(detail=f"kinds must be within {list(DOC_KINDS)}")
        limit = _limit(request.get("limit"))

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            hits = store.search_docs(company_id=company_id, harness_id=harness_id, query=query, kinds=kinds, limit=limit)
            return {"hits": hits}

        return self._call("retrieval.search", request, run, via)

    # --- политика ---------------------------------------------------------------

    def policy(self, request: Mapping[str, Any], via: Mapping[str, Any] | None = None) -> dict[str, Any]:
        employee_id = request.get("employee_id")
        sub_role = request.get("sub_role")
        for name, value in (("employee_id", employee_id), ("sub_role", sub_role)):
            if value is not None and (not isinstance(value, str) or not value):
                raise InvalidRequest(detail=f"{name} must be a non-empty string")

        def run(store: MetabrainStore, company_id: UUID, harness_id: UUID):
            try:
                return resolve_policy(self._policy, employee_id=employee_id, sub_role=sub_role)
            except PolicyError as error:
                raise InvalidRequest(detail=str(error)) from error

        return self._call("policy.get", request, run, via, allow_company_only=via is not None)
