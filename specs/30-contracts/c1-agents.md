# C-1 · Каналы и API харнесса (наружу один голос)

Две пользовательские поверхности (ADR-012):
- **веб-кабинет** — управление двойником и остальными ЦС: паспорт, автономия, предложения, журнал, подключения, навыки, расписание;
- **Telegram** — вопросы, отчёты, быстрые решения кнопками.

Обе поверхности — тонкие клиенты HTTP API ниже; вся логика и проверки — в API. Решение, принятое в любой поверхности, даёт одинаковый результат и одну запись в журнале. Внутренняя оркестрация (рой) скрыта.

## Операции

```
person.register {channel: "telegram", channel_user_id, company_invite? | company_name?}
    -> {person_id, company_id, harness_id}
    # без invite: создаётся компания, человек — её первый участник и получает инвайт-ссылку (ADR-010)
    # с invite: человек входит в компанию инвайта; в обоих случаях соглашается на внешний LLM (C-004)
company.invite {harness_id} -> {invite_link, exp}           # любой участник компании; ссылка многоразовая до exp

auth.web.start {} -> {authorization_url, state, exp}        # вход в веб-кабинет, см. «Вход в веб»
auth.web.callback {code, state} -> {session_token, csrf_token, exp, expires_in, person_id, harness_id}
    # если Telegram подтверждён, но организации ещё нет: -> {bootstrap: true, ticket, name, expires_in} (ADR-030)
auth.web.bootstrap {ticket, company_name} -> {session_token, csrf_token, exp, expires_in, person_id, harness_id}
    # саморегистрация в кабинете: по тикету из колбэка создаёт организацию и входит; тикет одноразовый
auth.web.logout {session_token, csrf_token} -> {}

process.list {} -> {processes[]}                         # только свой харнесс
process.get {process_id} -> {process, assignments[]}     # назначения с задачами
process.create {name, purpose?, inputs?, outputs?, status?} -> 201 process
project.list {} -> {projects[]}
project.get {project_id} -> {project, assignments[]}
project.create {name, goal?, starts_on?, ends_on?, budget_usd?, status?, result?} -> 201 project
assignment.create {employee_id, target_type: process|project, target_id, role?} -> 201 assignment
task.list {assignment_id?, status?} -> {tasks[]}
task.create {assignment_id, title, description?, due_at?} -> 201 task
task.update {task_id, status?, title?, description?, due_at?} -> 200 task
planning.summary {} -> {processes, projects, assignments, tasks}
planning.seed_examples {} -> {processes, projects, assignments, tasks}  # идемпотентно

settings.llm.usage {} -> {currency, installation: {limit_usd, spent_usd, remaining_usd, prompt_tokens, completion_tokens, total_tokens}, today: {...}}
settings.metabrain.get {} -> {enabled}
settings.metabrain.put {enabled} -> {enabled}
connections.catalog {} -> {groups[]}                    # группы, вход, allowed_roles/denied_roles
connections.custom.list {} -> {connections[]}           # без секрета, secret_masked
connections.custom.create {name, kind, target, auth_type, secret?, allowed_roles?, note?} -> 201 connection

employee.draft {harness_id, kind: "twin" | "new", role_text}
    -> {employee_id, passport_draft, proposal_id}            # паспорт (C-6) — предложение вида passport
employee.list {harness_id}
    -> {employees[]: {employee_id, kind, title, status, autonomy}}
employee.get {harness_id, employee_id}
    -> {passport (активная версия), versions[], status, pending_proposals[]}
employee.passport.propose {harness_id, employee_id, passport_patch}
    -> {proposal_id, version, diff}                           # новая версия паспорта (R-24); расписание и события — тоже здесь
    # passport_patch — секции, которые меняются целиком: role {title, mission}, duties [{text}],
    #   goals [{text, metric, target, period}], decision_bounds {may, must_not}, escalation [{condition}],
    #   schedule [{cron, duty}] (duty — номер обязанности с 1). tools и autonomy так не меняются:
    #   tools — через подключения (C-3), autonomy — через autonomy_change. Ошибка формы (cron, пустые поля) — 400;
    #   паспорт не прошёл валидатор — 422 с причинами. Правится последняя версия: ждущая решения (в т.ч. черновик до
    #   первого согласия) или действующая; ждущее предложение паспорта отзывается («заменено правкой»). Нет версий — 409.
    # Предмет предложения: {employee_id, version, passport, previous_version, diff}; diff — по секциям: было/стало.
employee.pause | employee.resume {harness_id, employee_id}
    -> {status}                                               # решение куратора, сразу в журнал; новые работы не стартуют
    # начатая работа доделывается, работа из очереди снимается (после возобновления — свежий слот);
    # ask к сотруднику на паузе — 404; новая версия паспорта паузу не снимает;
    # Исследователя на паузу не ставят (400), сотрудника без действующего паспорта — 409
employee.autonomy {harness_id, employee_id}
    -> {classes[]: {action_class, level, metrics, thresholds, unmet[], eligible_for}, recommendations, l1_thresholds, l1_unmet[]}
    # metrics: {decided, accepted, incidents, accepted_ratio, incident_free_days, eval_score} по предложениям `action` класса;
    # recommendations — те же метрики по рекомендациям ЦС (в демо классов записи нет, ЦС на L0)
employee.autonomy.request {harness_id, employee_id, action_class, to}
    -> {proposal_id} | {refused, unmet[]}                     # C-6: при невыполненных порогах — отказ со списком
employee.autonomy.lower {harness_id, employee_id, action_class, to}
    -> {level}                                                # понижение без порогов, сразу

ask {harness_id, employee_id?, text}                          # по умолчанию — двойник
    -> {answer, sources[], score_ref, proposal_ids[]}
    # ответ без подписанного Answer не выдаётся: 422 answer_blocked с сообщением человеку (GT-c1-02);
    # нет активного сотрудника — 404; сотрудник ещё отвечает на прошлый вопрос — 409 employee_busy

landscape.connect.start {harness_id, system}
    -> {connect_request_id, cabinet_url, exp}                 # из Telegram: одноразовая ссылка на форму доступа, exp = 30 мин
landscape.secret.put {harness_id, system, secret}
    -> {secret_ref}                                           # секрет — в хранилище секретов харнесса (ADR-028), в ответе только
    # ссылка `vault:<id>`, привязанная к харнессу и системе. Секрет не возвращает ни одна операция, его нет в журнале
    # (там — `secret.stored` со ссылкой). Хранилище не настроено — 503 `secret_store_unavailable`.
landscape.connect {harness_id, system, secret_ref, connect_request_id?}
    -> {proposal_id}                                          # предложение вида connect; без secret_ref не создаётся
    # secret_ref: `vault:<id>` этого харнесса и этой системы или `env:<NAME>` (не секреты самого харнесса: ключи
    # подписи, LLM, БД, Telegram — отказ). Система — из реестра LANDSCAPE_SYSTEMS, иначе 400.
landscape.map {harness_id}
    -> {map, systems[]: {system, status: available | pending | connected | active, connected_at?}}
    # available — в реестре, не подключена; pending — подключение ждёт решения куратора;
    # connected — подключение подтверждено, адаптер ещё не включён; active — адаптер работает
    -> {map, sources[]}

proposal.list {harness_id, status?, kind?, employee_id?}
    -> {proposals[]: {proposal_id, kind, employee_id?, summary, payload, change_plan?, sources[], score_ref, status, created_at}}
proposal.decide {harness_id, proposal_id, decision: "approve" | "return" | "incident", comment?, actor, surface: "web" | "telegram"}
    -> {status, consent_token?, applied?}
    # applied — что харнесс сделал по согласию сразу: паспорт активирован, адаптер включён,
    # recommendation → навык {personal, company} (C-6 «Навыки из работы»), skill_publish → навык опубликован в компании

journal.list {harness_id, employee_id?, kind?, from?, to?, cursor?}
    -> {entries[], next_cursor}
skills.list {harness_id, scope?: "personal" | "company"}
    -> {skills[]}                                             # проекция C-5 skills.resolve для кабинета
    # skills[]: {skill_id, version, scope, title, body, applies_to, examples, created_at, source_jobs, consent: bool, mine: bool}
skill.share {harness_id, skill_id}
    -> {proposal_id} | 422 {reasons}                          # личный навык → кандидат scope=company (C-6 «Навыки из работы», шаг 3):
    # идентификаторы — из фактов его исходных работ; pass → предложение skill_publish, fail — причины, предложения нет.
    # Навык не свой или не личный — 404. Без мета-мозга обе операции — 501.
```

