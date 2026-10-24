"""Вход в веб-кабинет: Telegram Login по OpenID Connect + PKCE, сессии, CSRF (C-1, ADR-025)."""

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import logging
from pathlib import Path
import secrets
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import UUID

import httpx
import jwt
from psycopg import Connection

from src.harness.journal import JournalStore
from src.harness.store import HarnessStore, Registration


SCHEMA_PATH = Path(__file__).with_name("web_auth.sql")
ISSUER = "https://oauth.telegram.org"
AUTHORIZATION_URL = "https://oauth.telegram.org/auth"
TOKEN_URL = "https://oauth.telegram.org/token"
JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
ALGORITHMS = ["RS256", "ES256", "EdDSA"]
STATE_TTL = timedelta(minutes=10)
BOOTSTRAP_TTL = timedelta(minutes=15)
JWKS_TTL = 3600.0
READ_OPERATIONS = frozenset({
    "employee.list", "employee.get", "employee.autonomy", "proposal.list",
    "journal.list", "landscape.map", "skills.list",
    "process.list", "process.get", "project.list", "project.get",
    "task.list", "planning.summary", "settings.llm.get", "catalog.list", "org.get", "improvement.scan", "flow.status",
    "settings.llm.usage", "settings.metabrain.get",
    "connections.catalog", "connections.custom.list",
})
logger = logging.getLogger(__name__)


class WebAuthError(PermissionError):
    """Вход не состоялся или сессия недействительна — 401."""

    status_code = 401

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class CsrfRequired(PermissionError):
    status_code = 403
    code = "csrf_required"


class WebAuthNotConfigured(RuntimeError):
    code = "web_login_not_configured"


