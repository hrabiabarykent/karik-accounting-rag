"""
Trwała warstwa danych dla cyklu życia dokumentu, wersji audytowych i transakcyjnego outboxa.
Zapewnia atomowy zapis dokumentu z próbą PENDING i zdarzeniem Outbox w jednej transakcji SQL,
atomowe przejmowanie prób przez workerów (conditional UPDATE z leasingiem i fencingiem),
pełne wersjonowanie ekstrakcji i korekt operatora,
oraz 3-tabelowy trwały model eksportu ERP (erp_exports, erp_export_attempts, erp_export_events).

Wspiera:
- PostgreSQL (produkcja, integracja, multi-worker) za pośrednictwem psycopg2
- SQLite (izolowane testy jednostkowe) za pośrednictwem sqlite3
W przypadku jawnej konfiguracji PostgreSQL, awaria połączenia podnosi wyjątek
DatabaseConfigurationError (brak cichego przechodzenia w tryb pamięciowy / SQLite).
"""

import os
import json
import uuid
import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

from .models import (
    Document,
    ProcessingAttempt,
    OutboxEvent,
    ExtractionVersion,
    OperatorReview,
    ERPExportEvent,
    ERPExport,
    ERPExportAttempt,
    DocumentStatus,
    AttemptStatus,
    SourceType,
    ExportStatus,
    InvoiceSourceData,
    AISuggestions,
    ValidationReport,
)

logger = logging.getLogger(__name__)


class DatabaseConfigurationError(RuntimeError):
    """Podnoszony w przypadku błędu połączenia lub konfiguracji produkcyjnej bazy PostgreSQL."""
    pass


class DocumentNotApprovedError(RuntimeError):
    """Podnoszony przy próbie eksportu dokumentu, który nie ma statusu APPROVED."""
    pass


class VersionNotApprovedError(RuntimeError):
    """Podnoszony gdy wskazana do eksportu wersja nie jest wersją zatwierdzoną."""
    pass


class ERPExportInFlightError(RuntimeError):
    """Podnoszony gdy operacja eksportu dla danego klucza idempotencji jest aktualnie w trakcie transmisji."""
    pass


class ERPExportUnknownError(RuntimeError):
    """Podnoszony gdy stan poprzedniej transmisji jest nieznany (UNKNOWN) i wymaga formalnego uzgodnienia."""
    pass


def _parse_dt(val: Any) -> Optional[datetime]:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    if isinstance(val, str):
        try:
            return datetime.fromisoformat(val)
        except Exception:
            return None
    return None


def _parse_json(val: Any) -> Any:
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    if isinstance(val, str):
        try:
            return json.loads(val)
        except Exception:
            return None
    return None


class _DocumentsProxy:
    def __init__(self, repo):
        self._repo = repo
    def __getitem__(self, doc_id):
        doc = self._repo.get_document(doc_id)
        if not doc:
            raise KeyError(doc_id)
        return doc
    def get(self, doc_id, default=None):
        doc = self._repo.get_document(doc_id)
        return doc if doc is not None else default
    def values(self):
        return self._repo.get_all_documents()
    def __contains__(self, doc_id):
        return self._repo.get_document(doc_id) is not None


class _AttemptsProxy:
    def __init__(self, repo):
        self._repo = repo
    def __getitem__(self, att_id):
        att = self._repo.get_attempt(att_id)
        if not att:
            raise KeyError(att_id)
        return att
    def get(self, att_id, default=None):
        att = self._repo.get_attempt(att_id)
        return att if att is not None else default
    def values(self):
        return self._repo.get_all_attempts()
    def __contains__(self, att_id):
        return self._repo.get_attempt(att_id) is not None


class _OutboxEventsProxy:
    def __init__(self, repo):
        self._repo = repo
    def __getitem__(self, event_id):
        ev = self._repo.get_outbox_event(event_id)
        if not ev:
            raise KeyError(event_id)
        return ev
    def get(self, event_id, default=None):
        ev = self._repo.get_outbox_event(event_id)
        return ev if ev is not None else default
    def values(self):
        return self._repo.get_all_outbox_events()
    def __contains__(self, event_id):
        return self._repo.get_outbox_event(event_id) is not None


class _VersionsProxy:
    def __init__(self, repo):
        self._repo = repo
    def get(self, doc_id, default=None):
        vers = self._repo.get_versions(doc_id)
        return vers if vers else default
    def __getitem__(self, doc_id):
        return self._repo.get_versions(doc_id)


class _ERPEventsProxy:
    def __init__(self, repo):
        self._repo = repo
    def __getitem__(self, doc_id):
        return self._repo.get_erp_events_for_document(doc_id)
    def get(self, doc_id, default=None):
        evs = self._repo.get_erp_events_for_document(doc_id)
        return evs if evs else default


