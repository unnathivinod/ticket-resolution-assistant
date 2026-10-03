-- Changes needed for new data and new ticket classes.
-- Every statement is safe to run again, so this file can be applied to an existing database:
--   docker compose run --rm tools python scripts/migrate.py

-- When was this document last written into the search index?
-- NULL (or older than updated_at) means "the index is behind", and the ingestion worker fixes it.
-- Rows that already exist were indexed by scripts/seed.py, so they start as "indexed now".
ALTER TABLE tickets     ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE tickets     ALTER COLUMN indexed_at DROP DEFAULT;
ALTER TABLE kb_articles ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE kb_articles ALTER COLUMN indexed_at DROP DEFAULT;

-- The agent can say which category was right, or that none of the existing ones fits.
ALTER TABLE feedback ADD COLUMN IF NOT EXISTS correct_category TEXT;

-- A proposal now remembers which complaints it was built from, with a few examples to read.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'class_proposals' AND column_name = 'example_ticket_ids'
    ) THEN
        ALTER TABLE class_proposals RENAME COLUMN example_ticket_ids TO request_ids;
    END IF;
END $$;
ALTER TABLE class_proposals ADD COLUMN IF NOT EXISTS examples      JSONB NOT NULL DEFAULT '[]';
ALTER TABLE class_proposals ADD COLUMN IF NOT EXISTS keywords      TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE class_proposals ADD COLUMN IF NOT EXISTS approved_name TEXT;
