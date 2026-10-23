-- Цифровые сотрудники (C-6, ADR-024). Таблицы харнесса (harnesses) должны уже существовать.

CREATE TABLE IF NOT EXISTS wf_employees (
    employee_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('twin', 'new', 'researcher')),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    status TEXT NOT NULL CHECK (status IN ('draft', 'active', 'paused', 'retired')),
    active_version INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS wf_employees_one_researcher
    ON wf_employees (harness_id) WHERE kind = 'researcher';
CREATE INDEX IF NOT EXISTS wf_employees_harness_idx ON wf_employees (harness_id, created_at);

CREATE TABLE IF NOT EXISTS wf_passports (
    employee_id UUID NOT NULL REFERENCES wf_employees (employee_id) ON DELETE CASCADE,
    version INT NOT NULL CHECK (version >= 1),
    passport JSONB NOT NULL,
    artifact JSONB NOT NULL,
    scorecard JSONB NOT NULL,
    proposal_id UUID,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'superseded', 'returned')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (employee_id, version)
);

-- Версия паспорта неизменяема: меняется только статус.
CREATE OR REPLACE FUNCTION wf_reject_passport_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.passport IS DISTINCT FROM OLD.passport
       OR NEW.artifact IS DISTINCT FROM OLD.artifact
       OR NEW.scorecard IS DISTINCT FROM OLD.scorecard
       OR NEW.version IS DISTINCT FROM OLD.version
       OR NEW.employee_id IS DISTINCT FROM OLD.employee_id THEN
        RAISE EXCEPTION 'passport versions are immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS wf_passports_immutable ON wf_passports;
CREATE TRIGGER wf_passports_immutable
    BEFORE UPDATE ON wf_passports
    FOR EACH ROW EXECUTE FUNCTION wf_reject_passport_change();

CREATE TABLE IF NOT EXISTS wf_jobs (
    job_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    employee_id UUID NOT NULL REFERENCES wf_employees (employee_id) ON DELETE CASCADE,
    duty_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('schedule', 'event', 'ask')),
    slot TIMESTAMPTZ,
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'waiting_curator', 'done', 'failed')),
    lease_until TIMESTAMPTZ,
    attempts INT NOT NULL DEFAULT 0,
    report JSONB,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

-- Один слот расписания — одна работа; одна незавершённая работа на обязанность (C-6).
CREATE UNIQUE INDEX IF NOT EXISTS wf_jobs_schedule_slot
    ON wf_jobs (employee_id, duty_id, slot) WHERE trigger = 'schedule';
CREATE UNIQUE INDEX IF NOT EXISTS wf_jobs_one_open_per_duty
    ON wf_jobs (employee_id, duty_id) WHERE status IN ('queued', 'running', 'waiting_curator');
CREATE INDEX IF NOT EXISTS wf_jobs_claim_idx ON wf_jobs (status, created_at);

CREATE TABLE IF NOT EXISTS wf_job_steps (
    job_id UUID NOT NULL REFERENCES wf_jobs (job_id) ON DELETE CASCADE,
    step_no INT NOT NULL,
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('started', 'done')),
    idempotency_key TEXT NOT NULL,
    result JSONB,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, step_no)
);

-- Процессы и проекты (C-6, планирование). Всё принадлежит харнессу: чужие данные не видны.
CREATE TABLE IF NOT EXISTS wf_processes (
    process_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    purpose TEXT NOT NULL DEFAULT '',
    inputs JSONB NOT NULL DEFAULT '[]'::jsonb,
    outputs JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL CHECK (status IN ('active', 'paused', 'archived')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wf_processes_harness_idx ON wf_processes (harness_id, created_at);

CREATE TABLE IF NOT EXISTS wf_projects (
    project_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    goal TEXT NOT NULL DEFAULT '',
    starts_on DATE,
    ends_on DATE,
    budget_usd NUMERIC(12, 2) NOT NULL DEFAULT 0 CHECK (budget_usd >= 0),
    status TEXT NOT NULL CHECK (status IN ('planned', 'active', 'paused', 'done')),
    result TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (ends_on IS NULL OR starts_on IS NULL OR ends_on >= starts_on),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wf_projects_harness_idx ON wf_projects (harness_id, created_at);

CREATE TABLE IF NOT EXISTS wf_assignments (
    assignment_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    employee_id UUID NOT NULL REFERENCES wf_employees (employee_id) ON DELETE CASCADE,
    target_type TEXT NOT NULL CHECK (target_type IN ('process', 'project')),
    target_id UUID NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (employee_id, target_type, target_id),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wf_assignments_target_idx ON wf_assignments (harness_id, target_type, target_id);
CREATE INDEX IF NOT EXISTS wf_assignments_employee_idx ON wf_assignments (employee_id, created_at);

CREATE TABLE IF NOT EXISTS wf_tasks (
    task_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    assignment_id UUID NOT NULL REFERENCES wf_assignments (assignment_id) ON DELETE CASCADE,
    employee_id UUID NOT NULL REFERENCES wf_employees (employee_id) ON DELETE CASCADE,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK (status IN ('todo', 'doing', 'done', 'blocked')),
    due_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS wf_tasks_assignment_idx ON wf_tasks (assignment_id, created_at);
CREATE INDEX IF NOT EXISTS wf_tasks_harness_idx ON wf_tasks (harness_id, status, created_at);
