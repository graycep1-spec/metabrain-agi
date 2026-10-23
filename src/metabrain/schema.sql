-- Мета-мозг (C-5, ADR-018). Каждая строка несёт company_id и harness_id вызывающего харнесса.

CREATE TABLE IF NOT EXISTS mb_skill_candidates (
    candidate_id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    artifact JSONB NOT NULL,
    artifact_hash TEXT NOT NULL,
    scorecard JSONB NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail')),
    published_skill_id UUID,
    published_version TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS mb_skills (
    skill_id UUID NOT NULL,
    version TEXT NOT NULL CHECK (version ~ '^\d+\.\d+\.\d+$'),
    major INT NOT NULL,
    minor INT NOT NULL,
    patch INT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('personal', 'company')),
    company_id UUID NOT NULL,
    owner_harness_id UUID,
    author_person_id TEXT,
    title TEXT NOT NULL,
    task_class TEXT NOT NULL,
    role_tags TEXT[] NOT NULL DEFAULT '{}',
    body TEXT NOT NULL,
    examples JSONB NOT NULL DEFAULT '[]'::jsonb,
    supersedes UUID,
    provenance JSONB NOT NULL,
    candidate_id UUID NOT NULL UNIQUE REFERENCES mb_skill_candidates (candidate_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    search TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('russian', title || ' ' || body) || to_tsvector('simple', title || ' ' || body)
    ) STORED,
    PRIMARY KEY (skill_id, version),
    CHECK ((scope = 'personal') = (owner_harness_id IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS mb_skills_company_idx ON mb_skills (company_id, scope, task_class);
CREATE INDEX IF NOT EXISTS mb_skills_owner_idx ON mb_skills (owner_harness_id) WHERE owner_harness_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS mb_skills_search_idx ON mb_skills USING GIN (search);

CREATE TABLE IF NOT EXISTS mb_used_consents (
    token_id UUID PRIMARY KEY,
    harness_id UUID NOT NULL,
    candidate_id UUID NOT NULL,
    used_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS mb_retrieval_docs (
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    doc_id TEXT NOT NULL CHECK (length(trim(doc_id)) > 0),
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'trajectory', 'card')),
    text TEXT NOT NULL,
    source JSONB,
    sensitivity JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    search TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('russian', text) || to_tsvector('simple', text)
    ) STORED,
    PRIMARY KEY (harness_id, doc_id)
);

CREATE INDEX IF NOT EXISTS mb_retrieval_scope_idx ON mb_retrieval_docs (company_id, harness_id, kind);
CREATE INDEX IF NOT EXISTS mb_retrieval_search_idx ON mb_retrieval_docs USING GIN (search);

CREATE TABLE IF NOT EXISTS mb_audit (
    id BIGSERIAL PRIMARY KEY,
    company_id UUID,
    harness_id UUID,
    operation TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE OR REPLACE FUNCTION mb_reject_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION '% is immutable', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

DROP TRIGGER IF EXISTS mb_skills_immutable ON mb_skills;
CREATE TRIGGER mb_skills_immutable
    BEFORE UPDATE OR DELETE ON mb_skills
    FOR EACH ROW EXECUTE FUNCTION mb_reject_mutation();

DROP TRIGGER IF EXISTS mb_skills_no_truncate ON mb_skills;
CREATE TRIGGER mb_skills_no_truncate
    BEFORE TRUNCATE ON mb_skills
    FOR EACH STATEMENT EXECUTE FUNCTION mb_reject_mutation();

DROP TRIGGER IF EXISTS mb_audit_append_only ON mb_audit;
CREATE TRIGGER mb_audit_append_only
    BEFORE UPDATE OR DELETE ON mb_audit
    FOR EACH ROW EXECUTE FUNCTION mb_reject_mutation();

DROP TRIGGER IF EXISTS mb_audit_no_truncate ON mb_audit;
CREATE TRIGGER mb_audit_no_truncate
    BEFORE TRUNCATE ON mb_audit
    FOR EACH STATEMENT EXECUTE FUNCTION mb_reject_mutation();
