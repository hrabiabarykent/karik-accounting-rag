import os
import time
import uuid
import concurrent.futures
from typing import List, Dict, Any, Optional
import pytest

from database.config import PostgresConfig
from database.migrations import apply_migrations
from database.exceptions import IngestionValidationError, DatabaseConnectionError
from rag.db import get_db_connection, ingest_articles_staged, search_hybrid_db


def check_postgres_available() -> bool:
    """Sprawdza dostępność bazy PostgreSQL dla testów integracyjnych."""
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
        conn.close()
        return True
    except Exception:
        return False


# Na Linux CI ze skonfigurowaną usługą PostgreSQL testy są bezwzględnie wymagane i nie mogą być cicho pomijane
if os.environ.get("CI") and os.environ.get("RUNNER_OS") == "Linux":
    pytestmark = [pytest.mark.postgres]
else:
    pytestmark = [
        pytest.mark.postgres,
        pytest.mark.skipif(not check_postgres_available(), reason="Baza PostgreSQL/pgvector jest niedostępna")
    ]


TEST_SESSION_ID = uuid.uuid4().hex[:12]
TRACKED_TEST_RUN_IDS = set()


def cleanup_test_records(run_ids: Optional[List[str]] = None):
    """
    Bezpiecznie usuwa wyłącznie dane utworzone przez bieżącą sesję testową:
    1. Identyfikuje wszystkie runy powiązane z tą sesją (source_file LIKE %s oraz jawnie przekazane run_ids).
    2. Usuwa powiązane rekordy w legal_articles i ingestion_run_acts według ingestion_run_id = ANY(%s::uuid[]).
    3. Usuwa powiązane wpisy w ingestion_runs według id = ANY(%s::uuid[]).
    4. Zabezpiecza usunięcie ewentualnych nieskończonych rekordów z kodami aktów tej sesji.
    5. Usuwa metadane aktów należących wyłącznie do tej sesji, które nie mają żadnych artykułów.
    NIGDY nie wykonuje globalnego czyszczenia 'TEST_%' ani 'pytest:%', chroniąc równoległe sesje i audyt awarii.
    """
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # 1. Zbierz wszystkie ID runów powiązane z tą sesją
            cur.execute(
                "SELECT id::text FROM ingestion_runs WHERE source_file LIKE %s;",
                (f"pytest:{TEST_SESSION_ID}:%",)
            )
            session_run_ids = {row[0] for row in cur.fetchall()}
            all_run_ids = list(session_run_ids.union(TRACKED_TEST_RUN_IDS).union(run_ids or []))

            # 2. Usuń artykuły i powiązania runów należące wyłącznie do tej sesji
            if all_run_ids:
                cur.execute("DELETE FROM legal_articles WHERE ingestion_run_id = ANY(%s::uuid[]);", (all_run_ids,))
                cur.execute("DELETE FROM ingestion_run_acts WHERE ingestion_run_id = ANY(%s::uuid[]);", (all_run_ids,))
                cur.execute("DELETE FROM ingestion_runs WHERE id = ANY(%s::uuid[]);", (all_run_ids,))

            # 3. Dodatkowe zabezpieczenie: usuń ewentualne artykuły oznaczone kodem tej sesji
            cur.execute(
                "DELETE FROM legal_articles WHERE act_code LIKE %s OR act_code LIKE %s;",
                (f"TEST_{TEST_SESSION_ID}_%", f"CONC_{TEST_SESSION_ID}_%")
            )
            cur.execute(
                "DELETE FROM ingestion_run_acts WHERE act_code LIKE %s OR act_code LIKE %s;",
                (f"TEST_{TEST_SESSION_ID}_%", f"CONC_{TEST_SESSION_ID}_%")
            )

            # 4. Usuń metadane aktów należących do tej sesji, które nie mają już żadnych artykułów
            cur.execute("""
                DELETE FROM legal_acts la
                WHERE (la.act_code LIKE %s OR la.act_code LIKE %s)
                  AND NOT EXISTS (
                      SELECT 1
                      FROM legal_articles art
                      WHERE art.act_code = la.act_code
                  );
            """, (f"TEST_{TEST_SESSION_ID}_%", f"CONC_{TEST_SESSION_ID}_%"))

            TRACKED_TEST_RUN_IDS.clear()
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean_test_environment():
    """Zapewnia aktualność schematu bazy i izolację danych testowych przed i po każdym teście."""
    cfg = PostgresConfig.from_env(require_password=False)
    if not cfg.dbname.endswith("_test"):
        pytest.fail(
            f"Testy integracyjne wymagają dedykowanej bazy testowej z sufiksem '_test' (wykryto dbname='{cfg.dbname}'). "
            "Ustaw POSTGRES_DB na bazę testową (np. 'karik_db_test'), aby zapobiec modyfikacji bazy produkcyjnej/deweloperskiej."
        )

    conn = get_db_connection()
    try:
        apply_migrations(conn)
    finally:
        conn.close()

    cleanup_test_records()
    yield
    cleanup_test_records()