Исходящее событие:

```
report {harness_id, employee_id, job_id, text, sources[], score_ref, proposal_ids[]}
    # в Telegram — сообщением; в кабинете — в журнале и в ленте ЦС
```

`kind` предложения: `passport · recommendation · action · adapter · connect · skill_publish · autonomy_change`.

## Согласие (`consent_token`)

```
ConsentToken {
  token_id, harness_id, proposal_id, subject_hash, scope, issued_by (person_id),
  surface, issued_at, exp, signing_public_key, key_certificate, signature
}
```

- выпускает только `proposal.decide(approve)`, где `actor` — куратор этого харнесса, подтверждённый поверхностью (привязанный Telegram-аккаунт или веб-сессия);
- поверхность решения определяется входом, а не полем запроса: операция с `session_token` — `web`, с `channel_user_id` — `telegram`;
- «Вернуть» по паспорту помечает его версию возвращённой; действующая версия не меняется;
- `subject_hash` = sha256 предмета предложения (паспорт, адаптер, `ChangePlan`, навык); изменился предмет — токен недействителен;
- одноразовый: повторное предъявление → отказ; истёк `exp` → отказ;
- по одному предложению принимается одно решение: второе решение из любой поверхности → `409 already_decided`;
- подпись — Ed25519 по JCS отдельным ключом харнесса; `key_certificate` удостоверяет этот публичный ключ корневым ключом по ADR-008.
- `signing_public_key` — raw Ed25519 public key в base64url без `=`; `key_certificate` — подпись корневым ключом JCS-объекта `{harness_id, signing_public_key}`; `signature` подписывает остальные поля токена, включая сертификат.

