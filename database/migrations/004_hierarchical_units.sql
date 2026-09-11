-- 004_hierarchical_units.sql: Kanoniczne jednostki prawne, unikalny unit_id, normalizacja OP i indeksy

-- 1. Dodanie kolumn hierarchii prawnej jako nullable
ALTER TABLE legal_articles
    ADD COLUMN IF NOT EXISTS paragraph VARCHAR(16),
    ADD COLUMN IF NOT EXISTS point VARCHAR(16),
    ADD COLUMN IF NOT EXISTS letter VARCHAR(16),
    ADD COLUMN IF NOT EXISTS unit_id TEXT,
    ADD COLUMN IF NOT EXISTS unit_type VARCHAR(16) DEFAULT 'article',
    ADD COLUMN IF NOT EXISTS parent_unit_id TEXT;

-- 2. Normalizacja kodu aktu prawnego OP (zamiast ORDYNACJA) we wszystkich tabelach
-- 2a. Rozwiązanie ewentualnych konfliktów PK w ingestion_run_acts
DELETE FROM ingestion_run_acts ira_old
WHERE ira_old.act_code = 'ORDYNACJA'
  AND EXISTS (
      SELECT 1 FROM ingestion_run_acts ira_new
      WHERE ira_new.ingestion_run_id = ira_old.ingestion_run_id
        AND ira_new.act_code = 'OP'
  );

UPDATE ingestion_run_acts SET act_code = 'OP' WHERE act_code = 'ORDYNACJA';

-- 2b. Rejestracja kanonicznego OP w legal_acts i usunięcie aliasu
INSERT INTO legal_acts (act_code, act_title)
VALUES ('OP', 'Ordynacja podatkowa')
ON CONFLICT (act_code) DO NOTHING;

UPDATE legal_articles SET act_code = 'OP' WHERE act_code = 'ORDYNACJA';

DELETE FROM legal_acts WHERE act_code = 'ORDYNACJA';

-- 3. Inteligentny backfill starych złożonych sygnatur do czystych pól i kanonicznego unit_id
DO $$
DECLARE
    r RECORD;
    v_art TEXT;
    v_par TEXT;
    v_pt TEXT;
    v_unit_id TEXT;
    v_type TEXT;
    m TEXT[];
BEGIN
    FOR r IN SELECT id, act_code, article_number FROM legal_articles WHERE unit_id IS NULL LOOP
        v_art := r.article_number;
        v_par := '-';
        v_pt := '-';
        v_type := 'article';

        -- Wzorzec 1: "23 ust. 1 pkt 4"
        m := regexp_matches(r.article_number, '^([0-9]+[a-z]?)\s+ust\.\s*([0-9]+[a-z]?)\s+pkt\s*([0-9]+[a-z]?)$', 'i');
        IF m IS NOT NULL THEN
            v_art := m[1];
            v_par := m[2];
            v_pt := m[3];
            v_type := 'point';
        ELSE
            -- Wzorzec 2: "23 ust. 1"
            m := regexp_matches(r.article_number, '^([0-9]+[a-z]?)\s+ust\.\s*([0-9]+[a-z]?)$', 'i');
            IF m IS NOT NULL THEN
                v_art := m[1];
                v_par := m[2];
                v_type := 'paragraph';
            ELSE
                -- Wzorzec 3: sam artykuł "23" lub "108a"
                m := regexp_matches(r.article_number, '^([0-9]+[a-z]?)$', 'i');
                IF m IS NOT NULL THEN
                    v_art := m[1];
                    v_type := 'article';
                END IF;
            END IF;
        END IF;

        v_unit_id := r.act_code || ':art=' || lower(v_art) || ':par=' || lower(v_par) || ':point=' || lower(v_pt) || ':letter=-';

        UPDATE legal_articles
        SET article_number = v_art,
            paragraph = CASE WHEN v_par = '-' THEN NULL ELSE v_par END,
            point = CASE WHEN v_pt = '-' THEN NULL ELSE v_pt END,
            letter = NULL,
            unit_id = v_unit_id,
            unit_type = v_type
        WHERE id = r.id;
    END LOOP;
END $$;

-- 4. Rozwiązanie konfliktów aktywnych jednostek:
-- Bezpieczna deduplikacja aktywnych rekordów (zachowanie pełnej historii wersji bez DELETE).
-- Jeżeli dla danego unit_id istnieje wiele aktywnych rekordów (np. z różnych przebiegów ingestu),
-- zachowujemy wyłącznie najnowszą aktywną wersję, a starsze nadmiarowe rekordy dezaktywujemy (is_active = FALSE).
WITH ranked_active AS (
    SELECT id,
           ROW_NUMBER() OVER (
               PARTITION BY unit_id
               ORDER BY version DESC, ingested_at DESC, id ASC
           ) AS rn
    FROM legal_articles
    WHERE is_active = TRUE
)
UPDATE legal_articles la
SET is_active = FALSE
FROM ranked_active ra
WHERE la.id = ra.id AND ra.rn > 1;

-- 5. Kontrola integralności przed utworzeniem unikalnego indeksu aktywnych jednostek.
-- Weryfikacja braku duplikatów:
-- SELECT unit_id, COUNT(*) FROM legal_articles WHERE is_active = TRUE GROUP BY unit_id HAVING COUNT(*) > 1;
-- W przypadku wykrycia jakichkolwiek niejednoznacznych duplikatów wśród aktywnych rekordów migracja zostaje przerwana.
DO $$
DECLARE
    v_dup_count INT;
BEGIN
    SELECT COUNT(*) INTO v_dup_count
    FROM (
        SELECT unit_id, COUNT(*)
        FROM legal_articles
        WHERE is_active = TRUE
        GROUP BY unit_id
        HAVING COUNT(*) > 1
    ) sub;

    IF v_dup_count > 0 THEN
        RAISE EXCEPTION 'Niejednoznaczny konflikt: wykryto % zduplikowanych unit_id wśród aktywnych rekordów. Przerywanie migracji 004.', v_dup_count;
    END IF;
END $$;

-- 6. Nałożenie ograniczenia NOT NULL oraz indeksów wydajnościowych
ALTER TABLE legal_articles ALTER COLUMN unit_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_legal_articles_active_unit_id
ON legal_articles (unit_id) WHERE is_active = TRUE;

CREATE INDEX IF NOT EXISTS idx_legal_articles_run_unit
ON legal_articles (ingestion_run_id, unit_id);

CREATE INDEX IF NOT EXISTS idx_legal_articles_hierarchical
ON legal_articles (act_code, article_number, paragraph, point, letter);

CREATE INDEX IF NOT EXISTS idx_legal_articles_unit_lookup
ON legal_articles (unit_id);

CREATE INDEX IF NOT EXISTS idx_legal_articles_act_active
ON legal_articles (act_code, is_active) WHERE is_active = TRUE;
