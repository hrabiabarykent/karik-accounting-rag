import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional
import socket

logger = logging.getLogger(__name__)


def _load_env_db():
    dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(dotenv_path):
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path, override=False)
        except ImportError:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k_s = k.strip()
                        if k_s and k_s not in os.environ:
                            os.environ[k_s] = v.strip().strip("'\"")

_load_env_db()


_cached_conn_params = None

def get_db_connection():
    """Nawiązuje połączenie z bazą PostgreSQL pgvector z automatycznym dopasowaniem hosta IPv4, portu i poświadczeń."""
    global _cached_conn_params
    
    # 1. Szybkie połączenie na zapamiętanych, zweryfikowanych parametrach
    if _cached_conn_params is not None:
        try:
            return psycopg2.connect(**_cached_conn_params, client_encoding="utf8", connect_timeout=3)
        except Exception:
            _cached_conn_params = None  # W razie utraty parametrów, reset i ponowna detekcja

    host_env = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port_env = os.getenv("POSTGRES_PORT")
    
    dbname_env = os.getenv("POSTGRES_DB", "karik_db")
    user_env = os.getenv("POSTGRES_USER", "karik_admin")
    pwd_env = os.getenv("POSTGRES_PASSWORD", "karik_password")

    # Sprawdzenie czy działamy w sieci Dockera
    is_docker_net = False
    if host_env and host_env not in ("localhost", "127.0.0.1", "::1"):
        try:
            socket.gethostbyname(host_env)
            is_docker_net = True
        except socket.gaierror:
            is_docker_net = False

    if is_docker_net:
        target_hosts = [host_env]
        target_ports = [int(port_env) if port_env else 5432]
    else:
        # Na Windows Docker Desktop porty kontenera są wystawione na IPv4 127.0.0.1
        target_hosts = ["127.0.0.1", "localhost"]
        main_port = int(port_env) if port_env else 15432
        target_ports = [main_port]
        if 15432 not in target_ports:
            target_ports.append(15432)
        if 5432 not in target_ports:
            target_ports.append(5432)

    creds_to_try = [
        (user_env, pwd_env, dbname_env),
        ("karik_admin", "karik_password", "karik_db"),
        ("wolf_admin", "wolf_password", "wolf_db"),
        ("wolf_admin", "wolf_password", "karik_db"),
        ("karik_admin", "karik_password", "wolf_db"),
        ("postgres", "karik_password", "karik_db"),
        ("postgres", "wolf_password", "wolf_db"),
        ("postgres", "postgres", "postgres"),
    ]

    last_error = None
    for h in target_hosts:
        for p in target_ports:
            for u, pwd, db in creds_to_try:
                try:
                    conn = psycopg2.connect(
                        host=h,
                        port=p,
                        dbname=db,
                        user=u,
                        password=pwd,
                        client_encoding="utf8",
                        connect_timeout=2
                    )
                    # Zapamiętujemy działający zestaw parametrów dla przyszłych błyskawicznych zapytań
                    _cached_conn_params = {
                        "host": h,
                        "port": p,
                        "dbname": db,
                        "user": u,
                        "password": pwd
                    }
                    return conn
                except Exception as e:
                    last_error = e

    # Bezpieczne dekodowanie błędu w przypadku problemów ze stroną kodową CP1250 na Windowsie
    if last_error:
        err_msg = str(last_error)
        if isinstance(last_error, UnicodeDecodeError) or "utf-8" in err_msg.lower():
            raw_bytes = getattr(last_error, "object", None)
            if isinstance(raw_bytes, bytes):
                decoded_msg = raw_bytes.decode("cp1250", errors="replace")
            else:
                decoded_msg = f"Błąd autoryzacji lub połączenia z bazą PostgreSQL na porcie {target_ports[0]}."
            raise RuntimeError(f"Błąd połączenia z PostgreSQL: {decoded_msg}") from last_error
        raise last_error



def init_database():
    """Inicjalizuje rozszerzenie pgvector oraz schemat tabel."""
    sql_script_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "init_db.sql")
    if os.path.exists(sql_script_path):
        try:
            with open(sql_script_path, "r", encoding="utf-8") as f:
                sql = f.read()
        except UnicodeDecodeError:
            with open(sql_script_path, "r", encoding="cp1250", errors="replace") as f:
                sql = f.read()

    else:
        sql = """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
        """

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        conn.close()

def save_articles_to_db(articles: List[Dict[str, Any]]):
    """Zapisuje listę artykułów z wektorami i tsvector w bazie pgvector z bezpiecznym nadpisywaniem starych wersji."""
    if not articles:
        return

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Czyszczenie starych wersji dla ustaw zawartych w paczce (np. 'VAT', 'PIT')
            act_codes = list({art["act_code"] for art in articles if "act_code" in art})
            for code in act_codes:
                cur.execute("DELETE FROM legal_articles WHERE act_code = %s;", (code,))

            for art in articles:
                # Wprowadzamy zaktualizowany artykuł
                cur.execute("""
                    INSERT INTO legal_articles (
                        act_code, act_title, chapter, article_number, 
                        full_title, content, clean_text, embedding, search_vector,
                        valid_from, status, year_effective
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s, %s, %s::vector, to_tsvector('simple', %s),
                        '1991-01-01', %s, %s
                    )
                    ON CONFLICT (id) DO NOTHING;
                """, (
                    art["act_code"],
                    art["act_title"],
                    art.get("chapter", ""),
                    art["article_number"],
                    art.get("full_title", ""),
                    art["content"],
                    art["clean_text"],
                    art.get("embedding"),
                    art["clean_text"],
                    art.get("status", "OBOWIĄZUJĄCY"),
                    art.get("year_effective", 2026)
                ))
        conn.commit()
    finally:
        conn.close()

def verify_no_duplicates_in_db() -> Dict[str, Any]:
    """Weryfikuje, czy baza danych nie zawiera duplikatów i zwraca statystyki unikalności."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT act_code, article_number, COUNT(*)
                FROM legal_articles
                GROUP BY act_code, article_number
                HAVING COUNT(*) > 1;
            """)
            duplicates = cur.fetchall()

            cur.execute("SELECT COUNT(*), COUNT(DISTINCT (act_code, article_number)) FROM legal_articles;")
            row = cur.fetchone()
            total_rows = row[0] if row else 0
            distinct_articles = row[1] if row else 0

        if duplicates:
            logger.error(f"Wskazano {len(duplicates)} zduplikowanych artykułów w bazie!")
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
                WHERE status = 'OBOWIĄZUJĄCY' AND embedding IS NOT NULL
                ORDER BY embedding <=> %s::vector
                LIMIT 25
            ),
            fts_search AS (
                SELECT id, act_code, act_title, chapter, article_number, full_title, content,
                       ts_rank(search_vector, websearch_to_tsquery('simple', %s)) AS fts_score,
                       ROW_NUMBER() OVER (ORDER BY ts_rank(search_vector, websearch_to_tsquery('simple', %s)) DESC) AS rank_fts
                FROM legal_articles
                WHERE search_vector @@ websearch_to_tsquery('simple', %s)
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