## Вход в веб

Вход — Telegram Login по OpenID Connect: Authorization Code Flow с PKCE (ADR-025; прежний виджет с HMAC-подписью Telegram перенёс в архив). Обмен кода и все проверки делает API харнесса, сервис `web` только перенаправляет браузер и хранит cookie.

1. `auth.web.start` — харнесс создаёт одноразовые `state`, `code_verifier`, `nonce` (10 минут) и отдаёт `authorization_url` на `https://oauth.telegram.org/auth` (`response_type=code`, `scope=openid profile`, `code_challenge_method=S256`, `redirect_uri` = `<адрес кабинета>/auth/callback` из конфига, не из запроса). `web` ставит cookie `mb_login` со `state` (`HttpOnly; Secure; SameSite=Lax`; 10 минут) и перенаправляет браузер.
2. Колбэк `/auth/callback?code&state`: `web` сверяет `state` с cookie `mb_login` (защита от подмены входа) и вызывает `auth.web.callback`.
3. Харнесс: `state` известен, не истёк и не использован → обмен `code` на токены (`POST https://oauth.telegram.org/token`, Basic-авторизация Client ID и Client Secret из BotFather, `code_verifier`) → проверка `id_token` по ключам JWKS Telegram: подпись (RS256, ES256 или EdDSA), `iss = https://oauth.telegram.org`, `aud` = Client ID, `exp` не истёк, `nonce` совпадает. Telegram-id пользователя — claim `id`; он должен быть привязан к `person_id` при регистрации, иначе `401 registration_required` и предложение начать с бота. Любая ошибка проверки — `401`.
4. Сессия: случайные `session_token` и `csrf_token`, в БД харнесса хранятся только их хэши; срок — `policy.web.session_ttl_hours`. Вход и выход — в журнал.
5. `web` ставит cookie сессии `HttpOnly; Secure; SameSite=Strict` и отвечает промежуточной страницей с переходом на кабинет: на редиректе, начатом с чужого сайта, cookie `SameSite=Strict` не отправляется.

Операции из кабинета передают `session_token` вместо `channel_user_id`; для всех операций, кроме чтения (`employee.list`, `employee.get`, `employee.autonomy`, `proposal.list`, `journal.list`, `landscape.map`, `skills.list`), нужен ещё `csrf_token` этой сессии, иначе `403 csrf_required`. Отозванная или истёкшая сессия — `401`. Сессия даёт доступ только к харнессу этого человека; других ролей в кабинете в MVP нет.

## Веб-кабинет — экраны

| Экран | Что видно и что можно | API | Д |
|---|---|---|---|
| Сотрудники | список ЦС харнесса, статус, уровни автономии | `employee.list` | ✓ |
| Паспорт ЦС | активная версия паспорта и история версий; правка (обязанности, цели, границы, эскалации, расписание, события) → новая версия-предложение с diff | `employee.get`, `employee.passport.propose` | ✓ |
| Управление ЦС | пауза / возобновление | `employee.pause`, `employee.resume` | |
| Автономия | уровень по каждому классу действий, метрики против порогов, запрос повышения, понижение | `employee.autonomy*` | |
| Предложения | очередь на решение: предмет, источники, превью изменений (`ChangePlan`, diff паспорта); «Подтвердить / Вернуть / Инцидент» | `proposal.list`, `proposal.decide` | ✓ |
| Журнал | лента работ, отчётов, решений, действий с источниками и `score_ref`; фильтры по ЦС и виду | `journal.list` | ✓ |
| Подключения и карта | подключённые системы, форма ввода доступа, карта ландшафта | `landscape.*` | |
| Навыки | личные и навыки компании, версии, происхождение; публикация личного навыка в компанию | `skills.list`, `skill.share` (→ C-5 `skill.propose`) | |