def test_migrations_runner_and_upgrade():
    """Weryfikacja, że runner migracji wykonuje się poprawnie, idempotentnie i tworzy wszystkie tabele."""
    conn = get_db_connection()
    try:
        applied = apply_migrations(conn)
        assert isinstance(applied, list)

        with conn.cursor() as cur:
            # Weryfikacja obecności wszystkich tabel modelu PR 4B
            cur.execute("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_name IN ('legal_acts', 'ingestion_runs', 'ingestion_run_acts', 'rag_audit_log', 'schema_migrations');
            """)
            tables = {row[0] for row in cur.fetchall()}
            assert "legal_acts" in tables
            assert "ingestion_runs" in tables
            assert "ingestion_run_acts" in tables
            assert "rag_audit_log" in tables
            assert "schema_migrations" in tables

            # Weryfikacja obecności nowych kolumn w legal_articles
            cur.execute("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'legal_articles' AND column_name IN ('ingestion_run_id', 'chunk_id', 'content_hash');
            """)
            cols = {row[0] for row in cur.fetchall()}
            assert "ingestion_run_id" in cols
            assert "chunk_id" in cols
            assert "content_hash" in cols
    finally:
        conn.close()


def test_ingest_articles_staged_and_per_act_atomic_switch():
    """
    Testuje kluczową zasadę per-akt ingestu:
    Aktualizacja aktu ACT_A nie deaktywuje istniejących rekordów aktu ACT_B!
    Kody aktów posiadają unikalny prefiks sesji TEST_SESSION_ID, co gwarantuje pełną izolację.
    """
    act_a = f"TEST_{TEST_SESSION_ID}_ACT_A"
    act_b = f"TEST_{TEST_SESSION_ID}_ACT_B"

    conn = get_db_connection()
    try:
        # 1. Ingest dwóch niezależnych aktów prawnych
        articles_batch_1 = [
            {
                "act_code": act_a,
                "act_title": "Ustawa A",
                "chapter": "Rozdział 1",
                "article_number": "1",
                "content": "Treść A wersja 1",
                "clean_text": "Tresc A wersja 1",
                "embedding": [0.01] * 768,
            },
            {
                "act_code": act_b,
                "act_title": "Ustawa B",
                "chapter": "Rozdział 1",
                "article_number": "1",
                "content": "Treść B wersja 1",
                "clean_text": "Tresc B wersja 1",
                "embedding": [0.02] * 768,
            }
        ]

        res1 = ingest_articles_staged(
            articles_batch_1,
            source_file=f"pytest:{TEST_SESSION_ID}:atomic_b1_{uuid.uuid4().hex[:6]}",
            validate=True
        )
        run_id_1 = res1["ingestion_run_id"]
        TRACKED_TEST_RUN_IDS.add(run_id_1)
        assert res1["status"] == "success"

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE act_code = %s AND is_active = TRUE;", (act_a,))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE act_code = %s AND is_active = TRUE;", (act_b,))
            assert cur.fetchone()[0] == 1

        # 2. Import nowej wersji WYŁĄCZNIE dla ACT_A
        articles_batch_2 = [
            {
                "act_code": act_a,
                "act_title": "Ustawa A",
                "chapter": "Rozdział 1",
                "article_number": "1",
                "content": "Treść A wersja 2 (znowelizowana)",
                "clean_text": "Tresc A wersja 2 (znowelizowana)",
                "embedding": [0.03] * 768,
            }
        ]

        res2 = ingest_articles_staged(
            articles_batch_2,
            source_file=f"pytest:{TEST_SESSION_ID}:atomic_b2_{uuid.uuid4().hex[:6]}",
            validate=True
        )
        run_id_2 = res2["ingestion_run_id"]
        TRACKED_TEST_RUN_IDS.add(run_id_2)

        with conn.cursor() as cur:
            # A zostało zaktualizowane: wersja 1 jest nieaktywna, wersja 2 aktywna
            cur.execute("""
                SELECT version, is_active, content 
                FROM legal_articles 
                WHERE act_code = %s 
                ORDER BY version ASC;
            """, (act_a,))
            rows_a = cur.fetchall()
            assert len(rows_a) == 2
            assert rows_a[0][1] is False  # v1 deaktywowana
            assert rows_a[1][1] is True   # v2 aktywna
            assert "wersja 2" in rows_a[1][2]

            # KLUCZOWY WARUNEK: ACT_B nadal pozostaje AKTYWNY!
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE act_code = %s AND is_active = TRUE;", (act_b,))
            assert cur.fetchone()[0] == 1

            # Weryfikacja wpisów w ingestion_run_acts
            cur.execute("SELECT act_code, is_active FROM ingestion_run_acts WHERE ingestion_run_id = %s;", (run_id_1,))
            run1_acts = {r[0]: r[1] for r in cur.fetchall()}
            assert run1_acts[act_a] is False  # A w starym runie jest superseded
            assert run1_acts[act_b] is True   # B w starym runie nadal jest aktywne!

            cur.execute("SELECT act_code, is_active FROM ingestion_run_acts WHERE ingestion_run_id = %s;", (run_id_2,))
            run2_acts = {r[0]: r[1] for r in cur.fetchall()}
            assert run2_acts[act_a] is True

        # Czyszczenie ściśle dla tej sesji
        cleanup_test_records([run_id_1, run_id_2])
    finally:
        conn.close()


