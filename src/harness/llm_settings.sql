-- Настройки LLM, заданные в кабинете (одна строка на установку). Применяются при старте харнесса.
-- Ключ не храним здесь: только ссылка api_key_ref (vault:<id>), сам секрет — в harness_secrets.
CREATE TABLE IF NOT EXISTS llm_settings (
    id          smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    provider    text        NOT NULL,
    api_base    text        NOT NULL,
    model_main  text        NOT NULL,
    model_judge text,
    api_key_ref text,
    dlp_enabled boolean     NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  uuid
);
