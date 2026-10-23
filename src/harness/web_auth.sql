-- Вход в веб-кабинет (C-1, «Вход в веб»; ADR-025). Хранятся только хэши токенов.

CREATE TABLE IF NOT EXISTS web_login_states (
    state_hash TEXT PRIMARY KEY CHECK (length(state_hash) = 64),
    code_verifier TEXT NOT NULL,
    nonce TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS web_login_states_exp_idx ON web_login_states (exp);

CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash TEXT PRIMARY KEY CHECK (length(token_hash) = 64),
    csrf_hash TEXT NOT NULL CHECK (length(csrf_hash) = 64),
    person_id UUID NOT NULL REFERENCES people (id) ON DELETE CASCADE,
    company_id UUID NOT NULL,
    harness_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY (harness_id, company_id) REFERENCES harnesses (id, company_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS web_sessions_person_idx ON web_sessions (person_id, exp);

-- Тикет саморегистрации (ADR-030): Telegram-личность подтверждена, организации ещё нет.
CREATE TABLE IF NOT EXISTS web_bootstrap_tickets (
    ticket_hash TEXT PRIMARY KEY CHECK (length(ticket_hash) = 64),
    telegram_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL,
    exp TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS web_bootstrap_tickets_exp_idx ON web_bootstrap_tickets (exp);

