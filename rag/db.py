import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional, Callable
import socket
import uuid
import hashlib
import re

logger = logging.getLogger(__name__)


from database.config import PostgresConfig, load_env_file, mask_password_in_url
from database.exceptions import DatabaseConfigurationError, DatabaseConnectionError, IngestionValidationError

load_env_file()

_cached_conn_params = None

def get_db_connection():
    """Nawiązuje połączenie z bazą PostgreSQL pgvector na podstawie PostgresConfig."""
    global _cached_conn_params

    # 1. Szybkie połączenie na zapamiętanych, zweryfikowanych parametrach
    if _cached_conn_params is not None:
        try:
            return psycopg2.connect(**_cached_conn_params, client_encoding="utf8", connect_timeout=3)
        except Exception:
            _cached_conn_params = None  # W razie utraty parametrów, reset i ponowna detekcja

    config = PostgresConfig.from_env(require_password=True)

    try:
        conn = psycopg2.connect(
            host=config.host,
            port=config.port,
            dbname=config.dbname,
            user=config.user,
            password=config.password,
            sslmode=config.sslmode,
            client_encoding="utf8",
            connect_timeout=config.connect_timeout,
        )
        _cached_conn_params = {
            "host": config.host,
            "port": config.port,
            "dbname": config.dbname,
            "user": config.user,
            "password": config.password,
            "sslmode": config.sslmode,
        }
        return conn
    except Exception as e:
        err_msg = str(e)
        if isinstance(e, UnicodeDecodeError) or "utf-8" in err_msg.lower():
            raw_bytes = getattr(e, "object", None)
            if isinstance(raw_bytes, bytes):
                decoded_msg = raw_bytes.decode("cp1250", errors="replace")
            else:
                decoded_msg = f"Błąd autoryzacji lub połączenia z bazą PostgreSQL na porcie {config.port}."
            raise DatabaseConnectionError(
                f"Błąd połączenia z PostgreSQL ({config.host}:{config.port}/{config.dbname}): {decoded_msg}"
            ) from e

        # Maskowanie hasła w treści błędu
        if config.password and config.password in err_msg:
            err_msg = err_msg.replace(config.password, "***")
        err_msg = mask_password_in_url(err_msg)
        raise DatabaseConnectionError(
            f"Błąd połączenia z PostgreSQL ({config.host}:{config.port}/{config.dbname}): {err_msg}"
        ) from e



def init_database():
    """Inicjalizuje rozszerzenie pgvector oraz aplikuje migracje schematu tabel."""
    conn = get_db_connection()
    try:
        from database.migrations import apply_migrations
        apply_migrations(conn)
    finally:
        conn.close()


PARSER_SCHEMA_VERSION = "v1"


def compute_chunk_id(act_code: str, article_number: str, paragraph: Optional[str] = None, point: Optional[str] = None, sub_role: str = "main") -> str:
    """Tworzy stabilny identyfikator fragmentu niezależny od zmian treści."""
    raw_key = f"{PARSER_SCHEMA_VERSION}:{act_code}:{article_number}:{paragraph or ''}:{point or ''}:{sub_role}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def compute_content_hash(content: str) -> str:
    """Tworzy sumę SHA-256 znormalizowanej treści artykułu."""
    normalized = re.sub(r"\s+", " ", content).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def sanitize_error_message(exc: Exception, password: Optional[str] = None) -> tuple:
    """Usuwa hasła, poświadczenia i DSN z komunikatu błędu przed zapisem audytowym."""
    error_code = type(exc).__name__
    msg = str(exc)
    if password and password in msg:
        msg = msg.replace(password, "***")
    msg = mask_password_in_url(msg)
    return error_code, msg[:1000]