def test_ingest_articles_staged_real_transactional_rollback():
    """
    Rzeczywisty test ROLLBACK:
    1. Tworzy aktywny artykuł w v1.
    2. Rozpoczyna nowy staged ingest v2 z POPRAWNYMI danymi (przechodzi INSERT stagingu i walidację).
    3. Wymusza kontrolowany wyjątek w punkcie 'before_activation' (po stagingu, przed aktywacją).
    4. Weryfikuje, że transakcja została atomowo wycofana:
       - brak rekordów v2 w legal_articles (rollback stagingu),
       - stary rekord v1 pozostał w 100% aktywny,
       - w tabeli ingestion_runs zapisano status 'FAILED' z oczyszczonym błędem i zachowano telemetrię.
    """
    act_rollback = f"TEST_{TEST_SESSION_ID}_ROLLBACK"
    conn = get_db_connection()
    try:
        # 1. Poprawny pierwszy import (v1)
        initial_articles = [
            {
                "act_code": act_rollback,
                "act_title": "Ustawa Rollback",
                "article_number": "1",
                "content": "Pierwotna treść stabilna",
                "clean_text": "Pierwotna tresc stabilna",
                "embedding": [0.05] * 768,
            }
        ]
        init_source_file = f"pytest:{TEST_SESSION_ID}:rollback_init_{uuid.uuid4().hex[:6]}"
        res1 = ingest_articles_staged(initial_articles, source_file=init_source_file, validate=True)
        initial_run_id = res1["ingestion_run_id"]
        TRACKED_TEST_RUN_IDS.add(initial_run_id)

        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE act_code = %s AND is_active = TRUE;", (act_rollback,))
            assert cur.fetchone()[0] == 1

        # 2. Próba importu v2 z poprawnymi danymi, ale wymuszonym błędem po stagingu i przed aktywacją
        valid_staged_articles = [
            {
                "act_code": act_rollback,
                "act_title": "Ustawa Rollback",
                "article_number": "1",
                "content": "Nowa treść stagingu, która nie powinna zostać aktywowana",
                "clean_text": "Nowa tresc stagingu, ktora nie powinna zostac aktywowana",
                "embedding": [0.08] * 768,
            }
        ]

        def crash_before_activation():
            raise RuntimeError("Simulated infrastructure crash before activation")

        fail_source_file = f"pytest:{TEST_SESSION_ID}:rollback_fail_{uuid.uuid4().hex[:6]}"
        with pytest.raises(RuntimeError, match="Simulated infrastructure crash before activation"):
            ingest_articles_staged(
                valid_staged_articles,
                source_file=fail_source_file,
                validate=True,
                before_activation=crash_before_activation
            )

        # 3. Weryfikacja stanu bazy po rollbacku
        with conn.cursor() as cur:
            # Całkowita liczba rekordów dla tego aktu to dokładnie 1 (rekord stagingowy v2 został całkowicie cofnięty przez ROLLBACK)
            cur.execute("SELECT COUNT(*) FROM legal_articles WHERE act_code = %s;", (act_rollback,))
            assert cur.fetchone()[0] == 1

            # Ten jedyny rekord to pierwotna wersja v1 i jest wciąż aktywny
            cur.execute("SELECT is_active, content FROM legal_articles WHERE act_code = %s;", (act_rollback,))
            row = cur.fetchone()
            assert row[0] is True
            assert row[1] == "Pierwotna treść stabilna"

            # W ingestion_run_acts nie ma aktywnego wpisu dla nieudanego importu
            cur.execute("SELECT COUNT(*) FROM ingestion_run_acts WHERE act_code = %s AND is_active = TRUE;", (act_rollback,))
            assert cur.fetchone()[0] == 1

            # W tabeli ingestion_runs istnieje wpis o awarii powiązany dokładnie z tym runem
            cur.execute("""
                SELECT status, error_code, error_message 
                FROM ingestion_runs 
                WHERE source_file = %s;
            """, (fail_source_file,))
            failed_run = cur.fetchone()
            assert failed_run is not None
            assert failed_run[0] == "FAILED"
            assert "Simulated infrastructure crash" in failed_run[2]
            # Upewnienie się że komunikat błędu nie zawiera haseł
            assert "password" not in failed_run[2].lower()

        # Czyszczenie
        cleanup_test_records([initial_run_id])
    finally:
        conn.close()


