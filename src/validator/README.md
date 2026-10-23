# Слой 2 — валидатор

Сервис `validate(artifact, profile, context)` → подписанный ScoreCard (Ed25519 по JCS, ADR-008). Профили Answer, Passport, Adapter, Action, Skill (C-4). Реализация — `specs/50-tasks.md` M3.

- `scorecard.py` — формат ScoreCard, подпись, `verify_scorecard` для потребителей (харнесс вызывает её перед выпуском ответа или применением артефакта).
- `service.py` — профили, наборы профилей (`mvp-v1`, `demo-v1`, ADR-016) и сборка вердикта. Гейт закрыт по умолчанию: чекер, которого нет или который упал, даёт `fail`.
- `checkers.py` — реестр чекеров по `(профиль, имя)`.
- `checks/` — чекеры по правилам C-4, раздел «Чекеры»:
  - `answer.py` — grounding, pii_dlp;
  - `passport.py` — schema, rights, autonomy;
  - `skill.py` — schema, pii_dlp, foreign_data, provenance;
  - `adapter.py` — schema, security (статический разбор), sandbox, eval;
  - `stand.py` — тест-стенд из фикстуры `demo-landscape`; сам драфт исполняется общим раннером `src/common/adapter_runner.py` (ADR-017, ADR-023).
- `app.py` — HTTP: `POST /v1/validate`, `GET /v1/keys`, `GET /healthz`. Запуск: `python -m src.validator.app`.

Ещё не реализованы: `judge` (Answer), `eval` (Skill), профиль Action — в наборе `mvp-v1` они дают `fail/not_implemented`.

## Конфигурация

- `VALIDATOR_SIGNING_KEY` — raw Ed25519 32 байта в base64 (тот же формат, что `CONSENT_SIGNING_KEY`). Без ключа сервис здоров, но `/v1/validate` отвечает `503 signing_key_not_configured`. Сгенерировать:

  ```bash
  python -c "import base64;from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey as K;from cryptography.hazmat.primitives.serialization import Encoding as E,PrivateFormat as P,NoEncryption as N;print(base64.b64encode(K.generate().private_bytes(E.Raw,P.Raw,N())).decode())"
  ```

- `VALIDATOR_PROFILE_SET` — `demo-v1` (по умолчанию в compose) или `mvp-v1` (по умолчанию в коде).
