# Веб-кабинет (C-1, ADR-012, ADR-025)

Тонкий клиент API харнесса: страницы, редиректы и cookie. Вход, сессии и CSRF проверяет харнесс (`src/harness/web_auth.py`).

- `app.py` — `/login`, `/login/start` → Telegram, `/auth/callback`, `/` (сотрудники), `POST /logout`. Запуск: `python -m src.web.app`.
- Экраны: «Сотрудники» и паспорт (T-32), «Предложения» и «Журнал» (T-33), «Подключения и карта» (T-34): статусы систем, ввод доступа → `landscape.secret.put` (хранилище харнесса, ADR-028) → `landscape.connect`; ссылка из Telegram `/connect` открывает эту форму с выбранной системой. Харнессу для этого нужен `SECRET_STORE_KEY` (`.env.example`), иначе форма отвечает 503.
- `templates/` — Jinja2 с автоэкранированием; `continue.html` — промежуточная страница после входа (cookie сессии `SameSite=Strict` не уходит на редиректе с чужого сайта).

## Как включить вход через Telegram

1. BotFather → бот → **Login Widget**: добавить Allowed URL `<WEB_BASE_URL>/auth/callback` (и origin кабинета), взять **Client ID** и **Client Secret**.
2. Харнессу: `TELEGRAM_OIDC_CLIENT_ID`, `TELEGRAM_OIDC_CLIENT_SECRET`, `WEB_BASE_URL`; кабинету: `WEB_BASE_URL`, `HARNESS_API_TOKEN`.
3. В бою кабинет — только за HTTPS (`WEB_BASE_URL=https://…`): cookie получают флаг `Secure`. На `http://localhost` флаг снимается только для разработки.

Войти может только человек, уже зарегистрированный в боте (`/start`). Срок сессии — `policy.web.session_ttl_hours` (12 ч).