def test_concurrent_parallel_ingests_with_advisory_locks():
    """
    Weryfikuje, że równoległe importy tego samego aktu lub wielu aktów
    dzięki blokadom advisory lock i deterministycznej kolejności nie powodują zakleszczeń (deadlocks)
    ani konfliktów wyścigów wersji.
    Kody aktów posiadają unikalny prefiks sesji CONC_{TEST_SESSION_ID}_*.
    """
    conc_a = f"CONC_{TEST_SESSION_ID}_A"
    conc_b = f"CONC_{TEST_SESSION_ID}_B"

    def run_worker_ingest(worker_id: int, act_list: List[str]):
        articles = []
        for code in act_list:
            articles.append({
                "act_code": code,
                "act_title": f"Ustawa {code}",
                "article_number": f"{worker_id}",
                "content": f"Treść wygenerowana przez worker {worker_id} dla aktu {code}",
                "clean_text": f"Tresc worker {worker_id} {code}",
                "embedding": [0.01 * worker_id] * 768,
            })
        return ingest_articles_staged(
            articles,
            source_file=f"pytest:{TEST_SESSION_ID}:conc_w{worker_id}_{uuid.uuid4().hex[:6]}",
            validate=True
        )

    # Dwa wątki importujące akty w odwrotnej kolejności (potencjalny deadlock bez deterministycznego sortowania)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(run_worker_ingest, 1, [conc_a, conc_b])
        f2 = executor.submit(run_worker_ingest, 2, [conc_b, conc_a])

        res1 = f1.result(timeout=30)
        res2 = f2.result(timeout=30)

        assert res1["status"] == "success"
        assert res2["status"] == "success"
        assert res1["ingestion_run_id"] != res2["ingestion_run_id"]
        TRACKED_TEST_RUN_IDS.add(res1["ingestion_run_id"])
        TRACKED_TEST_RUN_IDS.add(res2["ingestion_run_id"])

    # Weryfikacja spójności końcowej
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Dokładnie po 1 aktywnym artykule dla każdego aktu
            cur.execute(
                "SELECT act_code, COUNT(*) FROM legal_articles WHERE act_code IN (%s, %s) AND is_active = TRUE GROUP BY act_code;",
                (conc_a, conc_b)
            )
            active_counts = dict(cur.fetchall())
            assert active_counts[conc_a] == 1
            assert active_counts[conc_b] == 1

        # Czyszczenie
        cleanup_test_records([res1["ingestion_run_id"], res2["ingestion_run_id"]])
    finally:
        conn.close()


