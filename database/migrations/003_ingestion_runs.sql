-- 003_ingestion_runs.sql: Ingestion runs tracking, per-act lifecycle, chunk metadata, and legal acts
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. Tabela rejestrująca akty prawne
CREATE TABLE IF NOT EXISTS legal_acts (
    act_code VARCHAR(32) PRIMARY KEY,
    act_title TEXT NOT NULL,
    promulgation_date DATE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 2. Tabela przebiegów importu (cykl życia: STARTED -> COMPLETED / FAILED)
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id UUID PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    source_checksum VARCHAR(64),
    source_file TEXT,
    total_articles INT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP WITH TIME ZONE,
    error_code VARCHAR(64),
    error_message TEXT
);

-- 3. Tabela statusów i wersji per akt w ramach przebiegu
CREATE TABLE IF NOT EXISTS ingestion_run_acts (
    ingestion_run_id UUID NOT NULL REFERENCES ingestion_runs(id) ON DELETE CASCADE,
    act_code VARCHAR(32) NOT NULL,
    snapshot_version BIGINT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    activated_at TIMESTAMP WITH TIME ZONE,
    superseded_at TIMESTAMP WITH TIME ZONE,
    PRIMARY KEY (ingestion_run_id, act_code)
);

CREATE INDEX IF NOT EXISTS idx_ingestion_run_acts_active
ON ingestion_run_acts (act_code, is_active) WHERE is_active = TRUE;

-- 4. Tabela audytu zapytań RAG
CREATE TABLE IF NOT EXISTS rag_audit_log (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query_text TEXT NOT NULL,
    retrieved_article_ids JSONB,
    latency_ms FLOAT,
    timestamp TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 5. Rozszerzenie tabeli legal_articles o metadane fragmentów i powiązanie z runem
ALTER TABLE legal_articles
    ADD COLUMN IF NOT EXISTS ingestion_run_id UUID REFERENCES ingestion_runs(id),
    ADD COLUMN IF NOT EXISTS chunk_id VARCHAR(64),
    ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64);

CREATE UNIQUE INDEX IF NOT EXISTS idx_legal_articles_run_chunk
ON legal_articles (ingestion_run_id, chunk_id);

CREATE INDEX IF NOT EXISTS idx_legal_articles_chunk_content
ON legal_articles (chunk_id, content_hash);