def ingest_articles_staged(
    articles: List[Dict[str, Any]],
    source_file: Optional[str] = None,
    source_checksum: Optional[str] = None,
    validate: bool = True,
    before_activation: Optional[Callable[[], None]] = None
) -> Dict[str, Any]:
    """
    Transakcyjny ingest aktów prawnych ze stagingiem per-akt, deterministyczną blokadą i wersjonowaniem:
    1. Rejestruje przebieg w tabeli ingestion_runs w stanie 'STARTED' i natychmiast zatwierdza transakcję.
    2. Pobiera deterministyczne 64-bitowe blokady advisory lock w kolejności alfabetycznej aktów.
    3. Wstawia artykuły w trybie stagingu (is_active = FALSE, przypisane do ingestion_run_id).
    4. Przeprowadza rygorystyczną walidację (wymiar wektorów 768, brak duplikatów chunk_id, brak pustych treści).
    5. Atomowo dezaktywuje wyłącznie poprzednie wersje aktów z importowanej paczki i aktywuje nowy snapshot.
    6. W razie błędu następuje transakcyjny ROLLBACK, a w osobnym połączeniu zapis statusu 'FAILED' z oczyszczonym błędem.
    """
    if not articles:
        raise ValueError("Lista artykułów do ingestu nie może być pusta.")

    act_codes = sorted(list(set(art["act_code"] for art in articles if art.get("act_code"))))
    if not act_codes:
        raise ValueError("Każdy artykuł musi posiadać zdefiniowany 'act_code'.")

    run_id = uuid.uuid4()

    # KROK 1: Rejestracja runu w stanie STARTED z osobnym COMMIT (dla śladu audytowego przy awarii)
    conn_start = get_db_connection()
    try:
        with conn_start.cursor() as cur:
            cur.execute("""
                INSERT INTO ingestion_runs (id, status, source_checksum, source_file, total_articles, created_at)
                VALUES (%s, 'STARTED', %s, %s, %s, CURRENT_TIMESTAMP);
            """, (str(run_id), source_checksum, source_file, len(articles)))
        conn_start.commit()
    finally:
        conn_start.close()

    password_to_mask = None
    try:
        cfg = PostgresConfig.from_env(require_password=False)
        password_to_mask = cfg.password
    except Exception:
        pass

    # KROK 2: Główna transakcja stagingowa ze ścisłą blokadą
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 2a. Deterministyczne blokady advisory lock per akt (w kolejności alfabetycznej)
            for code in act_codes:
                cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0));", (code,))

            # 2b. Rejestracja aktów w legal_acts
            for art in articles:
                code = art["act_code"]
                title = art.get("act_title", code)
                cur.execute("""
                    INSERT INTO legal_acts (act_code, act_title)
                    VALUES (%s, %s)
                    ON CONFLICT (act_code) DO UPDATE SET act_title = EXCLUDED.act_title;
                """, (code, title))

            # 2c. Wyznaczenie kolejnej wersji snapshotu dla każdego z importowanych aktów
            act_versions: Dict[str, int] = {}
            for code in act_codes:
                cur.execute("""
                    SELECT COALESCE(MAX(snapshot_version), 0) + 1
                    FROM ingestion_run_acts
                    WHERE act_code = %s;
                """, (code,))
                row = cur.fetchone()
                act_versions[code] = row[0] if row else 1

            # 2d. Wstawianie artykułów na staging (is_active = FALSE)
            seen_chunk_ids = set()
            for art in articles:
                code = art["act_code"]
                art_num = str(art["article_number"])
                paragraph = art.get("paragraph")
                point = art.get("point")
                chunk_id = art.get("chunk_id") or compute_chunk_id(code, art_num, paragraph, point)

                if chunk_id in seen_chunk_ids:
                    raise IngestionValidationError(
                        f"Wykryto zduplikowany chunk_id '{chunk_id}' dla aktu {code} art. {art_num} w przesyłanej paczce."
                    )
                seen_chunk_ids.add(chunk_id)

                content = art.get("content", "")
                content_hash = art.get("content_hash") or compute_content_hash(content)
                embedding = art.get("embedding")
                snapshot_ver = act_versions[code]

                cur.execute("""
                    INSERT INTO legal_articles (
                        id, act_code, act_title, chapter, article_number,
                        full_title, content, clean_text, embedding, search_vector,
                        valid_from, status, year_effective, version, is_active,
                        ingested_at, ingestion_run_id, chunk_id, content_hash
                    ) VALUES (
                        uuid_generate_v4(), %s, %s, %s, %s,
                        %s, %s, %s, %s::vector, to_tsvector('simple', %s),
                        '1991-01-01', %s, %s, %s, FALSE,
                        CURRENT_TIMESTAMP, %s, %s, %s
                    );
                """, (
                    code,
                    art.get("act_title", code),
                    art.get("chapter", ""),
                    art_num,
                    art.get("full_title", f"{code} Art. {art_num}"),
                    content,
                    art.get("clean_text", content),
                    embedding,
                    art.get("clean_text", content),
                    art.get("status", "OBOWIĄZUJĄCY"),
                    art.get("year_effective", 2026),
                    snapshot_ver,
                    str(run_id),
                    chunk_id,
                    content_hash
                ))

            # 2e. Rygorystyczna walidacja stagingu przed aktywacją
            if validate:
                cur.execute("""
                    SELECT
                        COUNT(*),
                        COUNT(chunk_id),
                        COUNT(DISTINCT chunk_id),
                        COUNT(content_hash),
                        COUNT(embedding),
                        COUNT(CASE WHEN length(clean_text) = 0 THEN 1 END),
                        COUNT(CASE WHEN vector_dims(embedding) != 768 THEN 1 END)
                    FROM legal_articles
                    WHERE ingestion_run_id = %s;
                """, (str(run_id),))
                total_staged, total_chunks, uniq_chunks, total_hashes, total_embeds, empty_texts, invalid_dims = cur.fetchone()

                if total_staged != len(articles):
                    raise IngestionValidationError(
                        f"Niezgodność liczby rekordów stagingu: oczekiwano {len(articles)}, zapisano {total_staged}."
                    )
                if total_chunks != uniq_chunks:
                    raise IngestionValidationError("Wykryto zduplikowane chunk_id na stagingu bazy danych.")
                if empty_texts > 0:
                    raise IngestionValidationError(f"Wykryto {empty_texts} artykułów z pustą treścią 'clean_text'.")
                if total_embeds < total_staged:
                    raise IngestionValidationError("Wykryto brakujące embeddingi w rekordach stagingu.")
                if invalid_dims > 0:
                    raise IngestionValidationError(f"Wykryto {invalid_dims} embeddingów o wymiarze innym niż 768.")

            # Punkt kontrolny testowania odporności i atomowego rollbacku po stagingu
            if before_activation is not None:
                before_activation()

            # 2f. Atomowe przełączenie aktywności per-akt
            # 1. Deaktywacja poprzednich wersji TYLKO dla aktów z bieżącej paczki
            cur.execute("""
                UPDATE legal_articles
                SET is_active = FALSE
                WHERE is_active = TRUE AND act_code = ANY(%s);
            """, (act_codes,))

            # 2. Oznaczenie poprzednich ingestion_run_acts jako superseded
            cur.execute("""
                UPDATE ingestion_run_acts
                SET is_active = FALSE, superseded_at = CURRENT_TIMESTAMP
                WHERE is_active = TRUE AND act_code = ANY(%s);
            """, (act_codes,))

            # 3. Zapisanie nowych rekordów ingestion_run_acts
            for code, ver in act_versions.items():
                cur.execute("""
                    INSERT INTO ingestion_run_acts (
                        ingestion_run_id, act_code, snapshot_version, is_active, activated_at
                    ) VALUES (%s, %s, %s, TRUE, CURRENT_TIMESTAMP);
                """, (str(run_id), code, ver))

            # 4. Aktywacja nowych artykułów ściśle według ingestion_run_id
            cur.execute("""
                UPDATE legal_articles
                SET is_active = TRUE
                WHERE ingestion_run_id = %s;
            """, (str(run_id),))

            # 5. Aktualizacja statusu runu na COMPLETED w tej samej transakcji
            cur.execute("""
                UPDATE ingestion_runs
                SET status = 'COMPLETED', completed_at = CURRENT_TIMESTAMP
                WHERE id = %s;
            """, (str(run_id),))

        conn.commit()
        logger.info(f"Pomyślnie i atomowo zaimportowano {len(articles)} artykułów dla aktów {act_codes} (run_id: {run_id}).")
        return {
            "status": "success",
            "ingestion_run_id": str(run_id),
            "act_codes": act_codes,
            "act_versions": act_versions,
            "articles_count": len(articles)
        }
    except Exception as e:
        conn.rollback()
        err_code, clean_msg = sanitize_error_message(e, password=password_to_mask)
        logger.error(f"Błąd podczas stagingu ingestu run_id={run_id} (wykonano ROLLBACK): {clean_msg}")

        # Zapisanie statusu FAILED w ingestion_runs w osobnym połączeniu
        try:
            conn_fail = get_db_connection()
            try:
                with conn_fail.cursor() as cur_fail:
                    cur_fail.execute("""
                        UPDATE ingestion_runs
                        SET status = 'FAILED', completed_at = CURRENT_TIMESTAMP,
                            error_code = %s, error_message = %s
                        WHERE id = %s;
                    """, (err_code, clean_msg, str(run_id)))
                conn_fail.commit()
            finally:
                conn_fail.close()
        except Exception as log_err:
            logger.warning(f"Nie udało się zaktualizować statusu FAILED dla run_id={run_id}: {log_err}")

        raise
    finally:
        conn.close()


