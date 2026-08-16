-- Skrypt Inicjalizacyjny Bazy Danych PostgreSQL z pgvector dla Systemu KARIK

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Segment 1: Prawo Podatkowe (Ustawy i Akty Prawne)
CREATE TABLE IF NOT EXISTS legal_articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    act_code VARCHAR(32) NOT NULL,              -- 'PIT', 'CIT', 'VAT', 'ORDYNACJA', 'UOR', 'ZUS'
    act_title TEXT NOT NULL,                    -- np. 'Ustawa o podatku dochodowym od osób fizycznych'
    chapter TEXT,                               -- np. 'Rozdział 4 - Koszty uzyskania przychodów'
    article_number VARCHAR(32) NOT NULL,        -- np. '22', '22a'
    full_title TEXT,                            -- Nagłówek/Tytuł artykułu jeśli występuje
    content TEXT NOT NULL,                      -- Pełna treść artykułu ze wszystkimi ustępami i punktami
    clean_text TEXT NOT NULL,                   -- Oczyszczona treść bez znaczników HTML
    embedding vector(768),                      -- Wektor 768D dla modelu sdadas/mmlu-polish-e5-base
    search_vector tsvector,                     -- Full-Text Search w PostgreSQL dla języka polskiego
    valid_from DATE DEFAULT '1991-01-01',
    valid_to DATE DEFAULT NULL,
    status VARCHAR(20) DEFAULT 'OBOWIĄZUJĄCY',
    year_effective INT DEFAULT 2026,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indeks Wektorowy HNSW (Podobieństwo Kosinusowe)
CREATE INDEX IF NOT EXISTS idx_legal_articles_embedding 
ON legal_articles USING hnsw (embedding vector_cosine_ops)
WITH (m = 16, ef_construction = 64);

-- Indeks FTS (Full Text Search)
CREATE INDEX IF NOT EXISTS idx_legal_articles_fts 
ON legal_articles USING gin (search_vector);

-- Indeksy Filtrujące
CREATE INDEX IF NOT EXISTS idx_legal_articles_lookup 
ON legal_articles (act_code, article_number);

-- Segment 2: Multi-Tenant Baza Klienta (Dane Transakcyjne / Księgowe)
CREATE TABLE IF NOT EXISTS client_documents (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id UUID NOT NULL,                    -- Identyfikator klienta biura rachunkowego
    document_type VARCHAR(64) NOT NULL,         -- np. 'KPiR', 'Faktura', 'Interpretacja_Indywidualna'
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding vector(768),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_client_docs_tenant ON client_documents (tenant_id);
CREATE INDEX IF NOT EXISTS idx_client_docs_embedding ON client_documents USING hnsw (embedding vector_cosine_ops);
