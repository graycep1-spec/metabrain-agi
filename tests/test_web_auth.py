"""T-31: вход в кабинет по Telegram OIDC + PKCE, сессии, CSRF — GT-c1-09, -10 (C-1, ADR-025)."""

import base64
from datetime import timedelta
from hashlib import sha256
import time
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import httpx
import jwt
import pytest

from src.common.secrets import EnvSecretStore
from src.harness.api import create_app as create_harness
from src.harness.web_auth import OidcConfig, WebAuth, WebAuthError
from src.web.app import SESSION_COOKIE, create_app as create_web, http_harness
from tests.support_harness import CONSENT_KEY, CONSENT_PUBLIC_KEY, harness_env


CLIENT_ID = "7123456789"
CLIENT_SECRET = "telegram-client-secret"
REDIRECT = "https://cabinet.example.test/auth/callback"
API_TOKEN = "web-auth-test-api-token"


class FakeTelegram:
    """oauth.telegram.org: JWKS и token endpoint, проверка Basic-авторизации и PKCE."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "tg-key-1"
        self.codes: dict[str, tuple[str, dict]] = {}
        self.token_requests = 0

    def jwks(self, key=None, kid=None):
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk((key or self.key).public_key(), as_dict=True)
        return {"keys": [{**jwk, "kid": kid or self.kid, "alg": "RS256", "use": "sig"}]}

    def issue(self, code: str, challenge: str, claims: dict, *, key=None, kid=None):
        token = jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})
        self.codes[code] = (challenge, token)

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/jwks.json":
            return httpx.Response(200, json=self.jwks())
        if request.url.path == "/token":
            self.token_requests += 1
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if request.headers.get("Authorization") != expected or form.get("redirect_uri") != REDIRECT:
                return httpx.Response(401, json={"error": "invalid_client"})
            if form.get("code") == "error-200":
                return httpx.Response(200, json={"error": "invalid_grant"})
            challenge, token = self.codes.get(form.get("code"), (None, None))
            verifier_hash = base64.urlsafe_b64encode(sha256(form.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
            if token is None or verifier_hash != challenge:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "a", "token_type": "Bearer", "expires_in": 3600, "id_token": token})
        return httpx.Response(404)


def oidc():
    return OidcConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT,
                      authorization_url="https://oauth.telegram.org/auth", token_url="https://oauth.telegram.org/token",
                      jwks_url="https://oauth.telegram.org/.well-known/jwks.json")


def make_auth(env, telegram: FakeTelegram, ttl=timedelta(hours=12)) -> WebAuth:
    return WebAuth(env.connection, oidc=oidc(), session_ttl=ttl, clock=env.clock,
                   http_client=httpx.Client(transport=httpx.MockTransport(telegram.handler)))


def claims(telegram_id: int, nonce: str, **changes) -> dict:
    now = int(time.time())
    base = {"iss": "https://oauth.telegram.org", "aud": CLIENT_ID, "sub": "opaque-sub", "iat": now,
            "exp": now + 600, "id": telegram_id, "name": "Анна", "nonce": nonce}
    base.update(changes)
    return {k: v for k, v in base.items() if v is not None}


def begin(auth: WebAuth) -> dict:
    started = auth.start()
    query = {k: v[0] for k, v in parse_qs(urlsplit(started["authorization_url"]).query).items()}
    assert query["client_id"] == CLIENT_ID and query["redirect_uri"] == REDIRECT
    assert query["scope"] == "openid profile" and query["code_challenge_method"] == "S256"
    return query


def telegram_id(identity_suffix: str = "a") -> int:
    return int("10" + identity_suffix.encode().hex())


def login(env, auth, telegram, code="code-1", **claim_changes):
    query = begin(auth)
    token_claims = {**claims(telegram_id(), query["nonce"]), **claim_changes}
    telegram.issue(code, query["code_challenge"], {k: v for k, v in token_claims.items() if v is not None})
    return auth.callback(code=code, state=query["state"])


# --- GT-c1-09: вход ------------------------------------------------------------------------

def test_valid_login_creates_a_session_bound_to_the_registered_person():
    with harness_env() as env:
        me = env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        session = login(env, auth, telegram)

        identity, _ = auth.resolve(session["session_token"])
        assert (identity.person_id, identity.harness_id) == (me.person_id, me.harness_id)
        stored = env.connection.execute("SELECT token_hash FROM web_sessions").fetchone()[0]
        assert stored != session["session_token"] and len(stored) == 64          # только хэш
        kinds = [r[0] for r in env.connection.execute("SELECT kind FROM journal_entries ORDER BY created_at").fetchall()]
        assert "auth.web.login" in kinds


def test_id_token_without_nonce_is_accepted():
    """Telegram OIDC может не вернуть nonce; защита — одноразовый state и PKCE."""
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        query = begin(auth)
        telegram.issue("no-nonce", query["code_challenge"], claims(telegram_id(), None))
        session = auth.callback(code="no-nonce", state=query["state"])
        assert session["session_token"]


def test_telegram_id_as_numeric_string_is_accepted():
    """Telegram может вернуть id строкой; для регистрации он приводится к int."""
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        session = login(env, auth, telegram, id=str(telegram_id()))
        assert session["session_token"]


@pytest.mark.parametrize("changes, code", [
    ({"aud": "999"}, "id_token_invalid"),
    ({"iss": "https://evil.example"}, "id_token_invalid"),
    ({"exp": int(time.time()) - 120}, "id_token_invalid"),
    ({"nonce": "other-nonce"}, "nonce_mismatch"),
    ({"id": None}, "telegram_id_missing"),
])
def test_gt_c1_09_bad_id_tokens_and_unknown_people_get_401(changes, code):
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        with pytest.raises(WebAuthError) as error:
            login(env, auth, telegram, **changes)
        assert error.value.code == code
        assert env.connection.execute("SELECT count(*) FROM web_sessions").fetchone()[0] == 0


def test_gt_c1_09_token_signed_by_someone_else_is_rejected():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        query = begin(auth)
        forger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        telegram.issue("c", query["code_challenge"], claims(telegram_id(), query["nonce"]), key=forger)
        with pytest.raises(WebAuthError) as same_kid:
            auth.callback(code="c", state=query["state"])
        assert same_kid.value.code == "id_token_invalid"

        query = begin(auth)
        telegram.issue("d", query["code_challenge"], claims(telegram_id(), query["nonce"]), key=forger, kid="unknown")
        with pytest.raises(WebAuthError) as unknown_kid:
            auth.callback(code="d", state=query["state"])
        assert unknown_kid.value.code == "unknown_signing_key"


def test_token_endpoint_200_error_is_rejected():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        query = begin(auth)
        telegram.issue("error-200", query["code_challenge"], claims(telegram_id(), query["nonce"]))
        with pytest.raises(WebAuthError) as error:
            auth.callback(code="error-200", state=query["state"])
        assert error.value.code == "token_exchange_failed"


def test_gt_c1_09_state_is_single_use_and_must_be_ours():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        query = begin(auth)
        telegram.issue("c", query["code_challenge"], claims(telegram_id(), query["nonce"]))
        auth.callback(code="c", state=query["state"])
        for state in (query["state"], "never-issued"):
            with pytest.raises(WebAuthError) as error:
                auth.callback(code="c", state=state)
            assert error.value.code == "invalid_state"


def test_pkce_verifier_is_sent_and_checked_by_telegram():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        query = begin(auth)
        telegram.issue("c", "challenge-of-another-login", claims(telegram_id(), query["nonce"]))
        with pytest.raises(WebAuthError) as error:
            auth.callback(code="c", state=query["state"])
        assert error.value.code == "token_exchange_failed"


def test_expired_state_and_expired_session_are_rejected():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram, ttl=timedelta(hours=1))
        query = begin(auth)
        env.clock.advance(minutes=11)
        with pytest.raises(WebAuthError):
            auth.callback(code="x", state=query["state"])

        session = login(env, auth, telegram, code="fresh")
        env.clock.advance(hours=2)
        with pytest.raises(WebAuthError) as error:
            auth.resolve(session["session_token"])
        assert error.value.code == "session_invalid"


# --- GT-c1-10: CSRF и сессия в операциях C-1 -----------------------------------------------------

def harness_client(env, auth) -> TestClient:
    app = create_harness(env.connection, api_token=API_TOKEN, app_base_url="https://cabinet.example.test",
                         secret_store=EnvSecretStore({
                             "CONSENT_SIGNING_KEY": CONSENT_KEY,
                             "CONSENT_PUBLIC_KEY": CONSENT_PUBLIC_KEY,
                         }), web_auth=auth,
                         workforce=env.workforce, researcher=env.researcher)
    return TestClient(app)


def op(client, name, **payload):
    response = client.post(f"/v1/operations/{name}", json=payload, headers={"Authorization": f"Bearer {API_TOKEN}"})
    return response.status_code, response.json()


def test_gt_c1_10_reads_need_only_the_session_changes_need_csrf():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        client = harness_client(env, auth)
        session = login(env, auth, telegram)
        other = login(env, auth, telegram, code="code-2")

        assert op(client, "employee.list", session_token=session["session_token"])[0] == 200
        assert op(client, "company.invite", session_token=session["session_token"])[0] == 403
        assert op(client, "company.invite", session_token=session["session_token"], csrf_token=other["csrf_token"])[0] == 403
        assert op(client, "employee.draft", session_token=session["session_token"], kind="twin", role_text="x")[0] == 403
        status, body = op(client, "employee.draft", session_token=session["session_token"],
                          csrf_token=session["csrf_token"], kind="twin", role_text="Я менеджер")
        assert status == 201 and body["proposal_id"]
        assert op(client, "journal.list", session_token="forged")[0] == 401


def test_session_gives_access_only_to_its_own_harness():
    with harness_env() as env:
        env.register("a")
        other = env.register("b")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        client = harness_client(env, auth)
        session = login(env, auth, telegram)
        status, body = op(client, "journal.list", session_token=session["session_token"], harness_id=str(other.harness_id))
        assert status == 403


def test_logout_needs_csrf_and_revokes_the_session():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        client = harness_client(env, auth)
        session = login(env, auth, telegram)
        assert op(client, "auth.web.logout", session_token=session["session_token"])[0] == 403
        assert op(client, "auth.web.logout", session_token=session["session_token"], csrf_token=session["csrf_token"])[0] == 200
        assert op(client, "employee.list", session_token=session["session_token"])[0] == 401


def test_login_without_oidc_configuration_is_503():
    with harness_env() as env:
        auth = WebAuth(env.connection, oidc=None, session_ttl=timedelta(hours=1))
        assert op(harness_client(env, auth), "auth.web.start")[0] == 503


# --- сервис web: cookie, редиректы, промежуточная страница -------------------------------------

def web_with(env, auth, base="https://cabinet.example.test") -> TestClient:
    harness = http_harness("http://harness", API_TOKEN, client=harness_client(env, auth))
    return TestClient(create_web(harness, web_base_url=base, bot_username="metabrain_bot"), base_url=base)


def test_cabinet_login_flow_end_to_end_with_cookies():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        web = web_with(env, auth)

        assert web.get("/", follow_redirects=False).headers["location"] == "/login"
        assert "Войти через Telegram" in web.get("/login").text

        start = web.get("/login/start", follow_redirects=False)
        assert start.status_code == 303 and start.headers["location"].startswith("https://oauth.telegram.org/auth?")
        login_cookie = start.headers["set-cookie"]
        assert "HttpOnly" in login_cookie and "SameSite=lax" in login_cookie and "Secure" in login_cookie
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        telegram.issue("web-code", query["code_challenge"], claims(telegram_id(), query["nonce"]))

        callback = web.get(f"/auth/callback?code=web-code&state={query['state']}", follow_redirects=False)
        assert callback.status_code == 200 and 'http-equiv="refresh"' in callback.text
        session_cookie = next(c for c in callback.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE))
        assert "HttpOnly" in session_cookie and "SameSite=strict" in session_cookie and "Secure" in session_cookie

        home = web.get("/")
        assert home.status_code == 200 and "Исследователь" in home.text
        assert home.headers["content-security-policy"].startswith("default-src 'self'")
        assert home.headers["cache-control"] == "no-store"

        assert web.post("/logout", content="csrf_token=wrong", headers={"content-type": "application/x-www-form-urlencoded"}).status_code == 403
        csrf = web.cookies.get(SESSION_COOKIE).split(".", 1)[1]
        out = web.post("/logout", content=f"csrf_token={csrf}", headers={"content-type": "application/x-www-form-urlencoded"},
                       follow_redirects=False)
        assert out.status_code == 303 and out.headers["location"] == "/login"
        assert web.get("/", follow_redirects=False).headers["location"] == "/login"


def test_callback_with_a_state_from_another_browser_never_reaches_telegram():
    with harness_env() as env:
        env.register("a")
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        web = web_with(env, auth)
        query = begin(auth)                                   # вход начат «в другом браузере»: cookie нет
        response = web.get(f"/auth/callback?code=x&state={query['state']}")
        assert response.status_code == 400 and telegram.token_requests == 0


def test_unregistered_telegram_account_goes_to_self_registration():
    with harness_env() as env:
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        web = web_with(env, auth)
        start = web.get("/login/start", follow_redirects=False)
        query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
        telegram.issue("c", query["code_challenge"], claims(999, query["nonce"]))
        response = web.get(f"/auth/callback?code=c&state={query['state']}", follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] == "/bootstrap"   # ADR-030
        assert SESSION_COOKIE not in web.cookies


def test_cookies_are_not_secure_only_on_plain_http_dev_base():
    with harness_env() as env:
        telegram = FakeTelegram()
        web = web_with(env, make_auth(env, telegram), base="http://localhost:8081")
        start = web.get("/login/start", follow_redirects=False)
        assert "Secure" not in start.headers["set-cookie"]


def test_unregistered_user_gets_bootstrap_ticket_and_can_create_org():
    """ADR-030: Telegram подтверждён, организации нет → тикет → саморегистрация → вход."""
    with harness_env() as env:
        telegram = FakeTelegram()
        auth = make_auth(env, telegram)
        result = login(env, auth, telegram)                       # telegram_id() не зарегистрирован
        assert result.get("bootstrap") is True and result.get("ticket")
        assert env.connection.execute("SELECT count(*) FROM web_sessions").fetchone()[0] == 0

        session = auth.bootstrap(ticket=result["ticket"], company_name="  ООО Бутстрап  ")
        assert session["session_token"] and session["csrf_token"]
        identity, _ = auth.resolve(session["session_token"])
        name = env.connection.execute("SELECT name FROM companies WHERE id = %s", (identity.company_id,)).fetchone()[0]
        assert name == "ООО Бутстрап"

        with pytest.raises(WebAuthError) as again:                # тикет одноразовый
            auth.bootstrap(ticket=result["ticket"], company_name="Другая")
        assert again.value.code == "bootstrap_ticket_invalid"
        with pytest.raises(WebAuthError) as empty:
            auth.bootstrap(ticket="x", company_name="")
        assert empty.value.code in ("bootstrap_ticket_invalid", "company_name_required")
