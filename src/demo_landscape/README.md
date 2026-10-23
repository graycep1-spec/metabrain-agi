# demo-landscape

Учебные системы для демо и E2E: `crm` (заявки), `stock` (склад), `pricing` (цены). Только чтение. Спека — `specs/60-demo-landscape.md`, задача T-18.

- `fixture.json` — данные: заявки 1042 (manager-a) и 1043 (manager-b) с дефицитом позиции и подходящим аналогом.
- `data.py` — загрузка фикстуры и `schema` трёх систем со связями и разметкой PII.
- `app.py` — HTTP API: `GET /{system}/schema`, `/crm/orders`, `/stock/items`, `/pricing/prices`. Запуск: `python -m src.demo_landscape.app`.

Доступ — `Authorization: Bearer <token>`, таблица токенов в `DEMO_LANDSCAPE_TOKENS`, пример для локального `.env`:

```
DEMO_LANDSCAPE_TOKENS={"demo-a":{"user":"manager-a","systems":["crm","stock","pricing"]},"demo-b":{"user":"manager-b","systems":["crm","stock","pricing"]}}
```

Без таблицы сервис здоров, но на запросы данных отвечает `503`.
