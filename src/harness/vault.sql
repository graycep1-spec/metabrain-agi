-- Хранилище секретов харнесса (ADR-028): доступы к системам, введённые в кабинете. Только шифротекст.

CREATE TABLE IF NOT EXISTS harness_secrets (
    secret_id UUID PRIMARY KEY,
    harness_id UUID NOT NULL,
    company_id UUID NOT NULL,
    system TEXT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    created_by UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS harness_secrets_harness_idx ON harness_secrets (harness_id, system);
