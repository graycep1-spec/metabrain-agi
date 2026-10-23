# C-4 · Валидатор (слой 2)

Отдельный сервис. Всё исходящее и всё применяемое проходит профиль чекеров и получает **подписанный ScoreCard**. Без него — наружу и в системы ничего (R-09, C-005).

```
validate(artifact, profile, context{company_id, harness_id, employee_id?, job_id?}) -> ScoreCard
verify_signature(scorecard) -> bool         # доступно всем сервисам по публичному ключу
```

## Транспорт

Внутренний HTTP JSON (сеть compose, наружу не публикуется):

```
POST /v1/validate {artifact, profile, context}  -> ScoreCard          # 200 и при verdict=fail; 4xx — только невалидный запрос
GET  /v1/keys                                   -> {keys[]: {key_id, alg: "Ed25519", public_key (base64url)}}
GET  /healthz
```

Потребители проверяют подпись сами по `/v1/keys` (ключи кэшируются по `key_id`). `artifact_hash` = sha256 от JCS(artifact).

## Форма артефактов

```
Answer   { text, claims[]: {claim_id, text, source_ids[]}, sources[]: {source_id, system, object_ref, read_at, skill_ref?} }
Passport { passport: RolePassport (C-6), curator_scopes: {connectors[], read_scopes[], write_classes[]}, previous_passport?: RolePassport }
Adapter  { manifest (C-3), files: {path -> content}, test_data_ref }      # test_data_ref в демо-срезе: "demo-landscape:fixture-v1"
Action   { action (C-3), change_plan, grant, current_level }
Skill    { candidate: Skill (C-5) без skill_id/version, source_job_summaries[]: {job_id, identifiers[]} }
         # identifiers — имена клиентов, id объектов и людей из исходных работ; foreign_data ищет их в кандидате
```

Grounding считает долю `claims` без `source_ids` или со ссылкой на отсутствующий `source_id`. Разбиение ответа на `claims` делает рой ЦС (критик), валидатор только проверяет.

## ScoreCard

```
ScoreCard {
  scorecard_id, artifact_hash (sha256 канонического артефакта), profile, profile_set,
  context {company_id, harness_id, employee_id?, job_id?},
  verdict: pass | fail,
  checks[]: {name, verdict, metrics{}, log_ref},
  issued_at, key_id, signature
}
signature = Ed25519( JCS(RFC 8785)(ScoreCard без поля signature) )   # ADR-008
```

Потребитель проверяет: подпись, совпадение `artifact_hash` с тем, что он собирается выпустить/применить, совпадение `context.harness_id`. Любое несовпадение = unsigned.

## Профили

Состав чекеров задаёт **набор профилей** (`profile_set`), он записывается в подписанный ScoreCard. Валидатор работает с одним набором, его выбирает конфиг сервиса (`VALIDATOR_PROFILE_SET`).

| Набор | Отличие | Где |
|---|---|---|
| `mvp-v1` | полный состав по таблице ниже | целевой MVP |
| `demo-v1` | без `judge` в Answer и без `eval` в Skill: им нужны LLM-судья и eval-корпус, которых в демо-срезе нет (ADR-016) | трек D |

Потребитель может требовать конкретный `profile_set`; при переходе на `mvp-v1` ScoreCard'ы `demo-v1` перестают приниматься.

| Профиль | Артефакт | Чекеры |
|---|---|---|
| **Answer** | ответ / отчёт ЦС | grounding (каждый факт со ссылкой на `sources[]`; доля непокрытых ≤ порога policy) · pii/dlp (нет чувствительных категорий сверх прав харнесса) · judge (судья по рубрике, модель другого семейства; только выставляет оценку, артефакт не правит) |
| **Passport** | паспорт роли (C-6) | schema · rights (инструменты и scopes ⊆ подключения и права куратора) · autonomy (новый паспорт: все классы на L0; изменение уровня только через `autonomy_change`) |
| **Adapter** | драфт адаптера (C-3) | schema манифеста и файлов · security (статический разбор: нет хардкод-секретов, импорты только из разрешённых, нет опасных вызовов, scopes минимальны) · sandbox (запуск на тест-данных без доступа к боевым; не запускается, если security не прошёл) · eval (вывод `discover`/`probe` соответствует тест-стенду, разметка `sensitivity` сохранена, `tests[]` адаптера проходят) |
| **Action** | `action` + `ChangePlan` | policy_scope (класс разрешён этому ЦС на текущем уровне) · capability (класс есть в `writes[]` активного адаптера) · limits (частота и бюджет из policy) · preview (ChangePlan присутствует и соответствует action) · consent (на L1 — валидный consent_token) |
| **Skill** | кандидат навыка (C-5) | schema · pii/dlp · foreign_data (для `scope=company`: нет сырых записей, идентификаторов объектов и людей харнесса автора) · eval (на eval-кейсах класса задач не хуже без навыка) · provenance (есть исходные job) |

