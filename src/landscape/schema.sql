-- Подключения и адаптеры харнесса (C-3, ADR-023).

CREATE TABLE IF NOT EXISTS lc_connections (
    harness_id UUID NOT NULL,
    company_id UUID NOT NULL,
    system TEXT NOT NULL,
    secret_ref TEXT NOT NULL,
    proposal_id UUID NOT NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (harness_id, system),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS lc_adapters (
    adapter_id UUID PRIMARY KEY,
    harness_id UUID NOT NULL,
    company_id UUID NOT NULL,
    system TEXT NOT NULL,
    artifact JSONB NOT NULL,
    files_sha256 TEXT NOT NULL,
    scorecard JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('proposed', 'active', 'superseded', 'rejected')),
    proposal_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    activated_at TIMESTAMPTZ,
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS lc_adapters_one_active
    ON lc_adapters (harness_id, system) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS lc_adapters_proposal_idx ON lc_adapters (proposal_id);
