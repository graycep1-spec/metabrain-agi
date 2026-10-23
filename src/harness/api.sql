CREATE TABLE IF NOT EXISTS company_invites (
    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    created_by UUID NOT NULL REFERENCES people(id),
    created_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS company_invites_exp_idx
    ON company_invites (exp);

CREATE TABLE IF NOT EXISTS external_llm_consents (
    person_id UUID PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    consented_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connect_requests (
    id UUID PRIMARY KEY,
    harness_id UUID NOT NULL REFERENCES harnesses(id) ON DELETE CASCADE,
    system TEXT NOT NULL CHECK (length(trim(system)) > 0),
    created_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS connect_requests_scope_idx
    ON connect_requests (harness_id, exp);