def save_articles_to_db(articles: List[Dict[str, Any]]):
    """Zapisuje listę artykułów z wektorami w bazie pgvector z użyciem atomowego stagingu."""
    if not articles:
        return
    ingest_articles_staged(articles, validate=True)


def verify_no_duplicates_in_db() -> Dict[str, Any]:
    """Weryfikuje, czy aktywna baza danych nie zawiera duplikatów i zwraca statystyki unikalności."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT act_code, article_number, COUNT(*)
                FROM legal_articles
                WHERE is_active = TRUE
                GROUP BY act_code, article_number
                HAVING COUNT(*) > 1;
            """)
            duplicates = cur.fetchall()

            cur.execute("SELECT COUNT(*), COUNT(DISTINCT (act_code, article_number)) FROM legal_articles WHERE is_active = TRUE;")
            row = cur.fetchone()
            total_rows = row[0] if row else 0
            distinct_articles = row[1] if row else 0

        if duplicates:
            logger.error(f"Wskazano {len(duplicates)} zduplikowanych artykułów w aktywnej bazie!")
            raise RuntimeError(f"Baza zawiera duplikaty! Przykłady: {duplicates[:3]}")

        logger.info(f"Weryfikacja duplikatów [OK]: {total_rows} artykułów (100% unikalnych, 0 dubli).")
        return {
            "total_articles": total_rows,
            "distinct_articles": distinct_articles,
            "has_duplicates": False
        }
    finally:
        conn.close()


