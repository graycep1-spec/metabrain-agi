CREATE TABLE IF NOT EXISTS proposals (
    id UUID PRIMARY KEY,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'passport',
            'recommendation',
            'action',
            'adapter',
            'connect',
            'skill_publish',
            'autonomy_change'
        )
    ),
    summary TEXT NOT NULL CHECK (length(trim(summary)) > 0),
    subject JSONB NOT NULL,
    sources JSONB NOT NULL DEFAULT '[]'::jsonb,
    score_ref TEXT,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (
        status IN ('pending', 'approved', 'returned', 'incident')
    ),
    decision_comment TEXT,
    decided_by UUID REFERENCES people(id),
    decided_surface TEXT CHECK (decided_surface IN ('web', 'telegram')),
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (id, harness_id),
    FOREIGN KEY (harness_id, company_id)
        REFERENCES harnesses(id, company_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS proposals_scope_idx
    ON proposals (company_id, harness_id, status, created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS consent_tokens (
    token_id UUID PRIMARY KEY,
    harness_id UUID NOT NULL,
    proposal_id UUID NOT NULL,
    subject_hash TEXT NOT NULL CHECK (length(subject_hash) = 64),
    scope TEXT NOT NULL,
    issued_by UUID NOT NULL REFERENCES people(id),
    surface TEXT NOT NULL CHECK (surface IN ('web', 'telegram')),
    issued_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL,
    signature TEXT NOT NULL,
    used_at TIMESTAMPTZ,
    UNIQUE (proposal_id),
    FOREIGN KEY (proposal_id, harness_id)
        REFERENCES proposals(id, harness_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS consent_tokens_proposal_idx
    ON consent_tokens (proposal_id, harness_id);
