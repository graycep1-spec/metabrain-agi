"""Веб-кабинет — тонкий клиент API харнесса (C-1; ADR-012, ADR-025).

Вход по Telegram OIDC, сессии и CSRF проверяет API харнесса; здесь — страницы, формы, редиректы и cookie.
CSRF-токен в изменения берётся из поля формы, а не из cookie: иначе проверка теряет смысл.
"""

from collections.abc import Callable, Mapping
import hmac
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.concurrency import run_in_threadpool


SESSION_COOKIE = "mb_session"
LOGIN_COOKIE = "mb_login"
LOGIN_COOKIE_TTL = 600
BOOTSTRAP_COOKIE = "mb_bootstrap"
BOOTSTRAP_COOKIE_TTL = 900

KINDS = {"twin": "Двойник", "new": "Новый сотрудник", "researcher": "Исследователь"}
STATUSES = {"draft": "Ждёт согласия", "active": "Работает", "paused": "На паузе", "retired": "Выведен"}
VERSION_STATUSES = {"proposed": "ждёт решения", "active": "действует", "superseded": "заменена", "returned": "возвращена"}
PROPOSAL_KINDS = {
    "passport": "Паспорт", "connect": "Подключение", "adapter": "Адаптер", "recommendation": "Рекомендация",
    "action": "Действие", "skill_publish": "Навык в компанию", "autonomy_change": "Автономия",
}
DECISIONS = {"approved": "подтверждено", "returned": "возвращено", "incident": "инцидент", "pending": "ждёт"}
FIELDS = {
    "role.title": "Роль", "role.mission": "Миссия", "duties": "Обязанности", "goals": "Цели",
    "decision_bounds.may": "Может", "decision_bounds.must_not": "Нельзя", "escalation": "Спрашивает куратора",
    "schedule": "Расписание", "tools.connectors": "Системы", "tools.read_scopes": "Чтение", "autonomy": "Автономия",
}
JOURNAL_KINDS = {
    "report": "Отчёт сотрудника", "proposal.decision": "Решение куратора",
    "employee.passport.proposed": "Паспорт предложен", "employee.passport.activated": "Паспорт действует",
    "employee.passport.rejected": "Паспорт отклонён валидатором", "landscape.connected": "Система подключена",
    "adapter.proposed": "Адаптер предложен", "adapter.rejected": "Адаптер отклонён", "adapter.activated": "Адаптер включён",
    "adapter.call": "Вызов адаптера", "landscape.discovered": "Карта ландшафта", "auth.web.login": "Вход в кабинет",
    "auth.web.logout": "Выход из кабинета", "consent.external_llm": "Согласие на внешний LLM",
    "secret.stored": "Доступ сохранён", "landscape.discovery_failed": "Исследование не удалось",
    "answer.blocked": "Ответ не выдан", "skill.published": "Навык опубликован", "skill.rejected": "Навык отклонён",
    "skill.failed": "Навык не создан", "employee.paused": "Сотрудник на паузе", "employee.resumed": "Сотрудник возобновлён",
    "proposal.withdrawn": "Предложение заменено", "proposal.applied": "Согласие применено",
    "proposal.decision_denied": "Решение отклонено: не куратор",
}
METRIC_NAMES = {"min_decided": "решений куратора", "accepted_ratio": "доля принятых", "eval_score": "оценка на eval",
                "incident_free_days": "дней без инцидентов"}
SYSTEM_STATUSES = {"available": "не подключена", "pending": "ждёт вашего решения", "connected": "подключена, адаптер готовится",
                   "active": "работает"}
NOTICES = {
    "approved": "Подтверждено.", "returned": "Возвращено.", "incident": "Отмечено как инцидент.",
    "proposed": "Новая версия предложена — подтвердите её ниже.",
    "conflict": "Это предложение уже решено — возможно, в Telegram.",
    "connect": "Доступ сохранён, подключение предложено — подтвердите его ниже.",
    "shared": "Навык прошёл проверку — подтвердите публикацию в компанию ниже.",
    "paused": "Сотрудник на паузе: новые работы не стартуют, начатая доделывается.",
    "process_created": "Процесс создан.",
    "project_created": "Проект создан.",
    "assigned": "Сотрудник прикреплён.",
    "task_created": "Задача добавлена.",
    "task_updated": "Задача обновлена.",
    "examples": "Примеры процессов и проектов добавлены.",
    "resumed": "Сотрудник снова работает по расписанию.",
    "llm_saved": "Настройки LLM сохранены. Применятся после перезапуска харнесса.",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}

# Вызов API харнесса: (операция, тело) -> (HTTP-статус, JSON).
HarnessCall = Callable[[str, Mapping[str, Any]], tuple[int, dict[str, Any]]]