Пост-проверка L2: результат `verify` (C-3) записывается в ScoreCard действия отдельным чекером `post_verify`; fail → инцидент (C-6).

## Чекеры

Чекер = один контракт `check(artifact, context) -> {verdict, metrics, log}`. Целевой MVP: новый чекер = новый контейнер. Демо-срез: чекеры — модули внутри сервиса валидатора, sandbox — отдельный процесс (ADR-017). «Валидатор валидируется»: у каждого чекера свой golden-набор pass/fail в CI.

Правила чекеров демо-набора (детерминированные):

| Чекер | Профиль | fail, если |
|---|---|---|
| grounding | Answer | нет `claims`; у claim нет `source_ids`; ссылка на отсутствующий `source_id`; у источника нет `system`/`object_ref`/`read_at`; доля непокрытых > `grounding_threshold` (по умолчанию 0) |
| pii_dlp | Answer | в тексте ответа номер карты (Луна), паспорт РФ рядом со словом «паспорт», СНИЛС, секрет/токен. Телефон и email клиента — в правах владельца харнесса, допустимы |
| pii_dlp | Skill | любая из категорий выше **и** телефон, email |
| schema | Passport, Adapter, Skill | структура не совпадает с формой артефакта; у Skill есть `skill_id`/`version` или `company_id` ≠ контекста; у Adapter нет `adapter.py`, путь файла абсолютный или с `..` |
| rights | Passport | `passport.harness_id` ≠ контекста; `tools.connectors`, `read_scopes`, `write_classes` не ⊆ `curator_scopes`; у Исследователя есть `write_classes` |
| autonomy | Passport | уровень не из L0/L1/L2; новый паспорт (без `previous_passport`) выше L0; уровень отличается от `previous_passport` (меняется только через `autonomy_change`) |
| security | Adapter | хардкод-секрет (известные форматы ключей или строковая константа в переменной вида token/secret/password/api_key); импорт вне списка C-3; вызовы `eval`/`exec`/`compile`/`open`/`__import__`/`globals`/`vars`; доступ к dunder-атрибутам; `auth.secret_ref` не ссылка secret store (`env:<NAME>` или `vault:<uuid>`); в `read_scopes` есть `*`, `admin` или `write` |
| sandbox | Adapter | security fail; попытка сети, файла вне каталога адаптера, процесса или нативного кода; любой метод HTTP кроме GET; исключение; таймаут 10 с |
| eval | Adapter | `capabilities().writes` ≠ `manifest.writes`; `discover` не покрывает объекты схемы системы; `probe(ref)` для образцов стенда и `tests[]` даёт данные, не равные стенду, без `source` или без меток `sensitivity` из схемы |
| foreign_data | Skill | `scope=company` и в `title`/`body`/`examples` есть идентификатор из `source_job_summaries` (без учёта регистра) |
| provenance | Skill | нет `source_jobs`; job без сводки в `source_job_summaries`; нет `created_at` |

## Golden

| ID | Кейс | Ожидание |
|---|---|---|
| GT-c4-01 | ответ с фактом без источника | Answer fail |
| GT-c4-02 | адаптер с хардкод-секретом | Adapter fail |
| GT-c4-03 | артефакт без ScoreCard / с подписью другим ключом / с чужим `artifact_hash` | наружу блок |
| GT-c4-04 | паспорт с инструментом, не подключённым у куратора; новый паспорт с L1 | Passport fail |
| GT-c4-05 | action класса, не разрешённого на текущем уровне; L1 без consent_token | Action fail |
| GT-c4-06 | кандидат навыка scope=company с именем клиента из данных харнесса | Skill fail |
| GT-c4-07 | ScoreCard харнесса A предъявлен в харнессе B | блок |
