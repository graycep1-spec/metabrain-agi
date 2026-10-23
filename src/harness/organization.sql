-- Профиль организации (ADR-035): одна строка на компанию; наследуется ролями и каталогом.
CREATE TABLE IF NOT EXISTS org_settings (
    company_id uuid        PRIMARY KEY REFERENCES companies(id) ON DELETE CASCADE,
    industry   text        NOT NULL DEFAULT '',
    size       text        NOT NULL DEFAULT '',
    kpi        text        NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by uuid
);
