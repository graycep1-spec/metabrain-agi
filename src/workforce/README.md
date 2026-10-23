# Workforce — цифровые сотрудники (C-6)

- `passport.py` — черновик паспорта разработчиком роя (`LLMPassportDrafter`, `parse_draft`) и сборка RolePassport (T-13).
- `service.py` — `WorkforceService`: `draft` (LLM → валидатор Passport → предложение), `on_passport_approved` (одноразовое согласие → активная версия), `propose_tool_extension` (новая система → новая версия паспорта только через согласие), `list`.
- `cron.py`, `runtime.py` — runtime работ (T-15): планировщик по `triggers.schedule`, исполнитель с арендой, шаги, переживающие рестарт.
- `reports.py` — доставка отчётов: журнал харнесса и Telegram куратору.
- `store.py`, `schema.sql` — сотрудники, неизменяемые версии паспортов, работы и шаги (ADR-024).

## Рой (T-14)

`swarm.py` — `SwarmHandler`, обработчик обязанности: collect (факты через адаптеры, только чтение) → skills (навыки из мета-мозга, T-23) → analyze (аналитик, LLM) → review (критик, LLM другого семейства) → validate (Answer, подпись проверяется до выдачи) → propose (рекомендация → предложение `recommendation`). Факты уходят модели только плейсхолдерами коннектора (DLP), текст ответа собирается только из утверждений с источниками (ADR-026).

- Работы по расписанию: `RuntimeThread` в процессе харнесса (`build_runtime_thread`, своё подключение в autocommit); отчёт — в журнал и куратору в Telegram.
- `ask` (C-1): работа с триггером `ask`, исполняется в запросе через `Runtime.run_job`; 422 `answer_blocked`, 409 `employee_busy`, 404 — нет активного сотрудника, 429 — бюджет.

## Навыки (T-23)

`skills.py` — `SkillFlow` (ADR-027): одобренная рекомендация → разработчик роя обобщает её в навык (факты — плейсхолдерами) → личный навык публикуется сразу → навык компании проходит профиль Skill (`foreign_data` по идентификаторам работы) и уходит куратору предложением `skill_publish` → после согласия `skill.publish` с `consent_token` в мета-мозг. Рой берёт последние 5 видимых навыков шагом `skills` (`visible_skills`) и ссылается на них как на источник.

- Включается переменной `METABRAIN_URL` у харнесса (в compose — `http://metabrain:8080`); у мета-мозга должен быть корневой `CONSENT_PUBLIC_KEY`, соответствующий `CONSENT_SIGNING_KEY` харнесса (`src/metabrain/README.md`). Без `METABRAIN_URL` рой работает без навыков.
- Клиент мета-мозга — `src/metabrain/client.py` (ошибки — те же классы, что у `Metabrain`).

Свой обработчик подключается так же — `DutyHandler.run(job, passport, steps) -> report`, шаги через `steps.run(name, fn)` (результат пишется в БД до/после, при рестарте не повторяется).
