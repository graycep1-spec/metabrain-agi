CREATE TABLE IF NOT EXISTS companies (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS people (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, company_id)
);

CREATE TABLE IF NOT EXISTS channel_identities (
    channel TEXT NOT NULL CHECK (channel = 'telegram'),
    channel_user_id TEXT NOT NULL CHECK (length(trim(channel_user_id)) > 0),
    person_id UUID NOT NULL REFERENCES people(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (channel, channel_user_id),
    UNIQUE (channel, person_id)
);

CREATE TABLE IF NOT EXISTS harnesses (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL REFERENCES companies(id),
    owner_person_id UUID NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, company_id),
    FOREIGN KEY (owner_person_id, company_id)
        REFERENCES people(id, company_id)
);

CREATE TABLE IF NOT EXISTS harness_records (
    id UUID NOT NULL,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (harness_id, id),
    FOREIGN KEY (harness_id, company_id)
        REFERENCES harnesses(id, company_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS harness_records_scope_idx
    ON harness_records (company_id, harness_id, kind, created_at, id);

CREATE TABLE IF NOT EXISTS llm_usage (
    id UUID PRIMARY KEY,
    harness_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_tokens BIGINT NOT NULL CHECK (prompt_tokens >= 0),
    completion_tokens BIGINT NOT NULL CHECK (completion_tokens >= 0),
    total_tokens BIGINT NOT NULL CHECK (total_tokens >= 0),
    cost_usd NUMERIC(20, 9) NOT NULL CHECK (cost_usd >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS llm_usage_installation_idx
    ON llm_usage (created_at);

CREATE INDEX IF NOT EXISTS llm_usage_harness_idx
    ON llm_usage (harness_id, created_at);

CREATE INDEX IF NOT EXISTS llm_usage_employee_idx
    ON llm_usage (harness_id, employee_id, created_at);

CREATE TABLE IF NOT EXISTS llm_budget_reservations (
    id UUID PRIMARY KEY,
    harness_id TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    estimated_cost_usd NUMERIC(20, 9) NOT NULL CHECK (estimated_cost_usd >= 0),
    estimated_tokens BIGINT NOT NULL CHECK (estimated_tokens >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp() + INTERVAL '1 hour'
);

CREATE INDEX IF NOT EXISTS llm_budget_reservations_expiry_idx
    ON llm_budget_reservations (expires_at);

CREATE INDEX IF NOT EXISTS llm_budget_reservations_scope_idx
    ON llm_budget_reservations (harness_id, employee_id, created_at);
