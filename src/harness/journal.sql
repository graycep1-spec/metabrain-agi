CREATE TABLE IF NOT EXISTS journal_entries (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    employee_id UUID,
    kind TEXT NOT NULL CHECK (length(trim(kind)) > 0),
    payload JSONB NOT NULL,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    score_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (harness_id, company_id)
        REFERENCES harnesses(id, company_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS journal_entries_scope_idx
    ON journal_entries (company_id, harness_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS journal_entries_employee_idx
    ON journal_entries (harness_id, employee_id, created_at DESC, id DESC);

CREATE OR REPLACE FUNCTION reject_journal_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'journal is append-only'
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS journal_entries_no_update_delete ON journal_entries;
CREATE TRIGGER journal_entries_no_update_delete
    BEFORE UPDATE OR DELETE ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION reject_journal_mutation();

DROP TRIGGER IF EXISTS journal_entries_no_truncate ON journal_entries;
CREATE TRIGGER journal_entries_no_truncate
    BEFORE TRUNCATE ON journal_entries
    FOR EACH STATEMENT EXECUTE FUNCTION reject_journal_mutation();
