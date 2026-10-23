import base64
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from psycopg import Connection
from psycopg.types.json import Jsonb
import rfc8785

from src.common.secrets import EnvSecretStore
from src.harness.journal import JournalStore
from src.harness.store import HarnessAccessDenied, HarnessStore


Decision = Literal["approve", "return", "incident"]
Surface = Literal["web", "telegram"]
ProposalKind = Literal[
    "passport",
    "recommendation",
    "action",
    "adapter",
    "connect",
    "skill_publish",
    "autonomy_change",
]


class ConsentError(PermissionError):
    _status_by_code = {
        "forbidden_harness": 403,
        "not_curator": 403,
        "invalid_signature": 403,
        "subject_changed": 403,
        "consent_required": 403,
        "consent_expired": 403,
        "consent_already_used": 403,
        "invalid_consent": 403,
    }

    def __init__(self, code: str):
        self.code = code
        self.status_code = self._status_by_code.get(code, 403)
        super().__init__(code)


class ProposalConflict(RuntimeError):
    status_code = 409
    code = "already_decided"


@dataclass(frozen=True)
class ConsentToken:
    token_id: UUID
    harness_id: UUID
    proposal_id: UUID
    subject_hash: str
    scope: str
    issued_by: UUID
    surface: Surface
    issued_at: datetime
    exp: datetime
    signing_public_key: str
    key_certificate: str
    signature: str


@dataclass(frozen=True)
class Proposal:
    id: UUID
    company_id: UUID
    harness_id: UUID
    kind: ProposalKind
    summary: str
    subject: dict[str, Any]
    sources: list[Any]
    score_ref: str | None
    status: str
    created_at: datetime


@dataclass(frozen=True)
class ProposalDecision:
    status: str
    consent_token: ConsentToken | None


