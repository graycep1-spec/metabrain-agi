-- Профиль организации (ADR-035): одна строка на компанию; наследуется ролями и каталогом.
CREATE TABLE IF NOT EXISTS org_settings (
    company_id uuid        PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    industry   text        NOT NULL DEFAULT '',
    size       text        NOT NULL DEFAULT '',
    kpi        text        NOT NULL DEFAULT '',
    metabrain_enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);

-- Установки, созданные до ADR-035/ADR-036, получают колонку без потери профиля.
ALTER TABLE org_settings ADD COLUMN IF NOT EXISTS metabrain_enabled boolean NOT NULL DEFAULT true;
