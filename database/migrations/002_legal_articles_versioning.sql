-- 002_legal_articles_versioning.sql: Add versioning and atomic staging support to legal_articles

ALTER TABLE legal_articles
    ADD COLUMN IF NOT EXISTS version INT DEFAULT 1,
    ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE,
    ADD COLUMN IF NOT EXISTS ingested_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP;

-- Ensure all existing rows have valid defaults
UPDATE legal_articles
SET version = 1, is_active = TRUE
WHERE version IS NULL OR is_active IS NULL;

-- High performance partial indices for active legal articles
CREATE INDEX IF NOT EXISTS idx_legal_articles_active
ON legal_articles (is_active)
WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_legal_articles_version_active
ON legal_articles (version, is_active);