class Unauthorized(Exception):
    """Нет сессии или харнесс её не принял — на страницу входа."""


class FormError(ValueError):
    pass


def http_harness(base_url: str, api_token: str, client: httpx.Client | None = None) -> HarnessCall:
    http = client or httpx.Client(base_url=base_url.rstrip("/"), timeout=60)

    def call(operation: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        try:
            response = http.post(f"/v1/operations/{operation}", json=dict(payload),
                                 headers={"Authorization": f"Bearer {api_token}"})
        except httpx.HTTPError:
            return 503, {"error": "harness_unavailable"}
        try:
            body = response.json()
        except ValueError:
            body = {}
        return response.status_code, body if isinstance(body, dict) else {}

    return call


# --- форма паспорта <-> правка C-1 ----------------------------------------------------------

def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def passport_form(passport: Mapping[str, Any]) -> dict[str, str]:
    def goal(g):
        target = "" if g.get("target") is None else g["target"]
        return f"{g['text']} | {g['metric']} | {target} | {g['period']}"

    return {
        "title": passport["role"]["title"],
        "mission": passport["role"]["mission"],
        "duties": "\n".join(d["text"] for d in passport["duties"]),
        "goals": "\n".join(goal(g) for g in passport["goals"]),
        "may": "\n".join(passport["decision_bounds"]["may"]),
        "must_not": "\n".join(passport["decision_bounds"]["must_not"]),
        "escalation": "\n".join(e["condition"] for e in passport["escalation"]),
        "schedule": "\n".join(f"{s['cron']} | {s['duty_id'][1:]}" for s in passport["triggers"]["schedule"]),
    }


def passport_patch(form: Mapping[str, str]) -> dict[str, Any]:
    goals = []
    for line in _lines(form.get("goals", "")):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 4:
            raise FormError(f"Цель «{line}»: нужно четыре части через «|» — что, метрика, цель, период.")
        goals.append({"text": parts[0], "metric": parts[1], "target": parts[2] or None, "period": parts[3]})
    schedule = []
    for line in _lines(form.get("schedule", "")):
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 2 or not parts[1].isdigit():
            raise FormError(f"Расписание «{line}»: нужно «cron | номер обязанности».")
        schedule.append({"cron": parts[0], "duty": int(parts[1])})
    return {
        "role": {"title": form.get("title", "").strip(), "mission": form.get("mission", "").strip()},
        "duties": [{"text": line} for line in _lines(form.get("duties", ""))],
        "goals": goals,
        "decision_bounds": {"may": _lines(form.get("may", "")), "must_not": _lines(form.get("must_not", ""))},
        "escalation": [{"condition": line} for line in _lines(form.get("escalation", ""))],
        "schedule": schedule,
    }


def create_app(harness: HarnessCall, *, web_base_url: str, bot_username: str = "") -> FastAPI:
    secure = web_base_url.startswith("https://")
    templates = Environment(loader=FileSystemLoader(Path(__file__).with_name("templates")),
                            autoescape=select_autoescape(["html"]))
    app = FastAPI(title="metabrain-web", docs_url=None, redoc_url=None, openapi_url=None)

    def page(name: str, status: int = 200, **context: Any) -> HTMLResponse:
        html = templates.get_template(name).render(bot_username=bot_username, **context)
        return HTMLResponse(html, status_code=status)

    def message(title: str, text: str, status: int, link: Mapping[str, str] | None = None,
                csrf_token: str | None = None) -> HTMLResponse:
        return page("message.html", status, title=title, text=text, link=link, csrf_token=csrf_token)

    def session(request: Request) -> tuple[str, str] | None:
        raw = request.cookies.get(SESSION_COOKIE, "")
        token, _, csrf = raw.partition(".")
        return (token, csrf) if token and csrf else None

    def clear_session(response: Response) -> Response:
        response.delete_cookie(SESSION_COOKIE, path="/", secure=secure, httponly=True, samesite="strict")
        return response

    async def raw_api(operation: str, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        return await run_in_threadpool(harness, operation, payload)

    async def api(request: Request, operation: str, payload: Mapping[str, Any] | None = None,
                  *, csrf: str | None = None) -> tuple[int, dict[str, Any]]:
        current = session(request)
        if current is None:
            raise Unauthorized()
        data = {**(payload or {}), "session_token": current[0]}
        if csrf is not None:
            data["csrf_token"] = csrf
        status, body = await raw_api(operation, data)
        if status == 401:
            raise Unauthorized()
        return status, body

    async def form(request: Request) -> dict[str, str]:
        parsed = parse_qs((await request.body()).decode("utf-8", "replace"), keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def csrf_of(request: Request) -> str:
        current = session(request)
        return current[1] if current else ""

    @app.exception_handler(Unauthorized)
    async def to_login(request: Request, exc: Unauthorized):
        return clear_session(RedirectResponse("/login", status_code=303))

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "web"}

    # --- вход и выход (T-31) ---------------------------------------------------------------

    @app.get("/login")
    def login() -> HTMLResponse:
        return page("login.html")

    @app.get("/login/start")
    async def login_start():
        status, body = await raw_api("auth.web.start", {})
        if status != 200 or "authorization_url" not in body:
            return message("Вход недоступен", "Вход через Telegram пока не настроен. Попробуйте позже.", 503)
        response = RedirectResponse(body["authorization_url"], status_code=303)
        # SameSite=Lax: cookie должна дойти на колбэк — это переход с oauth.telegram.org.
        response.set_cookie(LOGIN_COOKIE, body["state"], max_age=LOGIN_COOKIE_TTL, path="/auth/callback",
                            secure=secure, httponly=True, samesite="lax")
        return response

    @app.get("/auth/callback")
    async def callback(request: Request, code: str = "", state: str = ""):
        expected = request.cookies.get(LOGIN_COOKIE, "")
        if not code or not state or not expected or not hmac.compare_digest(expected, state):
            return message("Вход не удался", "Ссылка входа устарела или открыта в другом браузере. Начните вход заново.",
                           400, {"href": "/login", "label": "Войти снова"})
        status, body = await raw_api("auth.web.callback", {"code": code, "state": state})
        if status == 200 and body.get("bootstrap"):
            # Новый человек без организации — саморегистрация в кабинете (ADR-030).
            result = RedirectResponse("/bootstrap", status_code=303)
            result.set_cookie(BOOTSTRAP_COOKIE, body["ticket"], max_age=BOOTSTRAP_COOKIE_TTL, path="/",
                              secure=secure, httponly=True, samesite="lax")
            result.delete_cookie(LOGIN_COOKIE, path="/auth/callback", secure=secure, httponly=True, samesite="lax")
            return result
        if status != 200:
            if body.get("error") == "registration_required":
                link = {"href": f"https://t.me/{bot_username}", "label": "Открыть бота"} if bot_username else None
                result = message("Сначала зарегистрируйтесь",
                                 "Этот Telegram-аккаунт ещё не зарегистрирован. Напишите боту /start и вернитесь.", 401, link)
            else:
                result = message("Вход не удался", "Telegram не подтвердил вход. Попробуйте ещё раз.", 401,
                                 {"href": "/login", "label": "Войти снова"})
            result.delete_cookie(LOGIN_COOKIE, path="/auth/callback", secure=secure, httponly=True, samesite="lax")
            return result
        max_age = max(0, int(body["expires_in"]))
        response = page("continue.html")
        response.set_cookie(SESSION_COOKIE, f"{body['session_token']}.{body['csrf_token']}", max_age=max_age,
                            path="/", secure=secure, httponly=True, samesite="strict")
        response.delete_cookie(LOGIN_COOKIE, path="/auth/callback", secure=secure, httponly=True, samesite="lax")
        return response

    @app.post("/logout")
    async def logout(request: Request):
        current = session(request)
        if current is None:
            return RedirectResponse("/login", status_code=303)
        status, _ = await raw_api("auth.web.logout", {"session_token": current[0],
                                                      "csrf_token": (await form(request)).get("csrf_token", "")})
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return clear_session(RedirectResponse("/login", status_code=303))

    # --- саморегистрация: создание организации в кабинете (ADR-030) ---------------------------

    @app.get("/bootstrap")
    async def bootstrap_form(request: Request):
        if not request.cookies.get(BOOTSTRAP_COOKIE):
            return RedirectResponse("/login", status_code=303)
        return page("bootstrap.html")

    @app.post("/bootstrap")
    async def bootstrap_submit(request: Request):
        ticket = request.cookies.get(BOOTSTRAP_COOKIE, "")
        if not ticket:
            return RedirectResponse("/login", status_code=303)
        fields = await form(request)
        invite = fields.get("invite", "").strip()
        company = fields.get("company_name", "").strip()
        if invite:
            status, body = await raw_api("auth.web.bootstrap", {"ticket": ticket, "invite": invite})
        elif company:
            status, body = await raw_api("auth.web.bootstrap", {"ticket": ticket, "company_name": company})
        else:
            return page("bootstrap.html", 400, error="Введите название организации или код приглашения.")
        if status != 200:
            if body.get("error") == "invite_invalid":
                # Код не найден/истёк — тикет цел, даём повторить, не стирая cookie.
                return page("bootstrap.html", 400, invite=invite,
                            error="Код приглашения не найден или истёк. Проверьте его у куратора.")
            resp = message("Не удалось войти", "Ссылка устарела — войдите заново.", 400,
                           {"href": "/login", "label": "Войти"})
            resp.delete_cookie(BOOTSTRAP_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
            return resp
        response = page("continue.html")
        response.set_cookie(SESSION_COOKIE, f"{body['session_token']}.{body['csrf_token']}",
                            max_age=max(0, int(body.get("expires_in", 0))),
                            path="/", secure=secure, httponly=True, samesite="strict")
        response.delete_cookie(BOOTSTRAP_COOKIE, path="/", secure=secure, httponly=True, samesite="lax")
        return response

    # --- сотрудники и паспорт (T-32) ----------------------------------------------------------

    @app.get("/")
    async def employees(request: Request, role: str = "", status: str = ""):
        code, body = await api(request, "employee.list")
        if code != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        items = body.get("employees", [])
        roles = sorted({e.get("title", "") for e in items if e.get("kind") != "researcher"})
        shown = [e for e in items
                 if (not role or e.get("title") == role) and (not status or e.get("status") == status)]
        groups: dict[str, dict[str, Any]] = {}
        for e in shown:
            group = groups.setdefault(e.get("title", ""),
                                      {"role": e.get("title", ""), "kind": e.get("kind"), "employees": []})
            group["employees"].append(e)
        # роли по алфавиту, служебный Исследователь — в конце
        ordered = sorted(groups.values(), key=lambda g: (g["kind"] == "researcher", (g["role"] or "").lower()))
        summary_status, summary = await api(request, "planning.summary")
        return page("employees.html", groups=ordered, roles=roles, role=role, sel_status=status, total=len(shown),
                    csrf_token=csrf_of(request), kinds=KINDS, statuses=STATUSES,
                    planning=summary if summary_status == 200 else None, notice=NOTICES.get(request.query_params.get("notice", "")))

    @app.post("/roles")
    async def create_role(request: Request):
        fields = await form(request)
        role_text = fields.get("role_text", "").strip()
        if not role_text:
            return message("Опишите роль", "Напишите, что человек делает и за какой результат отвечает.", 400,
                           {"href": "/", "label": "Назад"}, csrf_token=csrf_of(request))
        status, body = await api(request, "employee.draft",
                                {"kind": "twin", "role_text": role_text}, csrf=fields.get("csrf_token", ""))
        if status == 201:
            return RedirectResponse("/proposals?notice=proposed", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 422:
            reasons = "; ".join(body.get("reasons", [])) or body.get("detail", "паспорт отклонён")
            return message("Роль не создана", f"Валидатор не пропустил паспорт: {reasons}", 422,
                           {"href": "/", "label": "Назад"}, csrf_token=csrf_of(request))
        return message("Роль не создана", "Нужен доступ к модели и валидатору. Попробуйте позже.", 503,
                       {"href": "/", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.post("/invite")
    async def make_invite(request: Request):
        fields = await form(request)
        status, body = await api(request, "company.invite", csrf=fields.get("csrf_token", ""))
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status != 200:
            return message("Не удалось создать приглашение",
                           "Для ссылок-приглашений нужен настроенный бот. Попробуйте позже.", 503,
                           {"href": "/", "label": "Назад"}, csrf_token=csrf_of(request))
        link = body.get("invite_link", "")
        start = parse_qs(urlparse(link).query).get("start") if link else None
        code = start[0] if start else link
        return page("invite.html", link=link, code=code, csrf_token=csrf_of(request))

    @app.get("/employees/{employee_id}")
    async def employee(request: Request, employee_id: str, notice: str = ""):
        status, body = await api(request, "employee.get", {"employee_id": employee_id})
        if status != 200:
            return message("Не найдено", "Такого сотрудника в вашем кабинете нет.", 404, {"href": "/", "label": "К сотрудникам"},
                           csrf_token=csrf_of(request))
        autonomy_status, autonomy = await api(request, "employee.autonomy", {"employee_id": employee_id})
        return page("employee.html", e=body, p=body.get("passport"), csrf_token=csrf_of(request), notice=NOTICES.get(notice),
                    autonomy=autonomy if autonomy_status == 200 else None, metric_names=METRIC_NAMES,
                    kinds=KINDS, statuses=STATUSES, version_statuses=VERSION_STATUSES)

    @app.post("/employees/{employee_id}/pause")
    async def pause(request: Request, employee_id: str):
        return await pause_or_resume(request, employee_id, "pause")

    @app.post("/employees/{employee_id}/resume")
    async def resume(request: Request, employee_id: str):
        return await pause_or_resume(request, employee_id, "resume")

    async def pause_or_resume(request: Request, employee_id: str, action: str) -> Response:
        fields = await form(request)
        status, _ = await api(request, f"employee.{action}", {"employee_id": employee_id}, csrf=fields.get("csrf_token", ""))
        if status == 200:
            notice = "paused" if action == "pause" else "resumed"
            return RedirectResponse(f"/employees/{employee_id}?notice={notice}", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 409:
            return message("Нельзя", "У сотрудника ещё нет действующего паспорта.", 409,
                           {"href": f"/employees/{employee_id}", "label": "Назад"}, csrf_token=csrf_of(request))
        return message("Не получилось", "Сервис временно недоступен. Попробуйте позже.", 503 if status >= 500 else status,
                       {"href": f"/employees/{employee_id}", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.get("/employees/{employee_id}/edit")
    async def edit_form(request: Request, employee_id: str):
        status, body = await api(request, "employee.get", {"employee_id": employee_id})
        if status != 200 or not body.get("passport"):
            return message("Править нечего", "У сотрудника ещё нет паспорта.", 409,
                           {"href": f"/employees/{employee_id}", "label": "Назад"}, csrf_token=csrf_of(request))
        return page("edit.html", employee_id=employee_id, title=body["title"], form=passport_form(body["passport"]),
                    csrf_token=csrf_of(request))

    @app.post("/employees/{employee_id}/edit")
    async def edit_submit(request: Request, employee_id: str):
        fields = await form(request)

        def again(error: str, status: int) -> HTMLResponse:
            return page("edit.html", status, employee_id=employee_id, title=fields.get("title", ""), form=fields,
                        error=error, csrf_token=csrf_of(request))

        try:
            patch = passport_patch(fields)
        except FormError as error:
            return again(str(error), 400)
        status, body = await api(request, "employee.passport.propose",
                                 {"employee_id": employee_id, "passport_patch": patch}, csrf=fields.get("csrf_token", ""))
        if status == 201:
            return RedirectResponse("/proposals?notice=proposed", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 422:
            return again("Валидатор не пропустил паспорт: " + "; ".join(body.get("reasons", [])), 422)
        if status == 400:
            return again(f"Проверьте форму: {body.get('detail', 'ошибка')}", 400)
        return again("Сервис временно недоступен. Попробуйте позже.", 503 if status >= 500 else status)

    # --- предложения (T-33) ------------------------------------------------------------------------

    @app.get("/proposals")
    async def proposals(request: Request, notice: str = ""):
        status, body = await api(request, "proposal.list")
        if status != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        items = body.get("proposals", [])
        pending = [p for p in items if p.get("status") == "pending"]
        decided = sorted((p for p in items if p.get("status") != "pending"), key=lambda p: p["created_at"], reverse=True)[:10]
        return page("proposals.html", pending=pending, decided=decided, csrf_token=csrf_of(request),
                    notice=NOTICES.get(notice), kinds=PROPOSAL_KINDS, fields=FIELDS, decisions=DECISIONS)

    @app.post("/proposals/{proposal_id}/decide")
    async def decide(request: Request, proposal_id: str):
        fields = await form(request)
        decision = fields.get("decision", "")
        if decision not in ("approve", "return", "incident"):
            return message("Неизвестное решение", "Обновите страницу и попробуйте снова.", 400, csrf_token=csrf_of(request))
        status, body = await api(request, "proposal.decide", {"proposal_id": proposal_id, "decision": decision},
                                 csrf=fields.get("csrf_token", ""))
        if status == 200:
            return RedirectResponse(f"/proposals?notice={body.get('status', '')}", status_code=303)
        if status == 409:
            return RedirectResponse("/proposals?notice=conflict", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Решение не применено", "Сервис временно недоступен — решение не сохранено. Попробуйте позже.",
                       503 if status >= 500 else status, {"href": "/proposals", "label": "К предложениям"},
                       csrf_token=csrf_of(request))

    # --- журнал (T-33) ------------------------------------------------------------------------------

    @app.get("/journal")
    async def journal(request: Request, kind: str = "", employee_id: str = "", cursor: str = ""):
        query = {k: v for k, v in {"kind": kind, "employee_id": employee_id, "cursor": cursor}.items() if v}
        status, body = await api(request, "journal.list", query)
        if status != 200:
            return message("Журнал недоступен", "Проверьте фильтр или попробуйте позже.", status if status < 500 else 503,
                           {"href": "/journal", "label": "Сбросить фильтр"}, csrf_token=csrf_of(request))
        _, team = await api(request, "employee.list")
        people = team.get("employees", [])
        next_cursor = body.get("next_cursor")
        next_query = urlencode({k: v for k, v in {"kind": kind, "employee_id": employee_id, "cursor": next_cursor}.items() if v})
        return page("journal.html", entries=body.get("entries", []), next_cursor=next_cursor, next_query=next_query,
                    kind=kind, employee_id=employee_id, employees=people,
                    names={e["employee_id"]: e["title"] for e in people}, journal_kinds=JOURNAL_KINDS,
                    csrf_token=csrf_of(request))

    # --- навыки (T-34) ------------------------------------------------------------------------------

    @app.get("/skills")
    async def skills(request: Request):
        status, body = await api(request, "skills.list")
        if status == 501:
            return message("Навыки недоступны", "Мета-мозг не подключён к харнессу (METABRAIN_URL).", 501,
                           csrf_token=csrf_of(request))
        if status != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        items = body.get("skills", [])
        return page("skills.html", company=[s for s in items if s["scope"] == "company"],
                    personal=[s for s in items if s["scope"] == "personal"], csrf_token=csrf_of(request))

    @app.post("/skills/{skill_id}/share")
    async def share(request: Request, skill_id: str):
        fields = await form(request)
        status, body = await api(request, "skill.share", {"skill_id": skill_id}, csrf=fields.get("csrf_token", ""))
        if status == 201:
            return RedirectResponse("/proposals?notice=shared", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 422:
            return message("Навык не прошёл проверку",
                           "Валидатор не пропустил навык для компании: " + ", ".join(body.get("reasons", [])) +
                           ". Обычно это имена клиентов или номера заявок из исходной работы — такой навык остаётся личным.",
                           422, {"href": "/skills", "label": "К навыкам"}, csrf_token=csrf_of(request))
        if status == 404:
            return message("Не найдено", "Такого личного навыка у вас нет.", 404, {"href": "/skills", "label": "К навыкам"},
                           csrf_token=csrf_of(request))
        return message("Не получилось", "Сервис временно недоступен. Попробуйте позже.", 503,
                       {"href": "/skills", "label": "К навыкам"}, csrf_token=csrf_of(request))

    # --- подключения и карта (T-34) ----------------------------------------------------------------

    async def connections_page(request: Request, *, selected: str = "", connect_request_id: str = "",
                               error: str | None = None, status: int = 200) -> HTMLResponse:
        code, body = await api(request, "landscape.map")
        if code != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        systems = body.get("systems", [])
        # Секрет из формы в страницу не возвращается — поле доступа всегда пустое.
        return page("connections.html", status, systems=systems, landscape=body.get("map", {}),
                    connectable=[s["system"] for s in systems if s["status"] != "active"], selected=selected,
                    connect_request_id=connect_request_id, error=error, system_statuses=SYSTEM_STATUSES,
                    csrf_token=csrf_of(request))

    @app.get("/connections")
    async def connections(request: Request, system: str = "", connect_request_id: str = ""):
        return await connections_page(request, selected=system, connect_request_id=connect_request_id)

    @app.post("/connections")
    async def connect(request: Request):
        fields = await form(request)
        system, request_id = fields.get("system", ""), fields.get("connect_request_id", "")
        csrf = fields.get("csrf_token", "")

        async def again(error: str, status: int, keep_request: bool = True) -> HTMLResponse:
            return await connections_page(request, selected=system, connect_request_id=request_id if keep_request else "",
                                          error=error, status=status)

        if not fields.get("secret", "").strip():
            return await again("Введите доступ к системе.", 400)
        status, body = await api(request, "landscape.secret.put", {"system": system, "secret": fields["secret"]}, csrf=csrf)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 503 and body.get("error") == "secret_store_unavailable":
            return await again("Хранилище доступов не настроено (SECRET_STORE_KEY) — обратитесь к администратору.", 503)
        if status == 400:
            return await again(f"Проверьте систему и доступ: {body.get('detail', 'ошибка')}", 400)
        if status != 201:
            return await again("Сервис временно недоступен. Попробуйте позже.", 503)
        payload = {"system": system, "secret_ref": body["secret_ref"]}
        if request_id:
            payload["connect_request_id"] = request_id
        status, body = await api(request, "landscape.connect", payload, csrf=csrf)
        if status == 201:
            return RedirectResponse("/proposals?notice=connect", status_code=303)
        if status == 400 and body.get("error") == "invalid_connect_request":
            return await again("Ссылка из Telegram устарела или уже использована — введите доступ ещё раз, "
                               "форма отправится без неё.", 400, keep_request=False)
        if status == 400:
            return await again(f"Проверьте форму: {body.get('detail', 'ошибка')}", 400)
        return await again("Сервис временно недоступен. Попробуйте позже.", 503)

    # --- настройки LLM (ADR-033) ---------------------------------------------------------

    @app.get("/settings")
    async def settings_page(request: Request, notice: str = ""):
        status, body = await api(request, "settings.llm.get")
        if status != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        return page("settings.html", llm=body, csrf_token=csrf_of(request),
                    notice=NOTICES.get(notice), test=None, error=None)

    @app.post("/settings")
    async def settings_save(request: Request):
        fields = await form(request)
        csrf = fields.get("csrf_token", "")
        payload = {
            "provider": fields.get("provider", ""), "api_base": fields.get("api_base", "").strip(),
            "model_main": fields.get("model_main", "").strip(), "model_judge": fields.get("model_judge", "").strip(),
            "dlp_enabled": fields.get("dlp_enabled", ""),
        }
        if fields.get("api_key", "").strip():
            payload["api_key"] = fields["api_key"]          # новый ключ — в vault, в журнал не попадёт
        status, body = await api(request, "settings.llm.put", payload, csrf=csrf)
        if status == 403:
            if body.get("error") == "settings_locked":
                return message("Редактирование выключено",
                               "Настройка LLM из кабинета отключена на этой установке (LLM_SETTINGS_EDITABLE).", 403,
                               {"href": "/settings", "label": "Назад"}, csrf_token=csrf_of(request))
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        if status == 400:
            _, cfg = await api(request, "settings.llm.get")
            cfg = cfg if isinstance(cfg, dict) else {}
            cfg.update({"provider": payload["provider"], "api_base": payload["api_base"],
                        "model_main": payload["model_main"], "model_judge": payload["model_judge"],
                        "dlp_enabled": fields.get("dlp_enabled", "") != ""})
            return page("settings.html", 400, llm=cfg, csrf_token=csrf_of(request),
                        error=f"Проверьте поля: {body.get('detail', 'ошибка')}", test=None, notice=None)
        if status != 200:
            return message("Не сохранено", "Сервис временно недоступен. Попробуйте позже.", 503)
        return RedirectResponse("/settings?notice=llm_saved", status_code=303)

    @app.post("/settings/test")
    async def settings_test(request: Request):
        fields = await form(request)
        status, body = await api(request, "settings.llm.test", {}, csrf=fields.get("csrf_token", ""))
        if status == 403:
            if body.get("error") == "settings_locked":
                return message("Проверка выключена", "Настройка LLM из кабинета отключена на этой установке.", 403,
                               {"href": "/settings", "label": "Назад"}, csrf_token=csrf_of(request))
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        cfg_status, cfg = await api(request, "settings.llm.get")
        return page("settings.html", llm=cfg if cfg_status == 200 else {}, csrf_token=csrf_of(request),
                    test=body if status == 200 else {"ok": False, "detail": "Проверка недоступна"},
                    error=None, notice=None)

    # --- процессы и проекты (C-6, планирование) ------------------------------------------

    @app.get("/processes")
    async def processes(request: Request, notice: str = ""):
        status, body = await api(request, "process.list")
        if status != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        return page("processes.html", processes=body.get("processes", []), csrf_token=csrf_of(request),
                    notice=NOTICES.get(notice))

    @app.post("/processes")
    async def create_process(request: Request):
        fields = await form(request)
        status, body = await api(
            request, "process.create",
            {"name": fields.get("name", ""), "purpose": fields.get("purpose", ""),
             "inputs": fields.get("inputs", ""), "outputs": fields.get("outputs", ""),
             "status": fields.get("status", "active")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse("/processes?notice=process_created", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Процесс не создан", body.get("detail", "Проверьте форму."), 400 if status < 500 else 503,
                       {"href": "/processes", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.get("/processes/{process_id}")
    async def process_detail(request: Request, process_id: str, notice: str = ""):
        status, body = await api(request, "process.get", {"process_id": process_id})
        if status != 200:
            return message("Процесс не найден", "В вашем кабинете такого процесса нет.", 404,
                           {"href": "/processes", "label": "К процессам"}, csrf_token=csrf_of(request))
        employees_status, employees_body = await api(request, "employee.list")
        return page("process.html", p=body,
                    employees=employees_body.get("employees", []) if employees_status == 200 else [],
                    csrf_token=csrf_of(request), notice=NOTICES.get(notice))

    @app.post("/processes/{process_id}/assign")
    async def assign_process(request: Request, process_id: str):
        fields = await form(request)
        status, body = await api(
            request, "assignment.create",
            {"employee_id": fields.get("employee_id", ""), "target_type": "process",
             "target_id": process_id, "role": fields.get("role", "")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse(f"/processes/{process_id}?notice=assigned", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        reason = "Этот сотрудник уже прикреплён." if status == 409 else body.get("detail", "Проверьте форму.")
        return message("Не прикреплено", reason, 409 if status == 409 else 400,
                       {"href": f"/processes/{process_id}", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.post("/processes/{process_id}/tasks")
    async def add_process_task(request: Request, process_id: str):
        fields = await form(request)
        status, body = await api(
            request, "task.create",
            {"assignment_id": fields.get("assignment_id", ""), "title": fields.get("title", ""),
             "description": fields.get("description", "")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse(f"/processes/{process_id}?notice=task_created", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Задача не создана", body.get("detail", "Проверьте форму."), 400,
                       {"href": f"/processes/{process_id}", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.get("/projects")
    async def projects(request: Request, notice: str = ""):
        status, body = await api(request, "project.list")
        if status != 200:
            return message("Кабинет недоступен", "Сервис временно недоступен. Попробуйте позже.", 503)
        return page("projects.html", projects=body.get("projects", []), csrf_token=csrf_of(request),
                    notice=NOTICES.get(notice))

    @app.post("/projects")
    async def create_project(request: Request):
        fields = await form(request)
        status, body = await api(
            request, "project.create",
            {"name": fields.get("name", ""), "goal": fields.get("goal", ""),
             "starts_on": fields.get("starts_on", ""), "ends_on": fields.get("ends_on", ""),
             "budget_usd": fields.get("budget_usd", "0"), "status": fields.get("status", "planned")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse("/projects?notice=project_created", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Проект не создан", body.get("detail", "Проверьте форму."), 400 if status < 500 else 503,
                       {"href": "/projects", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.get("/projects/{project_id}")
    async def project_detail(request: Request, project_id: str, notice: str = ""):
        status, body = await api(request, "project.get", {"project_id": project_id})
        if status != 200:
            return message("Проект не найден", "В вашем кабинете такого проекта нет.", 404,
                           {"href": "/projects", "label": "К проектам"}, csrf_token=csrf_of(request))
        employees_status, employees_body = await api(request, "employee.list")
        return page("project.html", p=body,
                    employees=employees_body.get("employees", []) if employees_status == 200 else [],
                    csrf_token=csrf_of(request), notice=NOTICES.get(notice))

    @app.post("/projects/{project_id}/assign")
    async def assign_project(request: Request, project_id: str):
        fields = await form(request)
        status, body = await api(
            request, "assignment.create",
            {"employee_id": fields.get("employee_id", ""), "target_type": "project",
             "target_id": project_id, "role": fields.get("role", "")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse(f"/projects/{project_id}?notice=assigned", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        reason = "Этот сотрудник уже прикреплён." if status == 409 else body.get("detail", "Проверьте форму.")
        return message("Не прикреплено", reason, 409 if status == 409 else 400,
                       {"href": f"/projects/{project_id}", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.post("/projects/{project_id}/tasks")
    async def add_project_task(request: Request, project_id: str):
        fields = await form(request)
        status, body = await api(
            request, "task.create",
            {"assignment_id": fields.get("assignment_id", ""), "title": fields.get("title", ""),
             "description": fields.get("description", "")},
            csrf=fields.get("csrf_token", ""),
        )
        if status == 201:
            return RedirectResponse(f"/projects/{project_id}?notice=task_created", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Задача не создана", body.get("detail", "Проверьте форму."), 400,
                       {"href": f"/projects/{project_id}", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.post("/tasks/{task_id}/status")
    async def update_task_status(request: Request, task_id: str):
        fields = await form(request)
        status, body = await api(request, "task.update",
                                 {"task_id": task_id, "status": fields.get("status", "")},
                                 csrf=fields.get("csrf_token", ""))
        if status == 200:
            target = fields.get("next", "/")
            return RedirectResponse(target if target.startswith("/") else "/", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Задача не обновлена", body.get("detail", "Проверьте форму."), 400,
                       {"href": "/", "label": "Назад"}, csrf_token=csrf_of(request))

    @app.post("/planning/examples")
    async def planning_examples(request: Request):
        fields = await form(request)
        status, body = await api(request, "planning.seed_examples", {}, csrf=fields.get("csrf_token", ""))
        if status == 200:
            return RedirectResponse("/processes?notice=examples", status_code=303)
        if status == 403:
            return message("Действие отклонено", "Не прошла проверка безопасности формы. Обновите страницу.", 403)
        return message("Примеры не добавлены", body.get("detail", "Сервис временно недоступен."), 503,
                       {"href": "/processes", "label": "Назад"}, csrf_token=csrf_of(request))

    return app


def main() -> None:
    import os

    import uvicorn

    from src.common.config import load_settings
    from src.common.secrets import EnvSecretStore, SecretNotFoundError

    try:
        settings = load_settings()
        api_token = EnvSecretStore().resolve("env:HARNESS_API_TOKEN")
    except (ValueError, SecretNotFoundError) as error:
        raise SystemExit(str(error)) from error
    app = create_app(
        http_harness(os.environ.get("HARNESS_API_URL", "http://harness:8080"), api_token),
        web_base_url=os.environ.get("WEB_BASE_URL", settings.app_base_url),
        bot_username=os.environ.get("TELEGRAM_BOT_USERNAME", "").lstrip("@"),
    )
    uvicorn.run(app, host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower(), access_log=False)


if __name__ == "__main__":
    main()