def test_search_hybrid_db_filters_inactive():
    """
    Weryfikuje, że search_hybrid_db zwraca WYŁĄCZNIE aktywne wersje (is_active = TRUE).
    Zapytanie i akt używają unikalnego tokenu sesji f"amortyzacja_{TEST_SESSION_ID}".
    """
    act_search = f"TEST_{TEST_SESSION_ID}_SEARCH"
    search_token = f"amortyzacja_{TEST_SESSION_ID}"

    # Ingest v1
    art_v1 = [{
        "act_code": act_search,
        "act_title": "Ustawa Wyszukiwanie",
        "article_number": "42",
        "content": f"Stary przepis nieobowiązujący o {search_token}",
        "clean_text": f"Stary przepis nieobowiazujacy o {search_token}",
        "embedding": [0.01] * 768,
    }]
    res1 = ingest_articles_staged(
        art_v1,
        source_file=f"pytest:{TEST_SESSION_ID}:search_v1_{uuid.uuid4().hex[:6]}",
        validate=True
    )
    TRACKED_TEST_RUN_IDS.add(res1["ingestion_run_id"])

    # Ingest v2 (nowa treść)
    art_v2 = [{
        "act_code": act_search,
        "act_title": "Ustawa Wyszukiwanie",
        "article_number": "42",
        "content": f"Nowy znowelizowany przepis aktualny o {search_token}",
        "clean_text": f"Nowy znowelizowany przepis aktualny o {search_token}",
        "embedding": [0.02] * 768,
    }]
    res2 = ingest_articles_staged(
        art_v2,
        source_file=f"pytest:{TEST_SESSION_ID}:search_v2_{uuid.uuid4().hex[:6]}",
        validate=True
    )
    TRACKED_TEST_RUN_IDS.add(res2["ingestion_run_id"])

    # Wyszukiwanie hybrydowe
    results = search_hybrid_db(query_embedding=[0.02] * 768, query_text=search_token, top_k=10)
    search_hits = [r for r in results if r["act_code"] == act_search]

    assert len(search_hits) == 1
    assert "Nowy znowelizowany przepis" in search_hits[0]["content"]
    assert "Stary przepis" not in search_hits[0]["content"]

    # Czyszczenie
    cleanup_test_records([res1["ingestion_run_id"], res2["ingestion_run_id"]])

