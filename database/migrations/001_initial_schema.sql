-- 001_initial_schema.sql: Baseline schema for KARIK RAG and legal articles

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE TABLE IF NOT EXISTS legal_articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    act_code VARCHAR(32) NOT NULL,
    act_title TEXT NOT NULL,
    chapter TEXT,
    article_number VARCHAR(32) NOT NULL,
    full_title TEXT,
    content TEXT NOT NULL,
    clean_text TEXT NOT NULL,
    embedding vector(768),
    search_vector tsvector,
    valid_from DATE DEFAULT '1991-01-01',
    valid_to DATE DEFAULT NULL,
    status VARCHAR(20) DEFAULT 'OBOWIĄZUJĄCY',
    year_effective INT DEFAULT 2026,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_legal_articles_embedding 
ON legal_articles USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_legal_articles_fts 
ON legal_articles USING gin (search_vector);

CREATE INDEX IF NOT EXISTS idx_legal_articles_lookup 
ON legal_articles (act_code, article_number);
