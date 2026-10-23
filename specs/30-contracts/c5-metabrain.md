# C-5 · Мета-мозг (слой 3, наружу только MCP)

Копит опыт и отдаёт его ЦС. Проекции: на харнесс (личные навыки, ретривал по данным харнесса) и на компанию (навыки компании). Единственная наружная поверхность — MCP-сервер со scope из policy.

## Операции

Каждый вызов несёт контекст вызывающего харнесса `{company_id, harness_id}`; мета-мозг фильтрует по нему всё, что читает и пишет.

```
skills.resolve   {company_id, harness_id, lock? | query?, task_class?, role_tags?, limit?} -> {skills[]}
    # lock = "skill_id@version": ровно эта версия, если она видна харнессу, иначе пусто
    # без lock: последние версии видимых навыков; фильтр task_class, пересечение role_tags, полнотекст по query
retrieval.index  {company_id, harness_id, docs[]: {doc_id, kind: fact | trajectory | card, text, source?, sensitivity?}} -> {indexed}
    # upsert по (harness_id, doc_id); пишет харнесс: факты ландшафта, траектории работ, карточки знаний
retrieval.search {company_id, harness_id, query, kinds?, limit?} -> {hits[]: {doc_id, kind, text, source, score}}
policy.get       {company_id, harness_id, employee_id?, sub_role?} -> Policy
skill.propose    {company_id, harness_id, candidate, source_job_summaries[]}
    -> {candidate_id, artifact_hash, verdict, scorecard}
    # мета-мозг отправляет Skill-артефакт (C-4) в валидатор и хранит кандидата со ScoreCard
skill.publish    {company_id, harness_id, candidate_id, consent_token?} -> {skill_id, version}
```

Транспорт: для харнесса — внутренний HTTP JSON в сети compose (`POST /v1/skills/resolve`, `/v1/retrieval/index`, `/v1/retrieval/search`, `/v1/policy/get`, `/v1/skills/propose`, `/v1/skills/publish`, `GET /healthz`); для внешних клиентов — те же операции как инструменты MCP (T-24). Внутренний HTTP наружу не публикуется.

## Публикация навыка

1. Харнесс (критик ЦС) вызывает `skill.propose`. Мета-мозг вызывает валидатор (профиль Skill) и сохраняет кандидата с ScoreCard.
2. `scope=personal`: харнесс сразу вызывает `skill.publish` — нужен только ScoreCard pass.
3. `scope=company`: харнесс создаёт предложение `skill_publish` (C-1) с предметом `{"candidate_id", "artifact_hash"}` и показывает куратору сам навык. После согласия — `skill.publish` с `consent_token`.

`skill.publish` проверяет:
- ScoreCard кандидата: подпись по ключам валидатора, `artifact_hash`, `harness_id`, `verdict=pass`, `profile=skill` (C-4);
- для `scope=company` — `consent_token`: сертификат `signing_public_key` по корневому `CONSENT_PUBLIC_KEY` и подпись Ed25519 этим ключом (ADR-008), `harness_id`, `scope=skill_publish`, `subject_hash` = sha256(JCS(`{"candidate_id", "artifact_hash"}`)), срок `exp`; токен одноразовый и для мета-мозга: повтор → отказ;
- кандидат публикуется один раз; повтор → `409`.

Прямого write-API в библиотеку нет.

Формат `consent_token` на проводе: JSON с полями ConsentToken из C-1, идентификаторы — строками, время — ISO 8601 UTC, `signing_public_key`, `key_certificate` и `signature` — base64url без `=`. Мета-мозг проверяет сертификат корневым ключом и токен — удостоверенным ключом харнесса.

## MCP (внешняя поверхность, T-24)

Отдельный сервис `metabrain-mcp` (ADR-022): единственная часть мета-мозга, которую можно публиковать наружу (за HTTPS-прокси). Внутренний HTTP мета-мозга наружу не выходит.

- **Транспорт:** Streamable HTTP, один эндпоинт `POST /mcp`, ответ — один JSON-объект (SSE не используется). `GET`/`DELETE` → `405`. Сессий нет, `Mcp-Session-Id` игнорируется.
- **Версии:** современная `2026-07-28` (версия и возможности клиента — в `_meta` каждого запроса, `server/discover`, заголовки `MCP-Protocol-Version`, `Mcp-Method`, `Mcp-Name` сверяются с телом, расхождение → `400` и `-32020`; неподдерживаемая версия → `400` и `-32022` со списком `supported`) и устаревшие `2025-11-25`, `2025-06-18`, `2025-03-26` через `initialize` (без сессий). Неизвестный метод → `-32601`.
- **Клиенты и авторизация:** `Authorization: Bearer <token>`. Реестр клиентов — секрет `MCP_CLIENTS`: `{token: {client_id, company_id, harness_id?}}`. Нет токена или он неизвестен → `401`. Заголовок `Origin` есть и не из `MCP_ALLOWED_ORIGINS` → `403`. OAuth 2.1 из спецификации MCP — OPEN-16.
- **Проекция задаёт привязка клиента, а не аргументы:** `company_id` и `harness_id` берутся только из реестра. Клиент, привязанный к харнессу, видит его личные навыки, навыки компании и его ретривал; клиент уровня компании — только навыки компании.
- **Инструменты** (все только читают; `readOnlyHint: true`):

