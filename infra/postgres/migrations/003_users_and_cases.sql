-- Sign-in accounts, and what the agent decided for each complaint.
-- Every statement is safe to run again, so this file can be applied to an existing database:
--   docker compose run --rm tools python scripts/migrate.py

-- The people who may sign in. The password itself is never stored, only a salted PBKDF2 hash
-- (see services/gateway/auth.py). The role decides what a person sees:
--   agent     resolves complaints and sees only the cases they handled themselves
--   expert    second-line support: also records fixes and sees every case
--   engineer  also gets the monitoring link and sees every case
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    display_name  TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('agent', 'expert', 'engineer')),
    password_hash TEXT NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Three demo accounts, so the project can be tried straight after it starts. They share one
-- password, which is public (it is in the README): demo1234
-- Each account still has its own salt, so the three stored hashes are different.
-- Before real use, switch them off (UPDATE users SET is_active = FALSE) and add real accounts
-- with: docker compose run --rm tools python scripts/add_user.py
INSERT INTO users (username, display_name, role, password_hash) VALUES
    ('priya', 'Priya S', 'agent',
     'pbkdf2_sha256$200000$d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4$71163aa761737d18d892be3141ea8a943d8b33e767349e9fc9141c5b95648328'),
    ('arun', 'Arun K', 'expert',
     'pbkdf2_sha256$200000$e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5e5$d973c068d7dc0f62833faa43676c0914ccf952d942866e3a741242144a084bfb'),
    ('meera', 'Meera R', 'engineer',
     'pbkdf2_sha256$200000$f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6f6$41cd6d7ad307701ecbd2a2cd0057739a9ca8fd6ea31bdf9470793d6a3f404a94')
-- An account that already exists is left alone, with one exception: while this was being built
-- each demo account had its own password. A database from then still holds one of those three
-- first hashes (recognised by its salt) and gets the shared password here. A password that
-- somebody set with add_user.py has a random salt and is never touched.
ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
    WHERE split_part(users.password_hash, '$', 3) IN (
        'a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1a1', 'b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2', 'c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3');

-- Who pressed Resolve. NULL for requests that came without a signed-in person (scripts, evals).
ALTER TABLE resolve_requests ADD COLUMN IF NOT EXISTS handled_by TEXT;
-- Was this complaint part of a possible service incident when it arrived?
ALTER TABLE resolve_requests ADD COLUMN IF NOT EXISTS incident BOOLEAN;
-- How the case ended, as recorded by a person. NULL means no decision yet ("open").
ALTER TABLE resolve_requests ADD COLUMN IF NOT EXISTS decision TEXT
    CHECK (decision IN ('resolved', 'escalated'));
ALTER TABLE resolve_requests ADD COLUMN IF NOT EXISTS decided_by TEXT;
ALTER TABLE resolve_requests ADD COLUMN IF NOT EXISTS decided_at TIMESTAMPTZ;

-- "My cases, newest first" is the query the Cases page runs most.
CREATE INDEX IF NOT EXISTS idx_requests_handled_by ON resolve_requests (handled_by, created_at DESC);