def search_hybrid_db(query_embedding: List[float], query_text: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """
    Wykonuje zapytanie hybrydowe (Vector HNSW + FTS tsvector) z łączeniem wyników RRF (Reciprocal Rank Fusion).
    Wyszukuje WYŁĄCZNIE artykuły ze statusem is_active = TRUE.
    """
    conn = get_db_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql = """
            WITH vector_search AS (
                SELECT id, act_code, act_title, chapter, article_number, full_title, content,
                       1 - (embedding <=> %s::vector) AS cosine_sim,
                       ROW_NUMBER() OVER (ORDER BY embedding <=> %s::vector) AS rank_vec
                FROM legal_articles
                WHERE is_active = TRUE AND status = 'OBOWIĄZUJĄCY' AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT 25
            ),
            fts_search AS (
                SELECT id, act_code, act_title, chapter, article_number, full_title, content,
                       ts_rank(search_vector, websearch_to_tsquery('simple', %s)) AS fts_score,
                       ROW_NUMBER() OVER (ORDER BY ts_rank(search_vector, websearch_to_tsquery('simple', %s)) DESC) AS rank_fts
                FROM legal_articles
                WHERE is_active = TRUE AND search_vector @@ websearch_to_tsquery('simple', %s)
                ORDER BY fts_score DESC
                LIMIT 25
            )
            SELECT 
                COALESCE(v.id, f.id) AS id,
                COALESCE(v.act_code, f.act_code) AS act_code,
                COALESCE(v.act_title, f.act_title) AS act_title,
                COALESCE(v.chapter, f.chapter) AS chapter,
                COALESCE(v.article_number, f.article_number) AS article_number,
                COALESCE(v.full_title, f.full_title) AS full_title,
                COALESCE(v.content, f.content) AS content,
                COALESCE(v.cosine_sim, 0) AS cosine_sim,
                COALESCE(f.fts_score, 0) AS fts_score,
                (COALESCE(1.0 / (60 + v.rank_vec), 0) + COALESCE(1.0 / (60 + f.rank_fts), 0)) AS rrf_score
            FROM vector_search v
            FULL OUTER JOIN fts_search f ON v.id = f.id
            ORDER BY rrf_score DESC
            LIMIT %s;
            """
            
            embedding_str = "[" + ",".join(str(x) for x in query_embedding) + "]"
            cur.execute(sql, (embedding_str, embedding_str, embedding_str, query_text, query_text, query_text, top_k))
            results = cur.fetchall()
            return [dict(r) for r in results]
    finally:
        conn.close()