class AccountingRepository:
    """
    Repozytorium cyklu życia dokumentów, wersjonowania ekstrakcji, outboxa i eksportu ERP
    oparte o trwałą bazę danych SQL (PostgreSQL / SQLite).
    """

    def __init__(
        self,
        db_engine: Optional[str] = None,
        database_url: Optional[str] = None,
        sqlite_db_path: Optional[str] = None,
    ):
        self._lock = threading.RLock()
        self._thread_local = threading.local()

        # 1. Rozpoznanie silnika bazy danych
        req_engine = (
            db_engine
            or os.getenv("KARIK_DB_ENGINE")
            or ""
        ).lower()

        raw_db_url = (
            database_url
            or os.getenv("DATABASE_URL")
            or ""
        )

        postgres_host = os.getenv("POSTGRES_HOST")

        if req_engine == "postgres" or raw_db_url.startswith(("postgresql://", "postgres://")) or (postgres_host and req_engine != "sqlite"):
            self.engine_type = "postgres"
            self.db_url = raw_db_url
            self.postgres_host = postgres_host or "localhost"
            self.postgres_port = int(os.getenv("POSTGRES_PORT", "5432"))
            self.postgres_db = os.getenv("POSTGRES_DB", "karik_db")
            self.postgres_user = os.getenv("POSTGRES_USER", "karik_admin")
            self.postgres_password = os.getenv("POSTGRES_PASSWORD", "karik_password")
            self._pool = None
            self._init_postgres_pool()
        else:
            self.engine_type = "sqlite"
            if sqlite_db_path:
                self.sqlite_path = sqlite_db_path
            elif raw_db_url.startswith("sqlite:///"):
                self.sqlite_path = raw_db_url.replace("sqlite:///", "")
            else:
                base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache"))
                os.makedirs(base_dir, exist_ok=True)
                self.sqlite_path = os.path.join(base_dir, "karik_accounting.db")

        # 2. Inicjalizacja schematu tabel
        self._init_schema()

    def _init_postgres_pool(self):
        """Inicjalizuje pulę połączeń psycopg2 dla PostgreSQL."""
        try:
            import psycopg2
            from psycopg2 import pool
            if self.db_url:
                self._pool = pool.ThreadedConnectionPool(minconn=1, maxconn=15, dsn=self.db_url)
            else:
                self._pool = pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=15,
                    host=self.postgres_host,
                    port=self.postgres_port,
                    dbname=self.postgres_db,
                    user=self.postgres_user,
                    password=self.postgres_password,
                    connect_timeout=3
                )
            logger.info(f"Pomyślnie połączono z bazą PostgreSQL ({self.postgres_host}:{self.postgres_port}/{self.postgres_db}).")
        except Exception as exc:
            # JAWNY BŁĄD KONFIGURACJI: brak cichego przejścia na SQLite!
            raise DatabaseConfigurationError(
                f"Nie można nawiązać połączenia z produkcyjną bazą PostgreSQL: {exc}"
            ) from exc

    @contextmanager
    def get_connection(self):
        """Menedżer kontekstu zwracający połączenie z bazą danych z transakcją."""
        if self.engine_type == "postgres":
            conn = self._pool.getconn()
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                self._pool.putconn(conn)
        else:
            import sqlite3
            # Połączenie SQLite z WAL mode dla bezpiecznej współbieżności procesów
            conn = sqlite3.connect(self.sqlite_path, timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA busy_timeout=5000;")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _format_sql(self, sql: str) -> str:
        """Dostosowuje placeholdery do specyfiki silnika (Postgres: %s, SQLite: ?)."""
        if self.engine_type == "sqlite":
            return sql.replace("%s", "?")
        return sql

    def _format_dt(self, dt: Optional[datetime]) -> Optional[Any]:
        if dt is None:
            return None
        if self.engine_type == "sqlite":
            return dt.isoformat()
        return dt

    def _init_schema(self):
        """Tworzy schemat 8 tabel trwałych jeśli nie istnieją."""
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Tabela dokumentów
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS documents (
                    id VARCHAR(36) PRIMARY KEY,
                    tenant_id VARCHAR(64) NOT NULL,
                    original_filename VARCHAR(255) NOT NULL,
                    storage_path VARCHAR(512) NOT NULL,
                    mime_type VARCHAR(64) NOT NULL,
                    sha256_hash VARCHAR(64) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    approved_version_id VARCHAR(36),
                    created_by_operator_id VARCHAR(64) NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    UNIQUE(tenant_id, sha256_hash)
                );
            """))

            # 2. Tabela prób przetwarzania (z polami dzierżawy leasing/fencing)
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS processing_attempts (
                    id VARCHAR(36) PRIMARY KEY,
                    document_id VARCHAR(36) NOT NULL REFERENCES documents(id),
                    task_id VARCHAR(128) NOT NULL,
                    attempt_number INT NOT NULL DEFAULT 1,
                    status VARCHAR(32) NOT NULL,
                    worker_id VARCHAR(64),
                    lease_token VARCHAR(64),
                    error_details TEXT,
                    started_at TIMESTAMP,
                    heartbeat_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                );
            """))

            # 3. Tabela wersji ekstrakcji (pełna niemutowalność i audytowalność)
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS extraction_versions (
                    id VARCHAR(36) PRIMARY KEY,
                    document_id VARCHAR(36) NOT NULL REFERENCES documents(id),
                    attempt_id VARCHAR(36),
                    version_number INT NOT NULL,
                    source_type VARCHAR(32) NOT NULL,
                    parent_version_id VARCHAR(36),
                    immutable_source_data TEXT,
                    extraction_status VARCHAR(32) NOT NULL DEFAULT 'SUCCESS',
                    review_reason VARCHAR(64),
                    ai_suggestions TEXT,
                    validation_report TEXT NOT NULL,
                    created_by_operator_id VARCHAR(64),
                    created_at TIMESTAMP NOT NULL
                );
            """))

            # 4. Tabela decyzji audytorów / operatorów
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS operator_reviews (
                    id VARCHAR(36) PRIMARY KEY,
                    document_id VARCHAR(36) NOT NULL REFERENCES documents(id),
                    target_version_id VARCHAR(36) NOT NULL,
                    operator_id VARCHAR(64) NOT NULL,
                    decision VARCHAR(32) NOT NULL,
                    notes TEXT,
                    created_at TIMESTAMP NOT NULL
                );
            """))

            # 5. Tabela transakcyjnego outboxa
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS outbox_events (
                    id VARCHAR(36) PRIMARY KEY,
                    attempt_id VARCHAR(36) NOT NULL,
                    aggregate_type VARCHAR(64) NOT NULL,
                    aggregate_id VARCHAR(36) NOT NULL,
                    event_type VARCHAR(64) NOT NULL,
                    payload TEXT NOT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
                    retry_count INT NOT NULL DEFAULT 0,
                    max_retries INT NOT NULL DEFAULT 5,
                    next_retry_at TIMESTAMP,
                    last_error TEXT,
                    created_at TIMESTAMP NOT NULL,
                    sent_at TIMESTAMP
                );
            """))
            # Bezpieczna migracja kolumn tabeli outbox_events bez przerywania transakcji
            if self.engine_type == "sqlite":
                cursor.execute("PRAGMA table_info(outbox_events);")
                existing_cols = {row[1] if isinstance(row, tuple) else row["name"] for row in cursor.fetchall()}
                if "max_retries" not in existing_cols:
                    cursor.execute("ALTER TABLE outbox_events ADD COLUMN max_retries INT DEFAULT 5;")
                if "next_retry_at" not in existing_cols:
                    cursor.execute("ALTER TABLE outbox_events ADD COLUMN next_retry_at TIMESTAMP;")
                if "last_error" not in existing_cols:
                    cursor.execute("ALTER TABLE outbox_events ADD COLUMN last_error TEXT;")
            else:
                cursor.execute("ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS max_retries INT DEFAULT 5;")
                cursor.execute("ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMP;")
                cursor.execute("ALTER TABLE outbox_events ADD COLUMN IF NOT EXISTS last_error TEXT;")

            # 6. Trwały 3-tabelowy schemat eksportu ERP (erp_exports)
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS erp_exports (
                    id VARCHAR(36) PRIMARY KEY,
                    document_id VARCHAR(36) NOT NULL REFERENCES documents(id),
                    extraction_version_id VARCHAR(36) NOT NULL REFERENCES extraction_versions(id),
                    erp_system VARCHAR(64) NOT NULL,
                    idempotency_key VARCHAR(64) NOT NULL UNIQUE,
                    status VARCHAR(32) NOT NULL,
                    erp_reference_id VARCHAR(128),
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL
                );
            """))

            # 7. Trwały 3-tabelowy schemat eksportu ERP (erp_export_attempts)
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS erp_export_attempts (
                    id VARCHAR(36) PRIMARY KEY,
                    erp_export_id VARCHAR(36) NOT NULL REFERENCES erp_exports(id),
                    attempt_number INT NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    http_status INT,
                    request_payload TEXT,
                    response_payload TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP NOT NULL
                );
            """))

            # 8. Trwały 3-tabelowy schemat eksportu ERP (erp_export_events)
            cursor.execute(self._format_sql("""
                CREATE TABLE IF NOT EXISTS erp_export_events (
                    id VARCHAR(36) PRIMARY KEY,
                    document_id VARCHAR(36) NOT NULL,
                    extraction_version_id VARCHAR(36) NOT NULL,
                    erp_system VARCHAR(64) NOT NULL,
                    idempotency_key VARCHAR(64) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    erp_reference_id VARCHAR(128),
                    error_message TEXT,
                    payload TEXT,
                    created_at TIMESTAMP NOT NULL
                );
            """))

            # Indeksy wydajnościowe i współbieżnościowe
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_doc_tenant ON documents(tenant_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_attempts_status ON processing_attempts(status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_versions_doc ON extraction_versions(document_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_outbox_status ON outbox_events(status);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_erp_exports_idem ON erp_exports(idempotency_key);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_erp_events_doc ON erp_export_events(document_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_erp_events_idem ON erp_export_events(idempotency_key);")

    # =========================================================================
    # CYKL ŻYCIA DOKUMENTU I OUTBOX (KROK 1)
    # =========================================================================

    def create_document_and_attempt_atomically(
        self,
        tenant_id: str,
        operator_id: str,
        original_filename: str,
        storage_path: str,
        mime_type: str,
        sha256_hash: str,
        payload_meta: Optional[Dict[str, Any]] = None
    ) -> Tuple[Document, Optional[ProcessingAttempt], Optional[OutboxEvent]]:
        """
        KROK 1 Outbox Pattern:
        W pojedynczej transakcji SQL zapisuje:
        1. Rekord Document (status=RECEIVED)
        2. Rekord ProcessingAttempt (status=PENDING)
        3. Rekord OutboxEvent (status=PENDING)
        Gwarantuje unikalność (tenant_id, sha256_hash) przez UNIQUE constraint.
        """
        now = datetime.now(timezone.utc)
        doc_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        event_id = str(uuid.uuid4())

        payload = {
            "document_id": doc_id,
            "attempt_id": attempt_id,
            "tenant_id": tenant_id,
            "storage_path": storage_path,
            "mime_type": mime_type,
            **(payload_meta or {})
        }

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Sprawdzenie deduplikacji
            cursor.execute(
                self._format_sql("SELECT id FROM documents WHERE tenant_id = %s AND sha256_hash = %s"),
                (tenant_id, sha256_hash)
            )
            row = cursor.fetchone()
            if row:
                existing_doc_id = row[0] if isinstance(row, tuple) else row["id"]
                logger.info(f"Deduplikacja: dokument o sha256 {sha256_hash} istnieje już jako {existing_doc_id}")
                doc = self.get_document(existing_doc_id)
                # Pobieramy najnowszą próbę
                cursor.execute(
                    self._format_sql("""
                        SELECT id, document_id, task_id, attempt_number, status, worker_id, lease_token,
                               error_details, started_at, heartbeat_at, finished_at, created_at, updated_at
                        FROM processing_attempts
                        WHERE document_id = %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    """),
                    (existing_doc_id,)
                )
                att_row = cursor.fetchone()
                attempt = self._row_to_attempt(att_row) if att_row else None
                return doc, attempt, None

            # Zapis rekordu Document
            cursor.execute(
                self._format_sql("""
                    INSERT INTO documents (
                        id, tenant_id, original_filename, storage_path, mime_type,
                        sha256_hash, status, created_by_operator_id, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    doc_id, tenant_id, original_filename, storage_path, mime_type,
                    sha256_hash, DocumentStatus.RECEIVED.value, operator_id,
                    self._format_dt(now), self._format_dt(now)
                )
            )

            # Zapis rekordu ProcessingAttempt
            cursor.execute(
                self._format_sql("""
                    INSERT INTO processing_attempts (
                        id, document_id, task_id, attempt_number, status, created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    attempt_id, doc_id, f"celery-{attempt_id}", 1,
                    AttemptStatus.PENDING.value, self._format_dt(now), self._format_dt(now)
                )
            )

            # Zapis rekordu OutboxEvent
            cursor.execute(
                self._format_sql("""
                    INSERT INTO outbox_events (
                        id, attempt_id, aggregate_type, aggregate_id, event_type, payload, status, retry_count, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    event_id, attempt_id, "document", doc_id, "DOCUMENT_ENQUEUE",
                    json.dumps(payload), "PENDING", 0, self._format_dt(now)
                )
            )

        doc = Document(
            id=doc_id,
            tenant_id=tenant_id,
            original_filename=original_filename,
            storage_path=storage_path,
            mime_type=mime_type,
            sha256_hash=sha256_hash,
            status=DocumentStatus.RECEIVED,
            created_by_operator_id=operator_id,
            created_at=now,
            updated_at=now
        )
        attempt = ProcessingAttempt(
            id=attempt_id,
            document_id=doc_id,
            task_id=f"celery-{attempt_id}",
            attempt_number=1,
            status=AttemptStatus.PENDING,
            created_at=now
        )
        event = OutboxEvent(
            id=event_id,
            attempt_id=attempt_id,
            aggregate_type="document",
            aggregate_id=doc_id,
            event_type="DOCUMENT_ENQUEUE",
            payload=payload,
            status="PENDING",
            retry_count=0,
            created_at=now
        )
        return doc, attempt, event

    # =========================================================================
    # PRZEJMOWANIE ZADAŃ Z LEASINGIEM / FENCINGIEM (KROK 2)
    # =========================================================================

    def claim_attempt_atomically(self, attempt_id: str, worker_id: str) -> Optional[str]:
        """
        KROK 2 Atomowe przejęcie próby (Conditional UPDATE w SQL):
        UPDATE processing_attempts
        SET status='RUNNING', worker_id=%s, lease_token=%s, started_at=%s, heartbeat_at=%s, updated_at=%s
        WHERE id=%s AND status='PENDING';

        Zwraca wygenerowany lease_token w przypadku sukcesu, lub None gdy próba została już przejęta.
        """
        lease_token = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Atomowy conditional update
            cursor.execute(
                self._format_sql("""
                    UPDATE processing_attempts
                    SET status = %s,
                        worker_id = %s,
                        lease_token = %s,
                        started_at = %s,
                        heartbeat_at = %s,
                        updated_at = %s
                    WHERE id = %s AND status = %s
                """),
                (
                    AttemptStatus.RUNNING.value, worker_id, lease_token,
                    self._format_dt(now), self._format_dt(now), self._format_dt(now),
                    attempt_id, AttemptStatus.PENDING.value
                )
            )

            if cursor.rowcount == 0:
                logger.warning(f"Próba przejęcia zadania {attempt_id} odrzucona: brak rekordu lub status != PENDING")
                return None

            # Aktualizacja statusu dokumentu na EXTRACTED
            cursor.execute(
                self._format_sql("""
                    UPDATE documents
                    SET status = %s, updated_at = %s
                    WHERE id = (SELECT document_id FROM processing_attempts WHERE id = %s)
                """),
                (DocumentStatus.EXTRACTED.value, self._format_dt(now), attempt_id)
            )

            return lease_token

    def heartbeat_attempt(self, attempt_id: str, lease_token: str) -> bool:
        """Odświeżenie dzierżawy przez aktywnego workera posiadającego poprawny lease_token."""
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    UPDATE processing_attempts
                    SET heartbeat_at = %s, updated_at = %s
                    WHERE id = %s AND lease_token = %s AND status = %s
                """),
                (self._format_dt(now), self._format_dt(now), attempt_id, lease_token, AttemptStatus.RUNNING.value)
            )
            return cursor.rowcount > 0

    def save_extraction_version(
        self,
        attempt_id: str,
        lease_token: str,
        source_data: Optional[InvoiceSourceData],
        ai_suggestions: AISuggestions,
        validation_report: ValidationReport,
        source_type: SourceType = SourceType.KSEF_DETERMINISTIC,
        parent_version_id: Optional[str] = None,
        extraction_status: str = "SUCCESS",
        review_reason: Optional[str] = None
    ) -> Optional[ExtractionVersion]:
        """
        Fencing zapisu wyniku:
        Tylko worker posiadający aktualny lease_token może zamknąć próbę i zapisać wersję.
        Zapis odbywa się w jednej atomowej transakcji SQL.
        """
        now = datetime.now(timezone.utc)
        version_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Sprawdzenie i zamknięcie próby z weryfikacją fencing tokena
            cursor.execute(
                self._format_sql("""
                    UPDATE processing_attempts
                    SET status = %s, finished_at = %s, updated_at = %s
                    WHERE id = %s AND lease_token = %s AND status = %s
                """),
                (
                    AttemptStatus.COMPLETED.value, self._format_dt(now), self._format_dt(now),
                    attempt_id, lease_token, AttemptStatus.RUNNING.value
                )
            )

            if cursor.rowcount == 0:
                logger.error(f"Fencing violation: worker z tokenem {lease_token} nie ma ważnej dzierżawy dla próby {attempt_id}!")
                return None

            # 2. Pobranie document_id i kolejnego numeru wersji
            cursor.execute(
                self._format_sql("SELECT document_id FROM processing_attempts WHERE id = %s"),
                (attempt_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            doc_id = row[0] if isinstance(row, tuple) else row["document_id"]

            cursor.execute(
                self._format_sql("SELECT COUNT(*) FROM extraction_versions WHERE document_id = %s"),
                (doc_id,)
            )
            v_count_row = cursor.fetchone()
            v_count = v_count_row[0] if isinstance(v_count_row, tuple) else v_count_row[0]
            version_number = int(v_count) + 1

            # 3. Zapis ExtractionVersion
            source_data_json = source_data.model_dump_json() if source_data else None
            ai_sugg_json = ai_suggestions.model_dump_json() if ai_suggestions else None
            val_rep_json = validation_report.model_dump_json()

            cursor.execute(
                self._format_sql("""
                    INSERT INTO extraction_versions (
                        id, document_id, attempt_id, version_number, source_type,
                        parent_version_id, immutable_source_data, extraction_status,
                        review_reason, ai_suggestions, validation_report, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    version_id, doc_id, attempt_id, version_number, source_type.value,
                    parent_version_id, source_data_json, extraction_status,
                    review_reason, ai_sugg_json, val_rep_json, self._format_dt(now)
                )
            )

            # 4. Aktualizacja statusu dokumentu
            cursor.execute(
                self._format_sql("""
                    UPDATE documents
                    SET status = %s, updated_at = %s
                    WHERE id = %s
                """),
                (validation_report.status.value, self._format_dt(now), doc_id)
            )

        return ExtractionVersion(
            id=version_id,
            document_id=doc_id,
            attempt_id=attempt_id,
            version_number=version_number,
            source_type=source_type,
            parent_version_id=parent_version_id,
            immutable_source_data=source_data,
            extraction_status=extraction_status,
            review_reason=review_reason,
            ai_suggestions=ai_suggestions,
            validation_report=validation_report,
            created_at=now
        )

    def create_operator_correction(
        self,
        document_id: str,
        target_version_id: str,
        operator_id: str,
        corrected_source_data: InvoiceSourceData,
        validation_report: ValidationReport,
        notes: Optional[str] = None
    ) -> Optional[ExtractionVersion]:
        """
        Korekta operatora:
        Tworzy NOWĄ wersję ekstrakcji (v2, v3...) ze statusem OPERATOR_CORRECTED
        z powiązaniem do parent_version_id w trwałej bazie SQL.
        """
        now = datetime.now(timezone.utc)
        version_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Pobranie wersji rodzica
            parent_ver = self.get_version(document_id, target_version_id)
            if not parent_ver:
                logger.error(f"Nie znaleziono wersji bazowej {target_version_id} dla dokumentu {document_id}")
                return None

            cursor.execute(
                self._format_sql("SELECT COUNT(*) FROM extraction_versions WHERE document_id = %s"),
                (document_id,)
            )
            v_count_row = cursor.fetchone()
            v_count = v_count_row[0] if isinstance(v_count_row, tuple) else v_count_row[0]
            version_number = int(v_count) + 1

            source_data_json = corrected_source_data.model_dump_json()
            ai_sugg_json = parent_ver.ai_suggestions.model_dump_json() if parent_ver.ai_suggestions else None
            val_rep_json = validation_report.model_dump_json()

            cursor.execute(
                self._format_sql("""
                    INSERT INTO extraction_versions (
                        id, document_id, attempt_id, version_number, source_type,
                        parent_version_id, immutable_source_data, extraction_status,
                        review_reason, ai_suggestions, validation_report, created_by_operator_id, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    version_id, document_id, None, version_number, SourceType.OPERATOR_CORRECTED.value,
                    target_version_id, source_data_json, "SUCCESS",
                    None, ai_sugg_json, val_rep_json, operator_id, self._format_dt(now)
                )
            )

            # Aktualizacja statusu dokumentu
            cursor.execute(
                self._format_sql("UPDATE documents SET status = %s, updated_at = %s WHERE id = %s"),
                (validation_report.status.value, self._format_dt(now), document_id)
            )

        return ExtractionVersion(
            id=version_id,
            document_id=document_id,
            attempt_id=None,
            version_number=version_number,
            source_type=SourceType.OPERATOR_CORRECTED,
            parent_version_id=target_version_id,
            immutable_source_data=corrected_source_data,
            ai_suggestions=parent_ver.ai_suggestions,
            validation_report=validation_report,
            created_by_operator_id=operator_id,
            created_at=now
        )

    def approve_document_version(
        self,
        document_id: str,
        version_id: str,
        operator_id: str,
        notes: Optional[str] = None
    ) -> bool:
        """
        Zatwierdzenie dokumentu:
        Powiązane ŚCIŚLE ze wskazanym version_id!
        Aktualizuje doc.approved_version_id = version_id oraz doc.status = APPROVED.
        Zapisuje rekord w operator_reviews.
        """
        now = datetime.now(timezone.utc)
        review_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Weryfikacja czy wersja istnieje dla tego dokumentu
            cursor.execute(
                self._format_sql("SELECT id FROM extraction_versions WHERE id = %s AND document_id = %s"),
                (version_id, document_id)
            )
            if not cursor.fetchone():
                logger.error(f"Nie można zatwierdzić: wersja {version_id} nie istnieje dla dokumentu {document_id}")
                return False

            cursor.execute(
                self._format_sql("""
                    UPDATE documents
                    SET approved_version_id = %s, status = %s, updated_at = %s
                    WHERE id = %s
                """),
                (version_id, DocumentStatus.APPROVED.value, self._format_dt(now), document_id)
            )

            cursor.execute(
                self._format_sql("""
                    INSERT INTO operator_reviews (
                        id, document_id, target_version_id, operator_id, decision, notes, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """),
                (review_id, document_id, version_id, operator_id, DocumentStatus.APPROVED.value, notes, self._format_dt(now))
            )

            return True

    def reject_document(self, document_id: str, operator_id: str, reason: str) -> bool:
        """
        Odrzucenie dokumentu:
        Zmienia status na REJECTED. Fizyczny plik NIE jest kasowany z magazynu.
        """
        now = datetime.now(timezone.utc)
        review_id = str(uuid.uuid4())

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("SELECT approved_version_id FROM documents WHERE id = %s" + (" FOR UPDATE" if self.engine_type == "postgres" else "")),
                (document_id,)
            )
            row = cursor.fetchone()
            if not row:
                return False
            approved_v = row[0] if isinstance(row, tuple) else row["approved_version_id"]
            cursor.execute(self._format_sql("SELECT id FROM erp_exports WHERE document_id=%s AND status IN ('EXPORT_IN_FLIGHT', 'EXPORT_UNKNOWN', 'EXPORT_TRANSMITTED')"), (document_id,))
            if cursor.fetchone():
                raise ERPExportInFlightError("Najpierw uzgodnij rozpoczęty eksport; odrzucenie dokumentu jest zablokowane.")

            cursor.execute(
                self._format_sql("UPDATE documents SET status = %s, approved_version_id = NULL, updated_at = %s WHERE id = %s"),
                (DocumentStatus.REJECTED.value, self._format_dt(now), document_id)
            )

            cursor.execute(
                self._format_sql("""
                    INSERT INTO operator_reviews (
                        id, document_id, target_version_id, operator_id, decision, notes, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """),
                (review_id, document_id, approved_v or "none", operator_id, DocumentStatus.REJECTED.value, reason, self._format_dt(now))
            )
            return True

    # =========================================================================
    # 3-TABELOWY MODEL EKSPORTU ERP (erp_exports, erp_export_attempts, erp_export_events)
    # =========================================================================

    def record_erp_export_step(self, event: ERPExportEvent) -> None:
        """
        Rejestruje krok eksportu ERP:
        1. Zapisuje w erp_export_events (pełny dziennik zdarzeń).
        2. Aktualizuje lub tworzy nadrzędny rekord w erp_exports.
        """
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Zapis do erp_export_events
            cursor.execute(
                self._format_sql("""
                    INSERT INTO erp_export_events (
                        id, document_id, extraction_version_id, erp_system,
                        idempotency_key, status, erp_reference_id, error_message, payload, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    event.id, event.document_id, event.extraction_version_id, event.erp_system,
                    event.idempotency_key, event.status.value, event.erp_reference_id,
                    event.error_message, json.dumps(event.payload or {}), self._format_dt(event.timestamp)
                )
            )

            # 2. Upsert w tabeli erp_exports
            cursor.execute(
                self._format_sql("SELECT id FROM erp_exports WHERE idempotency_key = %s"),
                (event.idempotency_key,)
            )
            existing_export = cursor.fetchone()
            if existing_export:
                cursor.execute(
                    self._format_sql("""
                        UPDATE erp_exports
                        SET status = %s,
                            erp_reference_id = COALESCE(%s, erp_reference_id),
                            updated_at = %s
                        WHERE idempotency_key = %s
                    """),
                    (event.status.value, event.erp_reference_id, self._format_dt(now), event.idempotency_key)
                )
            else:
                export_id = str(uuid.uuid4())
                cursor.execute(
                    self._format_sql("""
                        INSERT INTO erp_exports (
                            id, document_id, extraction_version_id, erp_system,
                            idempotency_key, status, erp_reference_id, created_at, updated_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """),
                    (
                        export_id, event.document_id, event.extraction_version_id, event.erp_system,
                        event.idempotency_key, event.status.value, event.erp_reference_id,
                        self._format_dt(now), self._format_dt(now)
                    )
                )

    def record_erp_export_attempt(
        self,
        erp_export_id: str,
        attempt_number: int,
        status: str,
        http_status: Optional[int] = None,
        request_payload: Optional[Dict[str, Any]] = None,
        response_payload: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None
    ) -> ERPExportAttempt:
        """Zapisuje pojedynczą próbę sieciową w tabeli erp_export_attempts."""
        att_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        req_json = json.dumps(request_payload or {})
        resp_json = json.dumps(response_payload or {})

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    INSERT INTO erp_export_attempts (
                        id, erp_export_id, attempt_number, status, http_status,
                        request_payload, response_payload, error_message, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    att_id, erp_export_id, attempt_number, status, http_status,
                    req_json, resp_json, error_message, self._format_dt(now)
                )
            )

        return ERPExportAttempt(
            id=att_id,
            erp_export_id=erp_export_id,
            attempt_number=attempt_number,
            status=status,
            http_status=http_status,
            request_payload=request_payload or {},
            response_payload=response_payload or {},
            error_message=error_message,
            created_at=now
        )

    def get_latest_erp_export(self, idempotency_key: str) -> Optional[ERPExportEvent]:
        """Zwraca najnowsze zdarzenie eksportu dla danego klucza idempotencji."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, extraction_version_id, erp_system,
                           idempotency_key, status, erp_reference_id, error_message, payload, created_at
                    FROM erp_export_events
                    WHERE idempotency_key = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                """),
                (idempotency_key,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_erp_event(row)

    def get_erp_events_for_document(self, document_id: str) -> List[ERPExportEvent]:
        """Zwraca listę wszystkich kroków eksportu dla wskazanego dokumentu."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, extraction_version_id, erp_system,
                           idempotency_key, status, erp_reference_id, error_message, payload, created_at
                    FROM erp_export_events
                    WHERE document_id = %s
                    ORDER BY created_at ASC
                """),
                (document_id,)
            )
            return [self._row_to_erp_event(r) for r in cursor.fetchall()]

    def reserve_erp_export_atomic(
        self,
        document_id: str,
        version_id: str,
        idempotency_key: str,
        erp_system: str
    ) -> Tuple[str, ExportStatus, Optional[str]]:
        """
        Atomowa rezerwacja eksportu ERP (w jednej transakcji SQL).
        Zwraca: (export_id, current_status, erp_reference_id)

        Zabezpieczenia współbieżności:
        1. Dokument musi mieć status APPROVED oraz approved_version_id == version_id.
           Jeśli dokument został odrzucony (status REJECTED lub approved_version_id is NULL) -> DocumentNotApprovedError.
        2. Sprawdzenie rekordu w erp_exports pod kątem idempotency_key:
           - Jeśli EXPORT_CONFIRMED -> zwraca (export_id, EXPORT_CONFIRMED, erp_reference_id).
           - Jeśli EXPORT_IN_FLIGHT -> rzuca ERPExportInFlightError.
           - Jeśli EXPORT_UNKNOWN lub EXPORT_TRANSMITTED -> rzuca ERPExportUnknownError.
           - Jeśli EXPORT_FAILED lub EXPORT_GENERATED -> atomowy, warunkowy UPDATE ze sprawdzeniem rowcount.
           - Jeśli brak -> INSERT INTO erp_exports ze statusem EXPORT_IN_FLIGHT oraz obsługą IntegrityError na UNIQUE(idempotency_key).
        3. W PostgreSQL stosuje blokowanie wiersza FOR UPDATE.
        4. Rejestruje zdarzenie w erp_export_events.
        """
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # 1. Sprawdzenie statusu dokumentu
            cursor.execute(
                self._format_sql("SELECT status, approved_version_id FROM documents WHERE id = %s" + (" FOR UPDATE" if self.engine_type == "postgres" else "")),
                (document_id,)
            )
            doc_row = cursor.fetchone()
            if not doc_row:
                raise KeyError(f"Dokument {document_id} nie istnieje.")

            doc_status = doc_row[0] if isinstance(doc_row, tuple) else doc_row["status"]
            approved_v = doc_row[1] if isinstance(doc_row, tuple) else doc_row["approved_version_id"]

            if doc_status != DocumentStatus.APPROVED.value:
                raise DocumentNotApprovedError(
                    f"Dokument nie posiada statusu APPROVED (aktualny status: {doc_status}). Eksport jest niedozwolony."
                )
            if approved_v != version_id:
                raise VersionNotApprovedError(
                    f"Wersja {version_id} nie jest wersją zatwierdzoną dokumentu (aktualnie zatwierdzona: {approved_v})."
                )

            # 2. Sprawdzenie tabeli erp_exports (z blokowaniem wiersza w Postgresie)
            select_sql = "SELECT id, status, erp_reference_id FROM erp_exports WHERE idempotency_key = %s"
            if self.engine_type == "postgres":
                select_sql += " FOR UPDATE"

            cursor.execute(self._format_sql(select_sql), (idempotency_key,))
            exp_row = cursor.fetchone()

            if exp_row:
                export_id = exp_row[0] if isinstance(exp_row, tuple) else exp_row["id"]
                current_st = exp_row[1] if isinstance(exp_row, tuple) else exp_row["status"]
                ref_id = exp_row[2] if isinstance(exp_row, tuple) else exp_row["erp_reference_id"]

                if current_st == ExportStatus.EXPORT_CONFIRMED.value:
                    return (export_id, ExportStatus.EXPORT_CONFIRMED, ref_id)

                if current_st in (ExportStatus.EXPORT_IN_FLIGHT.value, ExportStatus.EXPORT_PENDING.value):
                    raise ERPExportInFlightError(
                        "Eksport dla tego dokumentu i wersji jest aktualnie realizowany przez inny proces (IN_FLIGHT)."
                    )

                if current_st in (ExportStatus.EXPORT_UNKNOWN.value, ExportStatus.EXPORT_TRANSMITTED.value):
                    raise ERPExportUnknownError(
                        "Wykryto wcześniejszą próbę eksportu w stanie niepotwierdzonym (EXPORT_UNKNOWN/TRANSMITTED). "
                        "Wymagane formalne uzgodnienie stanu przez audytora przed ponowieniem."
                    )

                # Warunkowy UPDATE eliminujący wyścig przy ponowieniu FAILED/GENERATED
                cursor.execute(
                    self._format_sql("""
                        UPDATE erp_exports
                        SET status = %s, updated_at = %s
                        WHERE id = %s AND status IN (%s, %s)
                    """),
                    (
                        ExportStatus.EXPORT_IN_FLIGHT.value,
                        self._format_dt(now),
                        export_id,
                        ExportStatus.EXPORT_FAILED.value,
                        ExportStatus.EXPORT_GENERATED.value,
                    )
                )
                if cursor.rowcount == 0:
                    raise ERPExportInFlightError(
                        "Eksport dla tego dokumentu i wersji jest aktualnie realizowany przez inny proces (IN_FLIGHT)."
                    )
            else:
                export_id = str(uuid.uuid4())
                try:
                    cursor.execute(
                        self._format_sql("""
                            INSERT INTO erp_exports (
                                id, document_id, extraction_version_id, erp_system,
                                idempotency_key, status, erp_reference_id, created_at, updated_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """),
                        (
                            export_id, document_id, version_id, erp_system,
                            idempotency_key, ExportStatus.EXPORT_IN_FLIGHT.value, None,
                            self._format_dt(now), self._format_dt(now)
                        )
                    )
                except Exception as insert_err:
                    err_str = str(insert_err).lower()
                    if "unique" in err_str or "duplicate" in err_str or "integrity" in err_str:
                        raise ERPExportInFlightError(
                            "Eksport dla tego dokumentu i wersji jest aktualnie realizowany przez inny proces (IN_FLIGHT)."
                        ) from insert_err
                    raise

            # Rejestracja zdarzenia w erp_export_events
            ev_id = str(uuid.uuid4())
            cursor.execute(
                self._format_sql("""
                    INSERT INTO erp_export_events (
                        id, document_id, extraction_version_id, erp_system,
                        idempotency_key, status, erp_reference_id, error_message, payload, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """),
                (
                    ev_id, document_id, version_id, erp_system,
                    idempotency_key, ExportStatus.EXPORT_IN_FLIGHT.value, None,
                    None, json.dumps({"stage": "RESERVED_IN_FLIGHT"}), self._format_dt(now)
                )
            )

            return (export_id, ExportStatus.EXPORT_IN_FLIGHT, None)

    def record_erp_export_result(
        self,
        export_id: str,
        status: ExportStatus,
        erp_reference_id: Optional[str] = None,
        error_message: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Zapisuje autentyczny wynik transmisji ERP (EXPORT_CONFIRMED, EXPORT_TRANSMITTED, itp.).
        Zachowuje wierność stanu: zrzut pliku do katalogu wymiany Optimy otrzymuje status EXPORT_TRANSMITTED,
        a nie pozorowany EXPORT_CONFIRMED.
        """
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    UPDATE erp_exports
                    SET status = %s, erp_reference_id = COALESCE(%s, erp_reference_id), updated_at = %s
                    WHERE id = %s
                """),
                (status.value, erp_reference_id, self._format_dt(now), export_id)
            )
            cursor.execute(
                self._format_sql("SELECT document_id, extraction_version_id, erp_system, idempotency_key FROM erp_exports WHERE id = %s"),
                (export_id,)
            )
            row = cursor.fetchone()
            if row:
                d_id = row[0] if isinstance(row, tuple) else row["document_id"]
                v_id = row[1] if isinstance(row, tuple) else row["extraction_version_id"]
                erp_sys = row[2] if isinstance(row, tuple) else row["erp_system"]
                idem = row[3] if isinstance(row, tuple) else row["idempotency_key"]
                cursor.execute(
                    self._format_sql("""
                        INSERT INTO erp_export_events (
                            id, document_id, extraction_version_id, erp_system,
                            idempotency_key, status, erp_reference_id, error_message, payload, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """),
                    (
                        str(uuid.uuid4()), d_id, v_id, erp_sys, idem,
                        status.value, erp_reference_id, error_message,
                        json.dumps(payload or {"stage": status.value}), self._format_dt(now)
                    )
                )

    def confirm_erp_export(self, export_id: str, erp_reference_id: str) -> None:
        self.record_erp_export_result(export_id, ExportStatus.EXPORT_CONFIRMED, erp_reference_id)

    def fail_erp_export(self, export_id: str, error_message: str) -> None:
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    UPDATE erp_exports
                    SET status = %s, updated_at = %s
                    WHERE id = %s
                """),
                (ExportStatus.EXPORT_FAILED.value, self._format_dt(now), export_id)
            )
            cursor.execute(
                self._format_sql("SELECT document_id, extraction_version_id, erp_system, idempotency_key FROM erp_exports WHERE id = %s"),
                (export_id,)
            )
            row = cursor.fetchone()
            if row:
                d_id = row[0] if isinstance(row, tuple) else row["document_id"]
                v_id = row[1] if isinstance(row, tuple) else row["extraction_version_id"]
                erp_sys = row[2] if isinstance(row, tuple) else row["erp_system"]
                idem = row[3] if isinstance(row, tuple) else row["idempotency_key"]
                cursor.execute(
                    self._format_sql("""
                        INSERT INTO erp_export_events (
                            id, document_id, extraction_version_id, erp_system,
                            idempotency_key, status, erp_reference_id, error_message, payload, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """),
                    (
                        str(uuid.uuid4()), d_id, v_id, erp_sys, idem,
                        ExportStatus.EXPORT_FAILED.value, None, error_message[:1000],
                        json.dumps({"stage": "FAILED"}), self._format_dt(now)
                    )
                )

    def set_erp_export_unknown(self, export_id: str, error_message: str) -> None:
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    UPDATE erp_exports
                    SET status = %s, updated_at = %s
                    WHERE id = %s
                """),
                (ExportStatus.EXPORT_UNKNOWN.value, self._format_dt(now), export_id)
            )
            cursor.execute(
                self._format_sql("SELECT document_id, extraction_version_id, erp_system, idempotency_key FROM erp_exports WHERE id = %s"),
                (export_id,)
            )
            row = cursor.fetchone()
            if row:
                d_id = row[0] if isinstance(row, tuple) else row["document_id"]
                v_id = row[1] if isinstance(row, tuple) else row["extraction_version_id"]
                erp_sys = row[2] if isinstance(row, tuple) else row["erp_system"]
                idem = row[3] if isinstance(row, tuple) else row["idempotency_key"]
                cursor.execute(
                    self._format_sql("""
                        INSERT INTO erp_export_events (
                            id, document_id, extraction_version_id, erp_system,
                            idempotency_key, status, erp_reference_id, error_message, payload, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """),
                    (
                        str(uuid.uuid4()), d_id, v_id, erp_sys, idem,
                        ExportStatus.EXPORT_UNKNOWN.value, None, error_message[:1000],
                        json.dumps({"stage": "UNKNOWN_TIMEOUT"}), self._format_dt(now)
                    )
                )

    def sweep_stale_erp_exports(self, timeout_seconds: int = 60) -> List[str]:
        """
        Wyszukuje eksporty porzucone w stanie EXPORT_IN_FLIGHT dłużej niż timeout_seconds
        (np. z powodu nagłej awarii procesu) i przestawia je na EXPORT_UNKNOWN.
        W JEDNEJ transakcji aktualizuje tabelę erp_exports ORAZ rejestruje zdarzenie w erp_export_events,
        gwarantując, że endpoint uzgodnienia (erp-reconcile) natychmiast zobaczy ten stan.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=timeout_seconds)
        stale_ids = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, extraction_version_id, erp_system, idempotency_key
                    FROM erp_exports
                    WHERE status = %s AND updated_at < %s
                """),
                (ExportStatus.EXPORT_IN_FLIGHT.value, self._format_dt(cutoff))
            )
            rows = cursor.fetchall()
            for r in rows:
                eid = r[0] if isinstance(r, tuple) else r["id"]
                doc_id = r[1] if isinstance(r, tuple) else r["document_id"]
                ver_id = r[2] if isinstance(r, tuple) else r["extraction_version_id"]
                erp_sys = r[3] if isinstance(r, tuple) else r["erp_system"]
                idem = r[4] if isinstance(r, tuple) else r["idempotency_key"]

                cursor.execute(
                    self._format_sql("UPDATE erp_exports SET status = %s, updated_at = %s WHERE id = %s AND status = %s"),
                    (ExportStatus.EXPORT_UNKNOWN.value, self._format_dt(now), eid, ExportStatus.EXPORT_IN_FLIGHT.value)
                )
                if cursor.rowcount > 0:
                    cursor.execute(
                        self._format_sql("""
                            INSERT INTO erp_export_events (
                                id, document_id, extraction_version_id, erp_system,
                                idempotency_key, status, erp_reference_id, error_message, payload, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """),
                        (
                            str(uuid.uuid4()), doc_id, ver_id, erp_sys, idem,
                            ExportStatus.EXPORT_UNKNOWN.value, None,
                            "Sweeper ERP: Wykryto porzuconą sesję IN_FLIGHT (timeout procesu). Przestawiono na EXPORT_UNKNOWN.",
                            json.dumps({"stage": "SWEEPER_TIMEOUT_UNKNOWN"}), self._format_dt(now)
                        )
                    )
                    logger.warning(f"Sweeper ERP: Eksport {eid} utknął w EXPORT_IN_FLIGHT. Przestawiono na EXPORT_UNKNOWN.")
                    stale_ids.append(eid)
        return stale_ids

    # =========================================================================
    # DYSPOZYTOR OUTBOXA I SWEEPER
    # =========================================================================

    def heartbeat_attempt(self, attempt_id: str, lease_token: str) -> bool:
        """Odświeża timestamp heartbeat_at aktywnej próby, chroniąc przed przejęciem przez sweeper."""
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    UPDATE processing_attempts
                    SET heartbeat_at = %s, updated_at = %s
                    WHERE id = %s AND lease_token = %s AND status = %s
                """),
                (self._format_dt(now), self._format_dt(now), attempt_id, lease_token, AttemptStatus.RUNNING.value)
            )
            return cursor.rowcount > 0

    def get_pending_outbox_events(self, limit: int = 50) -> List[OutboxEvent]:
        """Pobiera nieprzetworzone zdarzenia outboxa (PENDING lub RETRY z minionym next_retry_at)."""
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, attempt_id, aggregate_type, aggregate_id, event_type, payload, status, retry_count, created_at, sent_at,
                           max_retries, next_retry_at, last_error
                    FROM outbox_events
                    WHERE status = 'PENDING' OR (status = 'RETRY' AND retry_count < max_retries AND (next_retry_at IS NULL OR next_retry_at <= %s))
                    ORDER BY created_at ASC
                    LIMIT %s
                """),
                (self._format_dt(now), limit)
            )
            return [self._row_to_outbox_event(r) for r in cursor.fetchall()]

    def mark_outbox_event_sent(self, event_id: str) -> None:
        """Oznacza zdarzenie outboxa jako pomyślnie wysłane (SENT)."""
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("UPDATE outbox_events SET status = 'SENT', sent_at = %s WHERE id = %s"),
                (self._format_dt(now), event_id)
            )

    def mark_outbox_event_failed(self, event_id: str, error: str, max_retries: Optional[int] = None) -> None:
        """
        Obsługa błędu wysyłki outbox:
        Wylicza exponential backoff (2^retry_count sekund).
        Jeśli retry_count + 1 >= max_retries, ustawia status FAILED.
        W przeciwnym razie ustawia status RETRY z wyliczonym next_retry_at.
        """
        now = datetime.now(timezone.utc)
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("SELECT retry_count, max_retries FROM outbox_events WHERE id = %s"),
                (event_id,)
            )
            row = cursor.fetchone()
            curr_retries = (row[0] if isinstance(row, tuple) else row["retry_count"]) if row else 0
            if max_retries is not None:
                m_retries = max_retries
            else:
                m_retries = (row[1] if isinstance(row, tuple) else row["max_retries"]) if (row and len(row) > 1 and row[1] is not None) else 5

            new_retries = curr_retries + 1
            if new_retries >= m_retries:
                cursor.execute(
                    self._format_sql("UPDATE outbox_events SET status = 'FAILED', retry_count = %s, max_retries = %s, last_error = %s WHERE id = %s"),
                    (new_retries, m_retries, error[:1000], event_id)
                )
            else:
                backoff_sec = 2 ** curr_retries
                next_time = now + timedelta(seconds=backoff_sec)
                cursor.execute(
                    self._format_sql("UPDATE outbox_events SET status = 'RETRY', retry_count = %s, max_retries = %s, next_retry_at = %s, last_error = %s WHERE id = %s"),
                    (new_retries, m_retries, self._format_dt(next_time), error[:1000], event_id)
                )

    def dispatch_outbox_events(self, batch_size: int = 50) -> int:
        """
        Dyspozytor outboxa:
        Pobiera zdarzenia PENDING lub gotowe RETRY, wysyła je do brokera Celery i oznacza jako SENT.
        """
        events = self.get_pending_outbox_events(limit=batch_size)
        dispatched_count = 0
        for ev in events:
            try:
                # Dispatch do brokera (np. Celery task)
                from tasks import process_invoice_task
                p = ev.payload
                process_invoice_task.apply_async(
                    args=[p.get("attempt_id"), p.get("storage_path"), p.get("tenant_id")],
                    task_id=f"celery-{p.get('attempt_id')}"
                )
                self.mark_outbox_event_sent(ev.id)
                dispatched_count += 1
            except Exception as exc:
                logger.error(f"Błąd wysyłki zdarzenia outbox {ev.id}: {exc}")
                self.mark_outbox_event_failed(ev.id, str(exc))

        return dispatched_count

    def _finish_attempt(self, cursor, attempt_id, lease_token, error, retryable):
        """Called under a transaction; invalidate the old lease before requeueing."""
        now = datetime.now(timezone.utc)
        sql = "SELECT a.document_id, e.id, e.retry_count, e.max_retries FROM processing_attempts a JOIN outbox_events e ON e.attempt_id=a.id WHERE a.id=%s"
        if self.engine_type == "postgres":
            sql += " FOR UPDATE OF a, e"
        cursor.execute(self._format_sql(sql), (attempt_id,))
        row = cursor.fetchone()
        if not row:
            return False
        doc_id, event_id, retries, maximum = tuple(row)
        retry = retryable and retries + 1 < maximum
        cursor.execute(self._format_sql("""
            UPDATE processing_attempts SET status=%s, lease_token=NULL, worker_id=NULL,
                error_details=%s, finished_at=%s, updated_at=%s
            WHERE id=%s AND status=%s AND lease_token=%s
        """), ("PENDING" if retry else "FAILED", json.dumps({"reason": error}),
                 None if retry else self._format_dt(now), self._format_dt(now),
                 attempt_id, "RUNNING", lease_token))
        if cursor.rowcount != 1:
            return False
        cursor.execute(self._format_sql("UPDATE outbox_events SET status=%s, retry_count=%s, next_retry_at=%s, last_error=%s WHERE id=%s"),
                       ("RETRY" if retry else "FAILED", retries + 1,
                        self._format_dt(now + timedelta(seconds=min(300, 2 ** min(retries, 8)))) if retry else None,
                        error, event_id))
        if not retry:
            cursor.execute(self._format_sql("UPDATE documents SET status=%s, updated_at=%s WHERE id=%s"),
                           (DocumentStatus.REQUIRES_REVIEW.value, self._format_dt(now), doc_id))
        return True

    def fail_attempt(self, attempt_id, lease_token, error, retryable=False):
        with self.get_connection() as conn:
            return self._finish_attempt(conn.cursor(), attempt_id, lease_token, error, retryable)

    def recover_stuck_attempts(self, timeout_seconds: int = 60) -> List[str]:
        now = datetime.now(timezone.utc)
        cutoff = self._format_dt(now - timedelta(seconds=timeout_seconds))
        recovered = []
        with self.get_connection() as conn:
            cursor = conn.cursor()
            sql = "SELECT id, lease_token FROM processing_attempts WHERE status='RUNNING' AND COALESCE(heartbeat_at, started_at)<%s"
            if self.engine_type == "postgres":
                sql += " FOR UPDATE SKIP LOCKED"
            cursor.execute(self._format_sql(sql), (cutoff,))
            for row in cursor.fetchall():
                if self._finish_attempt(cursor, row[0], row[1], "WORKER_LEASE_EXPIRED", True):
                    recovered.append(row[0])
            # Use the last publication time, not the age of the document.
            sql = """SELECT a.id, a.document_id, e.id, e.retry_count, e.max_retries
                FROM processing_attempts a JOIN outbox_events e ON e.attempt_id=a.id
                WHERE a.status='PENDING' AND e.status='SENT' AND e.sent_at<%s"""
            if self.engine_type == "postgres":
                sql += " FOR UPDATE OF a, e SKIP LOCKED"
            cursor.execute(self._format_sql(sql), (cutoff,))
            for att_id, doc_id, ev_id, retries, maximum in cursor.fetchall():
                retry = retries + 1 < maximum
                cursor.execute(self._format_sql("UPDATE outbox_events SET status=%s, retry_count=%s, next_retry_at=%s, last_error=%s WHERE id=%s AND status='SENT'"),
                    ("RETRY" if retry else "FAILED", retries + 1,
                     self._format_dt(now + timedelta(seconds=min(300, 2 ** min(retries, 8)))) if retry else None,
                     "MESSAGE_NOT_CONSUMED", ev_id))
                if not retry:
                    cursor.execute(self._format_sql("UPDATE processing_attempts SET status='FAILED', finished_at=%s, error_details=%s WHERE id=%s"),
                        (self._format_dt(now), json.dumps({"reason": "DELIVERY_RETRIES_EXHAUSTED"}), att_id))
                    cursor.execute(self._format_sql("UPDATE documents SET status=%s WHERE id=%s"),
                        (DocumentStatus.REQUIRES_REVIEW.value, doc_id))
                recovered.append(att_id)
        return recovered

    def sweep_stale_leases(self, timeout_seconds: int = 60) -> List[str]:
        """Alias dla procesu Sweepera."""
        return self.recover_stuck_attempts(timeout_seconds)

    # =========================================================================
    # METODY ODCZYTU (GETTERS)
    # =========================================================================

    def get_document(self, document_id: str) -> Optional[Document]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, tenant_id, original_filename, storage_path, mime_type,
                           sha256_hash, status, approved_version_id, created_by_operator_id, created_at, updated_at
                    FROM documents
                    WHERE id = %s
                """),
                (document_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_document(row)

    def get_versions(self, document_id: str) -> List[ExtractionVersion]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, attempt_id, version_number, source_type,
                           parent_version_id, immutable_source_data, extraction_status,
                           review_reason, ai_suggestions, validation_report, created_by_operator_id, created_at
                    FROM extraction_versions
                    WHERE document_id = %s
                    ORDER BY version_number ASC
                """),
                (document_id,)
            )
            return [self._row_to_version(r) for r in cursor.fetchall()]

    def get_version(self, document_id: str, version_id: str) -> Optional[ExtractionVersion]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, attempt_id, version_number, source_type,
                           parent_version_id, immutable_source_data, extraction_status,
                           review_reason, ai_suggestions, validation_report, created_by_operator_id, created_at
                    FROM extraction_versions
                    WHERE document_id = %s AND id = %s
                """),
                (document_id, version_id)
            )
            row = cursor.fetchone()
            if not row:
                return None
            return self._row_to_version(row)

    def get_attempt(self, attempt_id: str) -> Optional[ProcessingAttempt]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, task_id, attempt_number, status, worker_id, lease_token,
                           error_details, started_at, heartbeat_at, finished_at, created_at, updated_at
                    FROM processing_attempts
                    WHERE id = %s
                """),
                (attempt_id,)
            )
            row = cursor.fetchone()
            return self._row_to_attempt(row) if row else None

    def get_all_attempts(self) -> List[ProcessingAttempt]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, document_id, task_id, attempt_number, status, worker_id, lease_token,
                           error_details, started_at, heartbeat_at, finished_at, created_at, updated_at
                    FROM processing_attempts
                """)
            )
            return [self._row_to_attempt(r) for r in cursor.fetchall()]

    def get_all_documents(self) -> List[Document]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, tenant_id, original_filename, storage_path, mime_type,
                           sha256_hash, status, approved_version_id, created_by_operator_id, created_at, updated_at
                    FROM documents
                """)
            )
            return [self._row_to_document(r) for r in cursor.fetchall()]

    def get_outbox_event(self, event_id: str) -> Optional[OutboxEvent]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, attempt_id, aggregate_type, aggregate_id, event_type, payload, status, retry_count, created_at, sent_at,
                           max_retries, next_retry_at, last_error
                    FROM outbox_events
                    WHERE id = %s
                """),
                (event_id,)
            )
            row = cursor.fetchone()
            return self._row_to_outbox_event(row) if row else None

    def get_all_outbox_events(self) -> List[OutboxEvent]:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                self._format_sql("""
                    SELECT id, attempt_id, aggregate_type, aggregate_id, event_type, payload, status, retry_count, created_at, sent_at,
                           max_retries, next_retry_at, last_error
                    FROM outbox_events
                """)
            )
            return [self._row_to_outbox_event(r) for r in cursor.fetchall()]

    def reset_database(self) -> None:
        """
        Czyści wszystkie tabele bazy danych (wykorzystywane WYŁĄCZNIE w testach).
        Twardo odmawia czyszczenia jakiejkolwiek bazy produkcyjnej/aplikacyjnej!
        """
        if os.getenv("KARIK_ALLOW_DB_RESET") != "1":
            raise RuntimeError(
                "KRYTYCZNE ZABEZPIECZENIE: repository.reset_database() zostało zablokowane. "
                "Wymagane jest jawne ustawienie zmiennej KARIK_ALLOW_DB_RESET=1 oraz dedykowanej bazy testowej."
            )
        if self.engine_type == "postgres":
            db_name = (self.postgres_db or "").lower()
            if not (db_name.endswith("_test") or db_name.endswith("-test") or "test" in db_name):
                raise RuntimeError(
                    f"KRYTYCZNE ZABEZPIECZENIE: Odmowa zresetowania produkcyjnej bazy PostgreSQL '{self.postgres_db}'! "
                    "reset_database() dozwolony jest wyłącznie dla dedykowanych baz testowych (np. karik_db_test)."
                )

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM erp_export_attempts;")
            cursor.execute("DELETE FROM erp_export_events;")
            cursor.execute("DELETE FROM erp_exports;")
            cursor.execute("DELETE FROM outbox_events;")
            cursor.execute("DELETE FROM operator_reviews;")
            cursor.execute("DELETE FROM extraction_versions;")
            cursor.execute("DELETE FROM processing_attempts;")
            cursor.execute("DELETE FROM documents;")

    @property
    def _documents(self):
        return _DocumentsProxy(self)

    @property
    def _attempts(self):
        return _AttemptsProxy(self)

    @property
    def _outbox_events(self):
        return _OutboxEventsProxy(self)

    @property
    def _versions(self):
        return _VersionsProxy(self)

    @property
    def _erp_events(self):
        return _ERPEventsProxy(self)

    # =========================================================================
    # PARSERY WIERSZY SQL -> PYDANTIC MODELE
    # =========================================================================

    def _row_to_document(self, r: Any) -> Document:
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "tenant_id": r[1], "original_filename": r[2], "storage_path": r[3],
            "mime_type": r[4], "sha256_hash": r[5], "status": r[6], "approved_version_id": r[7],
            "created_by_operator_id": r[8], "created_at": r[9], "updated_at": r[10]
        }
        return Document(
            id=d["id"],
            tenant_id=d["tenant_id"],
            original_filename=d["original_filename"],
            storage_path=d["storage_path"],
            mime_type=d["mime_type"],
            sha256_hash=d["sha256_hash"],
            status=DocumentStatus(d["status"]),
            approved_version_id=d["approved_version_id"],
            created_by_operator_id=d["created_by_operator_id"],
            created_at=_parse_dt(d["created_at"]) or datetime.now(timezone.utc),
            updated_at=_parse_dt(d["updated_at"]) or datetime.now(timezone.utc)
        )

    def _row_to_attempt(self, r: Any) -> ProcessingAttempt:
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "document_id": r[1], "task_id": r[2], "attempt_number": r[3],
            "status": r[4], "worker_id": r[5], "lease_token": r[6], "error_details": r[7],
            "started_at": r[8], "heartbeat_at": r[9], "finished_at": r[10],
            "created_at": r[11], "updated_at": r[12]
        }
        return ProcessingAttempt(
            id=d["id"],
            document_id=d["document_id"],
            task_id=d["task_id"],
            attempt_number=d["attempt_number"],
            status=AttemptStatus(d["status"]),
            worker_id=d.get("worker_id"),
            lease_token=d.get("lease_token"),
            error_details=_parse_json(d.get("error_details")),
            started_at=_parse_dt(d.get("started_at")),
            heartbeat_at=_parse_dt(d.get("heartbeat_at")),
            finished_at=_parse_dt(d.get("finished_at")),
            created_at=_parse_dt(d["created_at"]) or datetime.now(timezone.utc)
        )

    def _row_to_version(self, r: Any) -> ExtractionVersion:
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "document_id": r[1], "attempt_id": r[2], "version_number": r[3],
            "source_type": r[4], "parent_version_id": r[5], "immutable_source_data": r[6],
            "extraction_status": r[7], "review_reason": r[8], "ai_suggestions": r[9],
            "validation_report": r[10], "created_by_operator_id": r[11], "created_at": r[12]
        }

        src_raw = d.get("immutable_source_data")
        src_obj = InvoiceSourceData.model_validate_json(src_raw) if src_raw else None

        ai_raw = d.get("ai_suggestions")
        ai_obj = AISuggestions.model_validate_json(ai_raw) if ai_raw else AISuggestions()

        val_raw = d["validation_report"]
        val_obj = ValidationReport.model_validate_json(val_raw)

        return ExtractionVersion(
            id=d["id"],
            document_id=d["document_id"],
            attempt_id=d.get("attempt_id"),
            version_number=d["version_number"],
            source_type=SourceType(d["source_type"]),
            parent_version_id=d.get("parent_version_id"),
            immutable_source_data=src_obj,
            extraction_status=d.get("extraction_status", "SUCCESS"),
            review_reason=d.get("review_reason"),
            ai_suggestions=ai_obj,
            validation_report=val_obj,
            created_by_operator_id=d.get("created_by_operator_id"),
            created_at=_parse_dt(d["created_at"]) or datetime.now(timezone.utc)
        )

    def _row_to_outbox_event(self, r: Any) -> OutboxEvent:
        if hasattr(r, "keys"):
            d = dict(r)
        else:
            d = {
                "id": r[0], "attempt_id": r[1], "aggregate_type": r[2], "aggregate_id": r[3],
                "event_type": r[4], "payload": r[5], "status": r[6], "retry_count": r[7],
                "created_at": r[8], "sent_at": r[9]
            }
            if len(r) > 10:
                d["max_retries"] = r[10]
            if len(r) > 11:
                d["next_retry_at"] = r[11]
            if len(r) > 12:
                d["last_error"] = r[12]

        return OutboxEvent(
            id=d["id"],
            attempt_id=d["attempt_id"],
            aggregate_type=d["aggregate_type"],
            aggregate_id=d["aggregate_id"],
            event_type=d["event_type"],
            payload=_parse_json(d["payload"]) or {},
            status=d["status"],
            retry_count=d.get("retry_count", 0),
            max_retries=d.get("max_retries", 5),
            next_retry_at=_parse_dt(d.get("next_retry_at")),
            last_error=d.get("last_error"),
            created_at=_parse_dt(d["created_at"]) or datetime.now(timezone.utc),
            sent_at=_parse_dt(d.get("sent_at"))
        )

    def _row_to_erp_event(self, r: Any) -> ERPExportEvent:
        d = dict(r) if hasattr(r, "keys") else {
            "id": r[0], "document_id": r[1], "extraction_version_id": r[2], "erp_system": r[3],
            "idempotency_key": r[4], "status": r[5], "erp_reference_id": r[6],
            "error_message": r[7], "payload": r[8], "created_at": r[9]
        }
        return ERPExportEvent(
            id=d["id"],
            document_id=d["document_id"],
            extraction_version_id=d["extraction_version_id"],
            erp_system=d["erp_system"],
            idempotency_key=d["idempotency_key"],
            status=ExportStatus(d["status"]),
            erp_reference_id=d.get("erp_reference_id"),
            error_message=d.get("error_message"),
            payload=_parse_json(d.get("payload")) or {},
            timestamp=_parse_dt(d["created_at"]) or datetime.now(timezone.utc)
        )


# Globalna instancja repozytorium
repository = AccountingRepository()


@contextmanager
def active_heartbeat(attempt_id: str, lease_token: str, interval_sec: float = 10.0):
    """
    Menedżer kontekstu uruchamiający wątek demoniczny w tle,
    który odświeża timestamp heartbeat_at w bazie danych co interval_sec sekund
    w trakcie długotrwałych operacji (OCR, Presidio, RAG, LLM).
    """
    stop_event = threading.Event()

    def _worker():
        while not stop_event.wait(interval_sec):
            try:
                repository.heartbeat_attempt(attempt_id, lease_token)
            except Exception as hb_err:
                logger.debug(f"Błąd wątku heartbeat dla próby {attempt_id}: {hb_err}")

    t = threading.Thread(target=_worker, daemon=True, name=f"heartbeat-{attempt_id[:8]}")
    t.start()
    try:
        yield
    finally:
        stop_event.set()
        t.join(timeout=2.0)
        try:
            repository.heartbeat_attempt(attempt_id, lease_token)
        except Exception:
            pass