@dataclass(frozen=True)
class OidcConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    authorization_url: str = AUTHORIZATION_URL
    token_url: str = TOKEN_URL
    jwks_url: str = JWKS_URL
    issuer: str = ISSUER


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _invite_token(invite: Any) -> str | None:
    """Нормализуем приглашение: принимаем голый код ИЛИ ссылку t.me/<bot>?start=<код>."""
    if not isinstance(invite, str) or not invite.strip():
        return None
    value = invite.strip()
    if "start=" in value or value.startswith("http"):
        start = parse_qs(urlparse(value).query).get("start")
        if start and start[0].strip():
            return start[0].strip()
    return value


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class WebAuth:
    def __init__(
        self,
        connection: Connection,
        *,
        oidc: OidcConfig | None,
        session_ttl: timedelta,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self._c = connection
        self._oidc = oidc
        self._ttl = session_ttl
        self._http = http_client or httpx.Client(timeout=15)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._harness = HarnessStore(connection)
        self._journal = JournalStore(connection)
        self._jwks: tuple[float, jwt.PyJWKSet] | None = None
        self._jwks_lock = threading.Lock()
        with connection.transaction():
            connection.execute(SCHEMA_PATH.read_text(encoding="utf-8"), prepare=False)

    # --- шаг 1: адрес авторизации Telegram -------------------------------------------

    def start(self) -> dict[str, Any]:
        oidc = self._require_config()
        state, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(3))
        now = self._clock()
        self._c.execute(
            """
            INSERT INTO web_login_states (state_hash, code_verifier, nonce, created_at, exp)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (_hash(state), verifier, nonce, now, now + STATE_TTL),
        )
        self._c.execute("DELETE FROM web_login_states WHERE exp < %s", (now - STATE_TTL,))
        query = urlencode({
            "client_id": oidc.client_id,
            "redirect_uri": oidc.redirect_uri,
            "response_type": "code",
            "scope": "openid profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
        })
        return {"authorization_url": f"{oidc.authorization_url}?{query}", "state": state,
                "exp": (now + STATE_TTL).isoformat()}

    # --- шаг 2: колбэк — обмен кода, проверка id_token, сессия --------------------------

    def callback(self, *, code: str, state: str) -> dict[str, Any]:
        oidc = self._require_config()
        now = self._clock()
        row = self._c.execute(
            """
            UPDATE web_login_states SET used_at = %s
            WHERE state_hash = %s AND used_at IS NULL AND exp > %s
            RETURNING code_verifier, nonce
            """,
            (now, _hash(state), now),
        ).fetchone()
        if row is None:
            logger.warning("web auth callback failed: invalid_state")
            raise WebAuthError("invalid_state")
        verifier, nonce = row

        claims = self._verify_id_token(self._exchange(oidc, code, verifier), oidc, nonce)
        telegram_id = claims.get("id")
        if isinstance(telegram_id, str) and telegram_id.isdigit():
            telegram_id = int(telegram_id)
        if not isinstance(telegram_id, int) or isinstance(telegram_id, bool):
            logger.warning(
                "Telegram id_token has no integer id; claims=%s id_type=%s",
                sorted(claims.keys()), type(claims.get("id")).__name__,
            )
            raise WebAuthError("telegram_id_missing")
        identity = self._harness.identity_for_channel_user(channel="telegram", channel_user_id=str(telegram_id))
        if identity is None:
            # Telegram-личность подтверждена, но организации ещё нет — тикет на её создание в кабинете (ADR-030).
            name = claims.get("name") if isinstance(claims.get("name"), str) else ""
            ticket = secrets.token_urlsafe(32)
            self._c.execute(
                """
                INSERT INTO web_bootstrap_tickets (ticket_hash, telegram_id, name, created_at, exp)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (_hash(ticket), str(telegram_id), name[:200], now, now + BOOTSTRAP_TTL),
            )
            self._c.execute("DELETE FROM web_bootstrap_tickets WHERE exp < %s", (now - BOOTSTRAP_TTL,))
            return {"bootstrap": True, "ticket": ticket, "name": name[:200],
                    "expires_in": int(BOOTSTRAP_TTL.total_seconds())}
        return self._issue_session(identity, now)

    def _issue_session(self, identity: Registration, now: datetime) -> dict[str, Any]:
        session_token, csrf_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        exp = now + self._ttl
        self._c.execute(
            """
            INSERT INTO web_sessions (token_hash, csrf_hash, person_id, company_id, harness_id, created_at, exp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (_hash(session_token), _hash(csrf_token), identity.person_id, identity.company_id,
             identity.harness_id, now, exp),
        )
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="auth.web.login", payload={"surface": "web", "exp": exp.isoformat()},
        )
        # expires_in — по часам харнесса: кабинет ставит cookie на этот срок, не сверяя свои часы с харнессом.
        return {"session_token": session_token, "csrf_token": csrf_token, "exp": exp.isoformat(),
                "expires_in": int(self._ttl.total_seconds()), "person_id": str(identity.person_id),
                "company_id": str(identity.company_id), "harness_id": str(identity.harness_id)}

    def bootstrap(self, *, ticket: Any, company_name: Any = None, invite: Any = None) -> dict[str, Any]:
        """Саморегистрация в кабинете (ADR-030): по тикету из колбэка создаём организацию
        (``company_name``) ИЛИ присоединяемся к существующей по коду приглашения (``invite``)."""
        self._require_config()
        if not isinstance(ticket, str) or not ticket:
            raise WebAuthError("bootstrap_ticket_invalid")
        invite_token = _invite_token(invite)
        if invite_token is None and (not isinstance(company_name, str) or not company_name.strip()):
            raise WebAuthError("company_name_required")
        now = self._clock()
        # Инвайт резолвим ДО гашения одноразового тикета: при опечатке в коде тикет
        # не сгорает и вход можно повторить.
        company_id: UUID | None = None
        if invite_token is not None:
            company_id = self._company_for_invite(invite_token, now)
            if company_id is None:
                raise WebAuthError("invite_invalid")
        row = self._c.execute(
            """
            UPDATE web_bootstrap_tickets SET used_at = %s
            WHERE ticket_hash = %s AND used_at IS NULL AND exp > %s
            RETURNING telegram_id
            """,
            (now, _hash(ticket), now),
        ).fetchone()
        if row is None:
            raise WebAuthError("bootstrap_ticket_invalid")
        telegram_id = row[0]
        identity = self._harness.identity_for_channel_user(channel="telegram", channel_user_id=telegram_id)
        created = identity is None
        joined = False
        if identity is None:
            try:
                if company_id is not None:
                    identity = self._harness.register_person(
                        channel="telegram", channel_user_id=telegram_id, trusted_company_id=company_id,
                    )
                    joined = True
                else:
                    identity = self._harness.register_person(
                        channel="telegram", channel_user_id=telegram_id, company_name=company_name.strip()[:200],
                    )
            except Exception as error:   # гонка или отказ регистрации
                raise WebAuthError("bootstrap_failed") from error
        # created=True и joined=False → API наполнит НОВУЮ организацию шаблоном (процессы/проекты).
        # joined=True → орг уже существует, шаблон не трогаем.
        return {**self._issue_session(identity, now), "created": created, "joined": joined}

    def _company_for_invite(self, token: str, now: datetime) -> UUID | None:
        """Резолвим код приглашения в company_id. Хэш совпадает с company.invite (api.py)."""
        row = self._c.execute(
            "SELECT company_id FROM company_invites WHERE token_hash = %s AND exp > %s",
            (_hash(token), now),
        ).fetchone()
        return row[0] if row is not None else None

    def _exchange(self, oidc: OidcConfig, code: str, verifier: str) -> str:
        basic = base64.b64encode(f"{oidc.client_id}:{oidc.client_secret}".encode()).decode()
        try:
            response = self._http.post(
                oidc.token_url,
                data={"grant_type": "authorization_code", "code": code, "redirect_uri": oidc.redirect_uri,
                      "client_id": oidc.client_id, "code_verifier": verifier},
                headers={"Authorization": f"Basic {basic}"},
            )
        except httpx.HTTPError as error:
            raise WebAuthError("token_exchange_failed") from error
        if response.status_code != 200:
            logger.warning("Telegram token endpoint returned HTTP %s", response.status_code)
            raise WebAuthError("token_exchange_failed")
        try:
            body = response.json()
        except ValueError as error:
            logger.warning("Telegram token endpoint returned non-JSON response")
            raise WebAuthError("token_exchange_failed") from error
        if not isinstance(body, dict):
            raise WebAuthError("token_exchange_failed")
        if body.get("error"):
            # Telegram может вернуть 200 с объектом ошибки (например, invalid_grant).
            logger.warning("Telegram token endpoint error=%s", body.get("error"))
            raise WebAuthError("token_exchange_failed")
        id_token = body.get("id_token")
        if not isinstance(id_token, str):
            logger.warning("Telegram token endpoint returned no id_token; keys=%s", sorted(body.keys()))
            raise WebAuthError("id_token_missing")
        return id_token

    def _verify_id_token(self, id_token: str, oidc: OidcConfig, nonce: str) -> dict[str, Any]:
        try:
            kid = jwt.get_unverified_header(id_token).get("kid")
        except jwt.PyJWTError as error:
            raise WebAuthError("id_token_malformed") from error
        key = self._key(oidc, kid)
        if key.algorithm_name not in ALGORITHMS:
            raise WebAuthError("id_token_algorithm_not_allowed")
        try:
            # Алгоритм — из ключа JWKS, а не из заголовка токена: подмена алгоритма невозможна.
            claims = jwt.decode(
                id_token, key=key.key, algorithms=[key.algorithm_name], audience=oidc.client_id, issuer=oidc.issuer,
                options={"require": ["exp", "iat", "iss", "aud"]}, leeway=30,
            )
        except jwt.PyJWTError as error:
            logger.warning("Telegram id_token rejected: %s", type(error).__name__)
            raise WebAuthError("id_token_invalid") from error
        claim_nonce = claims.get("nonce")
        if claim_nonce is None:
            # Telegram OIDC не гарантирует nonce в id_token (ручной OIDC-flow в документации его не содержит).
            # Защита остаётся на одноразовом state и PKCE; если nonce пришёл — сверяем.
            logger.warning("Telegram id_token has no nonce; relying on state and PKCE")
        elif not hmac.compare_digest(str(claim_nonce), nonce):
            logger.warning("Telegram id_token nonce mismatch")
            raise WebAuthError("nonce_mismatch")
        return claims

    def _key(self, oidc: OidcConfig, kid: Any):
        for refresh in (False, True):
            keys = self._keys(oidc, refresh=refresh)
            for key in keys.keys:
                if key.key_id == kid:
                    return key
        raise WebAuthError("unknown_signing_key")

    def _keys(self, oidc: OidcConfig, *, refresh: bool) -> jwt.PyJWKSet:
        with self._jwks_lock:
            if refresh or self._jwks is None or time.monotonic() - self._jwks[0] > JWKS_TTL:
                try:
                    response = self._http.get(oidc.jwks_url)
                    response.raise_for_status()
                    self._jwks = (time.monotonic(), jwt.PyJWKSet.from_dict(response.json()))
                except (httpx.HTTPError, jwt.PyJWTError, ValueError) as error:
                    raise WebAuthError("jwks_unavailable") from error
            return self._jwks[1]

    # --- сессии ---------------------------------------------------------------------------

    def resolve(self, session_token: Any) -> tuple[Registration, str]:
        """Сессия → (идентичность, хэш CSRF)."""
        if not isinstance(session_token, str) or not session_token:
            raise WebAuthError("session_required")
        row = self._c.execute(
            """
            SELECT person_id, company_id, harness_id, csrf_hash FROM web_sessions
            WHERE token_hash = %s AND revoked_at IS NULL AND exp > %s
            """,
            (_hash(session_token), self._clock()),
        ).fetchone()
        if row is None:
            raise WebAuthError("session_invalid")
        return Registration(person_id=row[0], company_id=row[1], harness_id=row[2]), row[3]

    @staticmethod
    def check_csrf(csrf_hash: str, csrf_token: Any) -> None:
        if not isinstance(csrf_token, str) or not hmac.compare_digest(csrf_hash, _hash(csrf_token)):
            raise CsrfRequired()

    def logout(self, *, session_token: Any, csrf_token: Any) -> dict[str, Any]:
        identity, csrf_hash = self.resolve(session_token)
        self.check_csrf(csrf_hash, csrf_token)
        self._c.execute(
            "UPDATE web_sessions SET revoked_at = %s WHERE token_hash = %s",
            (self._clock(), _hash(session_token)),
        )
        self._journal.append_entry(
            person_id=identity.person_id, company_id=identity.company_id, harness_id=identity.harness_id,
            kind="auth.web.logout", payload={"surface": "web"},
        )
        return {}

    def _require_config(self) -> OidcConfig:
        if self._oidc is None:
            raise WebAuthNotConfigured("Telegram OIDC client is not configured")
        return self._oidc
