-- Database schema. Postgres runs this file once, when the database is first created.
-- Postgres is the source of truth; the Qdrant index can always be rebuilt from it.

-- The list of ticket classes. Classes are ROWS here, not hard-coded in the program,
-- so a new class can be added without changing any code.
CREATE TABLE IF NOT EXISTS taxonomy (
    id          SERIAL PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('category', 'product')),
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'proposed', 'retired')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, name)
);

-- Past support tickets, with the steps that resolved them.
CREATE TABLE IF NOT EXISTS tickets (
    id               TEXT PRIMARY KEY,                 -- e.g. T-000123
    subject          TEXT NOT NULL,
    description      TEXT NOT NULL,                    -- the customer's complaint
    resolution_steps JSONB NOT NULL DEFAULT '[]',      -- ordered list of steps
    category         TEXT,
    product          TEXT,
    severity         TEXT CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    sentiment        TEXT CHECK (sentiment IN ('negative', 'neutral', 'positive')),
    scenario_id      TEXT,                             -- answer key used by the evals
    is_active        BOOLEAN NOT NULL DEFAULT TRUE,    -- FALSE hides outdated fixes from search
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_tickets_category ON tickets (category);
CREATE INDEX IF NOT EXISTS idx_tickets_product  ON tickets (product);
CREATE INDEX IF NOT EXISTS idx_tickets_created  ON tickets (created_at);

-- Knowledge-base articles.
CREATE TABLE IF NOT EXISTS kb_articles (
    id          TEXT PRIMARY KEY,                      -- e.g. KB-014
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    category    TEXT,
    product     TEXT,
    scenario_id TEXT,
    version     INTEGER NOT NULL DEFAULT 1,            -- goes up each time the article is edited
    is_active   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit log: one row per /v1/resolve request, so every answer can be traced later.
CREATE TABLE IF NOT EXISTS resolve_requests (
    request_id       UUID PRIMARY KEY,
    complaint_masked TEXT NOT NULL,                    -- personal data already removed
    triage           JSONB,
    source_ids       TEXT[] NOT NULL DEFAULT '{}',
    resolution       JSONB,
    grounded         BOOLEAN,
    escalated        BOOLEAN,
    latency_ms       JSONB,
    llm_model        TEXT,
    prompt_version   TEXT,
    index_version    TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_requests_created ON resolve_requests (created_at);

-- Thumbs up / down from the support agent.
CREATE TABLE IF NOT EXISTS feedback (
    id                BIGSERIAL PRIMARY KEY,
    request_id        UUID NOT NULL REFERENCES resolve_requests (request_id) ON DELETE CASCADE,
    helpful           BOOLEAN NOT NULL,
    comment           TEXT,
    edited_resolution TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- New ticket classes suggested by the discovery job, waiting for a human to approve.
CREATE TABLE IF NOT EXISTS class_proposals (
    id                 SERIAL PRIMARY KEY,
    kind               TEXT NOT NULL CHECK (kind IN ('category', 'product')),
    suggested_name     TEXT NOT NULL,
    example_ticket_ids TEXT[] NOT NULL DEFAULT '{}',
    cluster_size       INTEGER NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at         TIMESTAMPTZ
);
