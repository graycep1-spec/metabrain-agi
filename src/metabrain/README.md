# Слой 3 — мета-мозг

Skill Library (личные навыки и навыки компании) · Policy · Retrieval; наружу только MCP (C-5, MCP — T-24). Проекции на харнесс и на компанию. Реализация — `specs/50-tasks.md` M6; демо-упрощения — ADR-018.

- `service.py` — операции C-5: `skills.resolve`, `skill.propose`, `skill.publish`, `retrieval.index`, `retrieval.search`, `policy.get`; каждый вызов пишется в `mb_audit`.
- `store.py`, `schema.sql` — Postgres: навыки (неизменяемые, semver), кандидаты, использованные согласия, ретривал (полнотекстовый `russian` + `simple`), журнал.
- `consent.py` — проверка `consent_token` по публичному ключу куратор-гейта.
- `validator_client.py` — вызов валидатора (профиль Skill) и кэш его ключей.
- `policy/default.json` — политика установки (версия в поле `version`).
- `app.py` — внутренний HTTP (`POST /v1/...`, `GET /healthz`). Запуск: `python -m src.metabrain.app`.

## Публикация навыка (для харнесса, T-23)

1. `POST /v1/skills/propose {company_id, harness_id, candidate, source_job_summaries}` → `{candidate_id, artifact_hash, verdict, scorecard}`.
2. Личный навык: `POST /v1/skills/publish {company_id, harness_id, candidate_id}`.
3. Навык компании: предложение `skill_publish` с предметом `{"candidate_id", "artifact_hash"}` → согласие куратора → `publish` с `consent_token` (поля ConsentToken строками, как в `ProposalStore._token_payload`, плюс `signature`). Рабочий пример — `test_consent_issued_by_the_harness_proposal_store_is_accepted` в `tests/test_metabrain.py`.

## Конфигурация

- `METABRAIN_DATABASE_URL` — своя роль БД (ADR-019). Без БД сервис здоров, но операции отвечают `503`.
- `VALIDATOR_URL` — по умолчанию `http://validator:8080`.
- `CONSENT_PUBLIC_KEY` — публичный корневой ключ, удостоверяющий отдельные ключи подписи харнессов. Получить из `CONSENT_SIGNING_KEY`:

  ```bash
  python -c "import base64,sys;from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;from cryptography.hazmat.primitives.serialization import Encoding as E,PublicFormat as F;print(base64.b64encode(K.from_private_bytes(base64.b64decode(sys.argv[1])).public_key().public_bytes(E.Raw,F.Raw)).decode())" "$CONSENT_SIGNING_KEY"
  ```

  Без ключа навыки компании не публикуются (`consent_key_not_configured`), личные — публикуются.

## MCP (внешняя поверхность, T-24)

`mcp.py` — отдельный сервис `metabrain-mcp` (`python -m src.metabrain.mcp`, в compose — `127.0.0.1:8082`, наружу только за HTTPS-прокси). Эндпоинт `POST /mcp`, Streamable HTTP, ответы JSON. Поддерживает MCP `2026-07-28` и клиентов `2025-11-25`/`2025-06-18`/`2025-03-26` через `initialize` (ADR-022).

Инструменты только читают: `skills_resolve`, `retrieval_search` (только клиентам, привязанным к харнессу), `policy_get`. Проекция данных — из привязки токена клиента, аргументами её не поменять.

Подключить клиента:
1. Выдать токен (≥ 24 символов) в секрете `MCP_CLIENTS`: `{"<token>": {"client_id": "demo-ide", "company_id": "<uuid>", "harness_id": "<uuid>"}}`.
2. Разрешить инструменты в `policy/default.json` → `mcp_scopes["demo-ide"]` (в демо-политике уже разрешены все три).
3. В MCP-клиенте указать URL `https://<хост>/mcp` и заголовок `Authorization: Bearer <token>`.