Колонка **Д** — экран входит в демо-срез. Форма ввода доступа отправляет секрет напрямую в secret store и получает `secret_ref`; сам секрет не возвращается ни в одном ответе API.

**Подключение, начатое в Telegram (закрывает ADR-015):**
1. `/connect <система>` → `landscape.connect.start` → бот присылает `cabinet_url` (ведёт на экран «Подключения» с выбранной системой и `connect_request_id`).
2. Человек входит в кабинет (Telegram Login) и вводит доступ в форме → `landscape.secret.put` → `secret_ref`.
3. Кабинет вызывает `landscape.connect` с `secret_ref` и `connect_request_id` → предложение `connect` → решение куратора в кабинете или кнопкой в Telegram.

`connect_request_id` привязан к `harness_id` и `system`, одноразовый, истекает через 30 мин; чужой, истёкший или использованный → отказ. Пока `secret_ref` не создан, предложения нет и подключение не применяется. Демо-срез: шаги 1–3 обязательны, т.к. подключение `demo-landscape` идёт по этому пути.

## Telegram-адаптер

| Telegram | API |
|---|---|
| `/start` (+ инвайт-параметр) | `person.register` |
| `/invite` | `company.invite` |
| `/role <текст>` | `employee.draft kind=twin` |
| `/hire <текст>` | `employee.draft kind=new` |
| `/team` | `employee.list` |
| `/cabinet` | ссылка на веб-кабинет |
| `/connect <система>` | `landscape.connect.start` → ссылка в кабинет; доступ вводится только там |
| `/map` | `landscape.map` |
| свободный текст, `@<сотрудник> текст` | `ask` |
| кнопки «Подтвердить / Вернуть / Инцидент» под предложением | `proposal.decide surface=telegram` |
| сообщение от системы | `report` |

Привязка `channel_user_id → person_id` фиксируется при регистрации. Callback от другого Telegram-аккаунта → отказ. Если предложение уже решено в кабинете, кнопки в Telegram заменяются на статус решения.

## Инварианты

- суб-роли не экспонируются: в ответах, сообщениях и экранах нет идентификаторов аналитика, разработчика, критика;
- каждый `answer` и `report` несёт `sources[]` и `score_ref` валидного ScoreCard (C-4);
- все операции фильтруются по `harness_id` вызывающего; чужой `harness_id` → `403`;
- правка паспорта и повышение автономии в кабинете создают предложение, а не меняют состояние напрямую (C-010);
- решения куратора, вход в кабинет и выпуск токенов — в журнал.

## Golden

| ID | Кейс | Ожидание |
|---|---|---|
| GT-c1-01 | `ask` по данным подключённой демо-системы | ответ с `sources[]` и валидным `score_ref` |
| GT-c1-02 | рой выдал ответ с фактом без источника | ответ не доставлен, человеку — сообщение о блоке |
| GT-c1-03 | применение предложения без `consent_token` | отказ |
| GT-c1-04 | `proposal.decide` от не-куратора / с чужого Telegram-аккаунта | отказ, запись в журнал |
| GT-c1-05 | повторное использование `consent_token`; истёкший токен; токен при изменённом предмете | отказ во всех трёх |
| GT-c1-06 | два человека одной компании: `landscape.map`, `employee.list`, `ask`, `journal.list` с чужим `harness_id` | `403`, данных нет |
| GT-c1-07 | ответ, сообщения канала, экраны кабинета | нет имён суб-ролей |
| GT-c1-08 | `/start` с истёкшим или несуществующим инвайтом | отказ, компания не создаётся и человек никуда не добавлен |
| GT-c1-09 | `id_token` с чужой подписью, неизвестным `kid`, чужим `aud` или `iss`, истёкший, с другим `nonce`; Telegram-id без регистрации; неизвестный или повторный `state` | `401` во всех случаях, сессия не создана |
| GT-c1-10 | мутация в кабинете без CSRF-токена или с токеном другой сессии; чтение без CSRF | `403`; чтение — `200` |
| GT-c1-11 | предложение подтверждено в кабинете, затем нажата кнопка в Telegram | `409 already_decided`, в журнале одно решение |
| GT-c1-12 | правка паспорта в кабинете | создано предложение `passport` с diff; активная версия не изменилась до согласия |
| GT-c1-13 | секрет, введённый в форме подключения | нет в ответах API, логах, журнале, трейсе; есть только `secret_ref` |
| GT-c1-14 | запрос повышения автономии при невыполненных порогах | отказ со списком `unmet[]`, предложение не создано |
| GT-c1-15 | `connect_request_id` истёкший / использованный / другого харнесса; `landscape.connect` без `secret_ref` | отказ, предложение `connect` не создано |