| Инструмент | Операция C-5 | Аргументы | Кому |
|---|---|---|---|
| `skills_resolve` | `skills.resolve` | `lock?`, `query?`, `task_class?`, `role_tags?`, `limit?` | всем |
| `retrieval_search` | `retrieval.search` | `query`, `kinds?`, `limit?` | только клиентам с `harness_id` |
| `policy_get` | `policy.get` | `employee_id?`, `sub_role?` | всем |

- **Scope:** `policy.mcp_scopes {client_id -> [инструменты]}`. `tools/list` показывает только разрешённые; вызов неразрешённого — ошибка `-32602` «неизвестный инструмент» (существование не раскрывается) и запись `forbidden` в журнал. Публикации навыков через MCP нет.
- **Журнал:** каждый `tools/call` (разрешённый или нет) — в журнал мета-мозга с `client_id`; отказ авторизации — тоже.

## Навык

```
Skill {
  skill_id, version (semver), scope: personal | company,
  company_id, owner_harness_id (для personal; для company — author_person_id),
  title, applies_to {role_tags[], task_class},
  body,                       # процедура «как делать»: шаги, правила, проверки
  examples[],                 # обезличенные для scope=company
  supersedes?,                # skill_id своего навыка того же scope: публикация = следующая minor-версия
  provenance {source_jobs[], scorecard_ref, consent_ref?, created_at},
}
```

Навык неизменяем: правка = новая версия (`supersedes`). `lock` = `skill_id@version`.

## Policy

Policy-as-code, версионируется (`version`). В демо-срезе одна политика на установку — файл в репо; политики компаний — позже. Содержит:

```
version
llm_routing {
  routes {sub_role -> provider_name},                     # analyst, developer, critic, researcher
  providers {provider_name -> {model, family, max_tokens, temperature}}
}                                                         # base_url и ключи — конфиг развёртывания, не policy
pricing {currency, as_of, source, tier, per_million_tokens {provider_name -> {input_cache_hit, input_cache_miss, output}}}
                                                          # для budget_guard (C-2); у каждого провайдера из routes должна быть цена
limits {
  budget_usd_total {installation},                        # общий потолок установки
  budget_usd_per_day {harness, employee},                 # employee ≤ harness ≤ total
  tokens_per_day {harness, employee},
  actions_per_hour {employee}
}                                                         # null = лимит не задан; значения демо — ADR-021
dlp_rules {label -> mask | drop}
autonomy {
  allowed_l2_classes {employee_id -> [action_class]},
  promotion_thresholds {                                  # стартовые значения, ADR-011
    L0_to_L1 {min_decided: 10, accepted_ratio: 0.80},
    L1_to_L2 {min_decided: 20, accepted_ratio: 0.90, eval_score: 0.90, incident_free_days: 14}
  }
}
max_employees                                             # OPEN-11
web {session_ttl_hours}                                   # срок сессии кабинета (C-1, «Вход в веб»)
grounding_threshold
mcp_scopes {client -> tools[]}
```

`policy.get` с `employee_id` сужает `allowed_l2_classes` до этого ЦС, с `sub_role` — добавляет `llm` = провайдер этой роли.

## Хранилища

Skill Library (semver, immutable, provenance), Policy, Eval Corpus (golden-кейсы по классам задач), Retrieval Index (по ландшафту, траекториям и карточкам; строки с `company_id`, `harness_id`).

Демо-срез: Skill Library и Retrieval Index — Postgres, ретривал — полнотекстовый (`russian` + `simple`), без векторов. Векторный ретривал в Qdrant — когда выбран провайдер эмбеддингов (у DeepSeek эмбеддингов нет; OPEN-14).

## Правила

- запись навыков — только через валидатор (C-5 ↔ C-4);
- проекция: `retrieval.search` никогда не возвращает данные другого харнесса; `skills.resolve` никогда не возвращает навыки другой компании и чужие personal;
- каждый вызов — в журнал мета-мозга (операция, `company_id`, `harness_id`, исход); журнал только дописывается (C-008, C-009);
- scope MCP — из policy.

## Golden

| ID | Кейс | Ожидание |
|---|---|---|
| GT-c5-01 | `skills.resolve` по `lock` | ровно указанная версия; чужая версия по lock — пусто |
| GT-c5-02 | `retrieval.search` харнесса B по данным харнесса A той же компании | пусто |
| GT-c5-03 | `skill.publish` кандидата без ScoreCard pass / с поддельной подписью | блок |
| GT-c5-04 | `scope=company` без consent_token автора; с токеном другого предмета, другого харнесса, истёкшим или повторным | блок |
| GT-c5-05 | опубликованный навык компании | виден харнессу B той же компании, не виден харнессу другой компании |
| GT-c5-06 | MCP-вызов инструмента вне scope клиента | отказ `-32602` + запись `forbidden` в журнал; в `tools/list` его нет |
| GT-c5-08 | MCP-клиент харнесса A ищет навыки и документы | видит своё и навыки компании; аргументами нельзя подменить `company_id`/`harness_id` |
| GT-c5-09 | MCP без токена / с неизвестным токеном / с чужим `Origin` | `401` / `401` / `403` |
| GT-c5-10 | MCP 2026-07-28: расхождение заголовков с телом; неподдерживаемая версия; `server/discover` | `400 -32020`; `400 -32022` со списком версий; версии, возможности, `serverInfo` |
| GT-c5-11 | MCP-клиент 2025-11-25: `initialize` → `tools/list` → `tools/call` | работает без сессии |
| GT-c5-07 | `supersedes` | новая minor-версия того же `skill_id`; resolve без lock отдаёт её; старая доступна по lock; изменить опубликованную версию нельзя |