class ProposalStore:
    def __init__(
        self,
        connection: Connection,
        *,
        secret_store: EnvSecretStore,
        signing_key_ref: str | Callable[[UUID], str],
        verification_key_ref: str = "env:CONSENT_PUBLIC_KEY",
        token_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if token_ttl <= timedelta(0):
            raise ValueError("token_ttl must be positive")
        self._connection = connection
        self._secret_store = secret_store
        self._signing_key_ref = signing_key_ref
        self._verification_key_ref = verification_key_ref
        self._token_ttl = token_ttl
        self._clock = clock
        self._harness_store = HarnessStore(connection)
        self._journal = JournalStore(connection)
        schema = Path(__file__).with_name("proposals.sql").read_text(
            encoding="utf-8"
        )
        with connection.transaction():
            connection.execute(schema, prepare=False)

    def create_proposal(
        self,
        *,
        person_id: UUID,
        company_id: UUID,
        harness_id: UUID,
        kind: ProposalKind,
        summary: str,
        subject: Mapping[str, Any],
        sources: Sequence[Any] = (),
        score_ref: str | None = None,
    ) -> Proposal:
        if not summary.strip():
            raise ValueError("proposal summary is required")
        proposal_id = uuid4()
        subject_value = dict(subject)
        source_values = list(sources)
        with self._connection.transaction():
            try:
                self._harness_store.require_harness_access(
                    person_id, harness_id, company_id
                )
            except HarnessAccessDenied as error:
                raise ConsentError("forbidden_harness") from error
            row = self._connection.execute(
                """
                INSERT INTO proposals
                    (id, company_id, harness_id, kind, summary, subject, sources,
                     score_ref)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING created_at
                """,
                (
                    proposal_id,
                    company_id,
                    harness_id,
                    kind,
                    summary,
                    Jsonb(subject_value),
                    Jsonb(source_values),
                    score_ref,
                ),
            ).fetchone()
        return Proposal(
            id=proposal_id,
            company_id=company_id,
            harness_id=harness_id,
            kind=kind,
            summary=summary,
            subject=subject_value,
            sources=source_values,
            score_ref=score_ref,
            status="pending",
            created_at=row[0],
        )

    def list_proposals(
        self,
        *,
        person_id: UUID,
        harness_id: UUID,
        status: str | None = None,
        kind: str | None = None,
        employee_id: UUID | None = None,
    ) -> tuple[Proposal, ...]:
        with self._connection.transaction():
            company_id = self._require_access(person_id, harness_id)
            conditions = ["company_id = %s", "harness_id = %s"]
            params: list[Any] = [company_id, harness_id]
            if status is not None:
                conditions.append("status = %s")
                params.append(status)
            if kind is not None:
                conditions.append("kind = %s")
                params.append(kind)
            if employee_id is not None:
                conditions.append("subject ->> 'employee_id' = %s")
                params.append(str(employee_id))
            rows = self._connection.execute(
                f"""
                SELECT id, company_id, harness_id, kind, summary, subject,
                       sources, score_ref, status, created_at
                FROM proposals
                WHERE {' AND '.join(conditions)}
                ORDER BY created_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return tuple(self._proposal_from_row(row) for row in rows)

    def decide(
        self,
        *,
        person_id: UUID,
        harness_id: UUID,
        proposal_id: UUID,
        decision: Decision,
        actor_person_id: UUID,
        surface: Surface,
        channel_user_id: str | None = None,
        comment: str | None = None,
    ) -> ProposalDecision:
        if decision not in {"approve", "return", "incident"}:
            raise ValueError("unsupported proposal decision")
        if surface not in {"web", "telegram"}:
            raise ValueError("unsupported decision surface")
        if actor_person_id != person_id:
            raise ConsentError("not_curator")
        curator = self._connection.execute(
            """
            SELECT 1
            FROM harnesses
            WHERE id = %s AND owner_person_id = %s
            """,
            (harness_id, actor_person_id),
        ).fetchone()
        if curator is None:
            raise ConsentError("not_curator")
        if surface == "telegram":
            if not channel_user_id:
                raise ConsentError("not_curator")
            registered = self._connection.execute(
                """
                SELECT 1
                FROM channel_identities
                WHERE channel = 'telegram'
                  AND channel_user_id = %s
                  AND person_id = %s
                """,
                (channel_user_id, actor_person_id),
            ).fetchone()
            if registered is None:
                raise ConsentError("not_curator")

        token = None
        with self._connection.transaction():
            company_id = self._require_access(person_id, harness_id)
            row = self._connection.execute(
                """
                SELECT kind, summary, subject, sources, score_ref, status
                FROM proposals
                WHERE id = %s AND company_id = %s AND harness_id = %s
                FOR UPDATE
                """,
                (proposal_id, company_id, harness_id),
            ).fetchone()
            if row is None:
                raise ConsentError("forbidden_harness")
            if row[5] != "pending":
                raise ProposalConflict()

            status = {
                "approve": "approved",
                "return": "returned",
                "incident": "incident",
            }[decision]
            decided_at = self._clock()
            self._connection.execute(
                """
                UPDATE proposals
                SET status = %s, decision_comment = %s, decided_by = %s,
                    decided_surface = %s, decided_at = %s
                WHERE id = %s
                """,
                (
                    status,
                    comment,
                    actor_person_id,
                    surface,
                    decided_at,
                    proposal_id,
                ),
            )

            if decision == "approve":
                token = self._issue_token(
                    harness_id=harness_id,
                    proposal_id=proposal_id,
                    subject=row[2],
                    scope=row[0],
                    issued_by=actor_person_id,
                    surface=surface,
                    issued_at=decided_at,
                )
                self._connection.execute(
                    """
                    INSERT INTO consent_tokens
                        (token_id, harness_id, proposal_id, subject_hash, scope,
                         issued_by, surface, issued_at, exp, signature)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        token.token_id,
                        token.harness_id,
                        token.proposal_id,
                        token.subject_hash,
                        token.scope,
                        token.issued_by,
                        token.surface,
                        token.issued_at,
                        token.exp,
                        token.signature,
                    ),
                )

            self._journal.append_entry(
                person_id=person_id,
                company_id=company_id,
                harness_id=harness_id,
                kind="proposal.decision",
                payload={
                    "proposal_id": str(proposal_id),
                    "decision": decision,
                    "status": status,
                    "actor": str(actor_person_id),
                    "surface": surface,
                    "comment": comment,
                },
                sources=row[3],
                score_ref=row[4],
            )
        return ProposalDecision(status=status, consent_token=token)

    def apply_proposal(
        self,
        *,
        person_id: UUID,
        harness_id: UUID,
        proposal_id: UUID,
        consent_token: ConsentToken | None,
        current_subject: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if consent_token is None:
            raise ConsentError("consent_required")
        if not self.verify_token_signature(consent_token):
            raise ConsentError("invalid_signature")
        with self._connection.transaction():
            company_id = self._require_access(person_id, harness_id)
            if consent_token.harness_id != harness_id:
                raise ConsentError("forbidden_harness")
            if consent_token.proposal_id != proposal_id:
                raise ConsentError("invalid_consent")
            proposal = self._connection.execute(
                """
                SELECT subject, status
                FROM proposals
                WHERE id = %s AND harness_id = %s
                FOR UPDATE
                """,
                (proposal_id, harness_id),
            ).fetchone()
            if proposal is None:
                raise ConsentError("forbidden_harness")
            if proposal[1] != "approved":
                raise ConsentError("invalid_consent")
            subject = proposal[0]
            if current_subject is not None:
                subject = dict(current_subject)
            if self._subject_hash(subject) != consent_token.subject_hash:
                raise ConsentError("subject_changed")
            if self._clock() >= consent_token.exp:
                raise ConsentError("consent_expired")

            token_row = self._connection.execute(
                """
                SELECT subject_hash, scope, issued_by, surface, issued_at, exp,
                       signature, used_at
                FROM consent_tokens
                WHERE token_id = %s AND proposal_id = %s AND harness_id = %s
                FOR UPDATE
                """,
                (consent_token.token_id, proposal_id, harness_id),
            ).fetchone()
            if token_row is None or not self._matches_token(consent_token, token_row):
                raise ConsentError("invalid_consent")
            if token_row[7] is not None:
                raise ConsentError("consent_already_used")
            self._connection.execute(
                "UPDATE consent_tokens SET used_at = %s WHERE token_id = %s",
                (self._clock(), consent_token.token_id),
            )
            self._journal.append_entry(
                person_id=person_id,
                company_id=company_id,
                harness_id=harness_id,
                kind="proposal.applied",
                payload={
                    "proposal_id": str(proposal_id),
                    "token_id": str(consent_token.token_id),
                    "scope": consent_token.scope,
                },
            )
            return subject

    def withdraw(self, *, person_id: UUID, harness_id: UUID, proposal_id: UUID, reason: str) -> bool:
        """Отзыв ждущего предложения системой (C-6: новая версия заменила прежнюю) — не решение куратора."""
        with self._connection.transaction():
            row = self._connection.execute(
                """
                UPDATE proposals SET status = 'returned', decision_comment = %s, decided_at = %s
                WHERE id = %s AND harness_id = %s AND status = 'pending'
                RETURNING company_id
                """,
                (reason, self._clock(), proposal_id, harness_id),
            ).fetchone()
            if row is None:
                return False
            self._journal.append_entry(
                person_id=person_id, company_id=row[0], harness_id=harness_id, kind="proposal.withdrawn",
                payload={"proposal_id": str(proposal_id), "reason": reason},
            )
        return True

    def token_document(self, token: ConsentToken) -> dict[str, Any]:
        """Токен для мета-мозга (C-5 `skill.publish`): подписанные поля как в подписи и сама подпись."""
        return {**self._token_payload(token), "signature": token.signature}

    def verify_token_signature(self, token: ConsentToken) -> bool:
        try:
            self._public_key().verify(
                self._decode_signature(token.key_certificate),
                rfc8785.dumps(self._key_certificate_payload(token.harness_id, token.signing_public_key)),
            )
            Ed25519PublicKey.from_public_bytes(self._decode_signature(token.signing_public_key)).verify(
                self._decode_signature(token.signature),
                rfc8785.dumps(self._token_payload(token)),
            )
            return True
        except (InvalidSignature, ValueError, TypeError):
            return False

    def _issue_token(
        self,
        *,
        harness_id: UUID,
        proposal_id: UUID,
        subject: Mapping[str, Any],
        scope: str,
        issued_by: UUID,
        surface: Surface,
        issued_at: datetime,
    ) -> ConsentToken:
        signing_key = self._private_key(harness_id)
        signing_public_key = base64.urlsafe_b64encode(
            signing_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        ).decode("ascii").rstrip("=")
        key_certificate = base64.urlsafe_b64encode(
            self._root_private_key(harness_id).sign(
                rfc8785.dumps(self._key_certificate_payload(harness_id, signing_public_key))
            )
        ).decode("ascii").rstrip("=")
        unsigned = ConsentToken(
            token_id=uuid4(),
            harness_id=harness_id,
            proposal_id=proposal_id,
            subject_hash=self._subject_hash(subject),
            scope=scope,
            issued_by=issued_by,
            surface=surface,
            issued_at=issued_at,
            exp=issued_at + self._token_ttl,
            signing_public_key=signing_public_key,
            key_certificate=key_certificate,
            signature="",
        )
        signature = signing_key.sign(rfc8785.dumps(self._token_payload(unsigned)))
        return ConsentToken(
            token_id=unsigned.token_id,
            harness_id=unsigned.harness_id,
            proposal_id=unsigned.proposal_id,
            subject_hash=unsigned.subject_hash,
            scope=unsigned.scope,
            issued_by=unsigned.issued_by,
            surface=unsigned.surface,
            issued_at=unsigned.issued_at,
            exp=unsigned.exp,
            signing_public_key=unsigned.signing_public_key,
            key_certificate=unsigned.key_certificate,
            signature=base64.urlsafe_b64encode(signature)
            .decode("ascii")
            .rstrip("="),
        )

    def _private_key(self, harness_id: UUID) -> Ed25519PrivateKey:
        root_seed = self._root_private_key(harness_id).private_bytes(
            Encoding.Raw, PrivateFormat.Raw, NoEncryption()
        )
        derived_seed = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=harness_id.bytes,
            info=b"metabrain:consent-signing:v1",
        ).derive(root_seed)
        return Ed25519PrivateKey.from_private_bytes(derived_seed)

    def _root_private_key(self, harness_id: UUID) -> Ed25519PrivateKey:
        reference = (
            self._signing_key_ref(harness_id)
            if callable(self._signing_key_ref)
            else self._signing_key_ref
        )
        encoded_key = self._secret_store.resolve(reference)
        try:
            raw_key = base64.b64decode(encoded_key, validate=True)
            return Ed25519PrivateKey.from_private_bytes(raw_key)
        except (ValueError, TypeError) as error:
            raise ValueError(f"invalid Ed25519 signing key for harness {harness_id}") from error

    def _public_key(self) -> Ed25519PublicKey:
        encoded_key = self._secret_store.resolve(self._verification_key_ref)
        try:
            raw_key = base64.b64decode(encoded_key, validate=True)
            return Ed25519PublicKey.from_public_bytes(raw_key)
        except (ValueError, TypeError) as error:
            raise ValueError("invalid Ed25519 consent verification key") from error

    @staticmethod
    def _key_certificate_payload(harness_id: UUID, signing_public_key: str) -> dict[str, str]:
        return {
            "harness_id": str(harness_id),
            "signing_public_key": signing_public_key,
        }

    def _token_payload(self, token: ConsentToken) -> dict[str, Any]:
        return {
            "token_id": str(token.token_id),
            "harness_id": str(token.harness_id),
            "proposal_id": str(token.proposal_id),
            "subject_hash": token.subject_hash,
            "scope": token.scope,
            "issued_by": str(token.issued_by),
            "surface": token.surface,
            "issued_at": token.issued_at.astimezone(timezone.utc).isoformat(),
            "exp": token.exp.astimezone(timezone.utc).isoformat(),
            "signing_public_key": token.signing_public_key,
            "key_certificate": token.key_certificate,
        }

    def _subject_hash(self, subject: Mapping[str, Any]) -> str:
        return sha256(rfc8785.dumps(subject)).hexdigest()

    def _decode_signature(self, signature: str) -> bytes:
        padding = "=" * (-len(signature) % 4)
        return base64.b64decode(
            signature + padding,
            altchars=b"-_",
            validate=True,
        )

    def _matches_token(self, token: ConsentToken, row: tuple[Any, ...]) -> bool:
        return (
            token.subject_hash,
            token.scope,
            token.issued_by,
            token.surface,
            token.issued_at,
            token.exp,
            token.signature,
        ) == row[:7]

    def _require_access(self, person_id: UUID, harness_id: UUID) -> UUID:
        try:
            return self._harness_store.require_harness_access(person_id, harness_id)
        except HarnessAccessDenied as error:
            raise ConsentError("forbidden_harness") from error

    def _proposal_from_row(self, row: tuple[Any, ...]) -> Proposal:
        return Proposal(
            id=row[0],
            company_id=row[1],
            harness_id=row[2],
            kind=row[3],
            summary=row[4],
            subject=row[5],
            sources=row[6],
            score_ref=row[7],
            status=row[8],
            created_at=row[9],
        )
