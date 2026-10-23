# C-3 · Адаптер интеграции (самодописывание харнесса)

Исследователь (разработчик из его роя) **драфтит** адаптер к системе человека. Адаптер проходит валидатор и согласие куратора — потом применяется (C-006). Домен систем не зафиксирован (OPEN-01); в MVP — демо-ландшафт `demo-landscape`.

## Интерфейс адаптера

```
discover() -> LandscapeDelta               # read-only: что за система, какие объекты, связи
probe(ref) -> Facts                         # read-only чтение по ссылке
capabilities() -> {reads[], writes[]: {action_class, reversible, description}}
subscribe() -> events[]                     # опц.: какие события адаптер публикует в шину (R-17)

preview(action) -> ChangePlan               # что изменится; без изменений в системе
execute(action, grant) -> ActionResult      # запись; только с валидным grant
verify(action, result) -> VerifyResult      # пост-проверка: система в ожидаемом состоянии
```

- `Facts`: `{ref, data, source: {system, object_ref, read_at}, sensitivity: {field: label}}`. `source` — основа для `sources[]` в ответах (C-008), `sensitivity` — для DLP (C-2).
- `action`: `{action_class, target_ref, params, idempotency_key}`.
- `grant`: `{level: L1 | L2, scorecard_ref (профиль Action), consent_token? (обязателен для L1)}`.
- `ChangePlan`: `{action_class, target_ref, before, after, reversible}`.
- Событие: `{subject: "landscape.<harness_id>.<system>.<event>", ref, occurred_at}`.

## Среда исполнения адаптера

Адаптер не видит ни секретов, ни сети. Доступ к системе ему передаёт харнесс готовым клиентом.

```python
# adapter.py — обязательный файл драфта
class Adapter:
    def __init__(self, http):            # http: SystemHTTP, привязан к системе манифеста
        ...
    def capabilities(self) -> dict: ...  # {reads[], writes[]}
    def discover(self) -> dict: ...      # LandscapeDelta
    def probe(self, ref: str) -> dict: ...  # Facts
```

- `SystemHTTP.get(path, params=None) -> JSON`: `path` относительно корня системы (`/orders`, `/orders/1042`, `/schema`). Базовый адрес и `Authorization` из `auth.secret_ref` подставляет харнесс; в sandbox — тест-стенд валидатора. `post/put/patch/delete` появятся с классами записи (T-25); в демо-срезе любой вызов, кроме `get`, — нарушение.
- Ошибка HTTP приходит исключением `LookupError` (404) или `PermissionError` (401/403).
- `ref` = `<объект>/<ключ>`, например `order/1042`, `item/A-17`.
- `LandscapeDelta` = `{system, objects[]: {name, key, fields[], sensitivity{field: label}, links[], count?, keys?}}` — та же форма, что `schema` систем демо-ландшафта; `keys` — ключи видимых объектов (не больше 50), по ним рой ЦС делает `probe` (C-6).
- Разрешённые импорты: `json`, `re`, `datetime`, `dataclasses`, `typing`, `collections`, `itertools`, `functools`, `math`, `decimal`, `enum`, `string`, `urllib.parse` и другие `.py`-файлы самого драфта. Остальное, включая `os`, `sys`, `socket`, `httpx`, — fail чекера security (C-4).
- `tests[]` манифеста — список `ref`, которые `probe` обязан отдавать правильно на тест-стенде.

## Исследователь: драфт и исполнение (T-19)

- **Реестр систем.** Какие системы можно подключить и где они живут — конфиг `LANDSCAPE_SYSTEMS`: `{system: {base_url, test_data_ref}}`. Демо: `crm`, `stock`, `pricing` на `demo-landscape` с тест-стендом `demo-landscape:fixture-v1`.
- **Подключение.** Согласие на `connect` (C-1) применяет харнесс: система записывается в подключения харнесса со ссылкой на секрет.
- **Драфт.** Исследователь читает `GET /schema` системы (с токеном из `secret_ref`) и генерирует адаптер по шаблону для систем со схемой (ADR-023); манифест: `auth.secret_ref` подключения, `read_scopes = <объект>:read`, `writes = []`, `tests = []`. Драфт проходит профиль Adapter; при `pass` — предложение `adapter` с предметом `{system, manifest, files_sha256, scorecard_id}`, при `fail` — отказ куратору с причинами.
- **Применение и discovery.** После согласия адаптер становится активным. Харнесс исполняет его тем же изолированным процессом, что и sandbox (ADR-017), но вместо тест-стенда `SystemHTTP.get` проксируется харнессом в систему: харнесс сам подставляет `base_url` и токен и принимает только `GET` в пределах системы. Токен и сеть адаптеру недоступны. Результат `discover()` — карта ландшафта (`landscape.map`); каждый вызов адаптера — в журнал.

## Манифест

```
{ system, version, auth: {kind, secret_ref}, read_scopes[], writes[]: {action_class, reversible},
  provenance: {author: employee_id, job_id, created_at}, tests[]: ref }
```

Секреты — только ссылкой на secret store: `env:<NAME>` (ADR-014) или `vault:<id>` — хранилище харнесса для доступов, введённых в кабинете (ADR-028).

## Жизненный цикл

```
draft (код + манифест)
  -> валидатор, профиль Adapter: schema · sandbox на тест-данных · security · eval -> ScoreCard
  -> предложение adapter -> согласие куратора (consent_token)
  -> apply: адаптер активен, все writes на L0
  -> (опц.) повышение автономии по классу действия (C-6)
```

## Инварианты

- адаптер не применяется без ScoreCard pass и согласия (R-08);
- `execute` без `grant`, с `grant` чужого харнесса, с классом вне `writes[]` или с уровнем ниже требуемого → блок (C-007);
- повтор `execute` с тем же `idempotency_key` не меняет систему повторно и возвращает прежний результат (C-013);
- Исследователь вызывает только `discover`, `probe`, `subscribe` (R-03);
- каждый вызов адаптера — в журнал с `harness_id`, `employee_id`, `job_id`.

## Golden

| ID | Кейс | Ожидание |
|---|---|---|
| GT-c3-01 | draft → validate → consent → apply → discover на demo-landscape | карта построена, вызовы только read |
| GT-c3-02 | apply без ScoreCard или без согласия | блок |
| GT-c3-03 | `execute` без grant; с L1-grant без consent_token; класс вне `writes[]` | блок во всех |
| GT-c3-04 | sandbox: драфт, пытающийся ходить в сеть вне тест-стенда / читать боевой секрет | ScoreCard fail |
| GT-c3-05 | двойной `execute` с одним `idempotency_key` | одно изменение в системе |
| GT-c3-06 | `probe` поля с `sensitivity` | метка доходит до DLP (C-2) |
