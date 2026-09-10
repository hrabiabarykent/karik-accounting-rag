import os
import time
import uuid
import hashlib
import threading
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient

import main
from accounting.db import repository, active_heartbeat, ERPExportInFlightError, ERPExportUnknownError
from accounting.auth import create_access_token
from accounting.models import (
    DocumentStatus, AttemptStatus, SourceType, ExportStatus,
    InvoiceSourceData, AISuggestions, ValidationReport
)

client = TestClient(main.app)

@pytest.fixture
def auditor_auth_headers():
    token = create_access_token(
        operator_id="auditor_v63",
        allowed_tenants=["tenant_v63"],
        roles={"tenant_v63": "auditor"}
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": "tenant_v63"
    }

@pytest.fixture
def dummy_approved_invoice():
    """Tworzy zatwierdzony dokument w bazie gotowy do eksportu ERP."""
    doc_id = str(uuid.uuid4())
    tenant_id = "tenant_v63"
    sha = hashlib.sha256(f"file-content-{doc_id}".encode()).hexdigest()
    
    doc, att, ev = repository.create_document_and_attempt_atomically(
        tenant_id=tenant_id,
        operator_id="auditor_v63",
        original_filename="faktura_test.pdf",
        storage_path=f"/tmp/test_{doc_id}.pdf",
        mime_type="application/pdf",
        sha256_hash=sha
    )
    
    from decimal import Decimal
    from datetime import date
    from accounting.models import InvoiceParty

    inv_data = InvoiceSourceData(
        invoice_number="FV/2026/09/001",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Firma Testowa Sp. z o.o.", tax_id="7740001454"),
        buyer=InvoiceParty(name="Odbiorca S.A.", tax_id="5213894012"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("1000.00"),
        total_tax=Decimal("230.00"),
        total_gross=Decimal("1230.00")
    )
    
    lease_token = repository.claim_attempt_atomically(att.id, "worker-1")
    ver = repository.save_extraction_version(
        attempt_id=att.id,
        lease_token=lease_token,
        source_data=inv_data,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED),
        source_type=SourceType.OCR_HYPOTHESIS
    )
    
    repository.approve_document_version(
        document_id=doc.id,
        version_id=ver.id,
        operator_id="auditor_v63",
        notes="Zatwierdzono do testów produkcyjnych v6.3"
    )
    
    return doc, ver


def test_safe_ddl_migration_reentrant_and_no_abort():
    """
    Test 1: Sprawdzenie reentrantności i bezpieczeństwa migracji DDL.
    Wielokrotne wywołanie _init_schema() nie może rzucać wyjątków ani uszkadzać stanu transakcji.
    """
    repository._init_schema()
    repository._init_schema()
    
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        assert cursor.fetchone()[0] == 1


def test_erp_export_status_fidelity_file_drop_vs_confirmed(monkeypatch, auditor_auth_headers, dummy_approved_invoice, tmp_path):
    """
    Test 2: Zapis pliku wymiany XML nie staje się fikcyjnym 'EXPORT_CONFIRMED'.
    - Transmisja plikowa Comarch Optima zwraca EXPORT_TRANSMITTED.
    - Status w bazie (erp_exports i erp_export_events) musi wynosić EXPORT_TRANSMITTED.
    - Ponowna próba eksportu bez uzgodnienia jest blokowana (409 EXPORT_UNKNOWN/TRANSMITTED).
    - Audytor może uzgodnić stan przez /v1/invoice/erp-reconcile.
    """
    from accounting.erp_connector import erp_connector
    local_exchange = str(tmp_path / "test_optima_exchange")
    os.makedirs(local_exchange, exist_ok=True)
    monkeypatch.setattr(erp_connector, "xml_exchange_dir", local_exchange)
    doc, ver = dummy_approved_invoice
    
    # Wywołanie eksportu
    resp = client.post(
        f"/v1/invoice/erp-export/{doc.id}/{ver.id}",
        json={"erp_system": "comarch_optima"},
        headers=auditor_auth_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == ExportStatus.EXPORT_TRANSMITTED.value
    idempotency_key = data["idempotency_key"]
    
    # Sprawdzenie bazy danych
    latest_export = repository.get_latest_erp_export(idempotency_key)
    assert latest_export is not None
    assert latest_export.status == ExportStatus.EXPORT_TRANSMITTED
    
    # Ponowny eksport bez uzgodnienia musi zwrócić 409
    retry_resp = client.post(
        f"/v1/invoice/erp-export/{doc.id}/{ver.id}",
        json={"erp_system": "comarch_optima"},
        headers=auditor_auth_headers
    )
    assert retry_resp.status_code == 409
    assert "niepotwierdzonym" in retry_resp.json()["detail"]
    
    # Audytor uzgadnia stan jako potwierdzony
    reconcile_resp = client.post(
        f"/v1/invoice/erp-reconcile/{doc.id}/{ver.id}",
        json={
            "decision": "CONFIRM",
            "erp_reference_id": "OPTIMA-CONFIRMED-999",
            "notes": "Sprawdzono fizyczny import w Comarch Optima"
        },
        headers=auditor_auth_headers
    )
    assert reconcile_resp.status_code == 200
    assert reconcile_resp.json()["status"] == ExportStatus.EXPORT_CONFIRMED.value


def test_erp_export_atomic_reservation_concurrency_race(dummy_approved_invoice):
    """
    Test 3: Wyścig współbieżności w reserve_erp_export_atomic.
    Gdy rekord jest w stanie EXPORT_FAILED, wiele wątków próbuje go jednocześnie zarezerwować.
    Dokładnie JEDEN wątek może wygrać (EXPORT_IN_FLIGHT), a pozostałe MUSZĄ otrzymać ERPExportInFlightError.
    """
    doc, ver = dummy_approved_invoice
    idempotency_key = hashlib.sha256(f"{doc.id}:{ver.id}:comarch_optima".encode()).hexdigest()
    
    # Inicjalizacja eksportu w stanie EXPORT_FAILED
    exp_id, status, ref = repository.reserve_erp_export_atomic(
        document_id=doc.id,
        version_id=ver.id,
        idempotency_key=idempotency_key,
        erp_system="comarch_optima"
    )
    repository.fail_erp_export(exp_id, "Wymuszony błąd do testu wyścigu")
    
    success_count = 0
    in_flight_errors = 0
    lock = threading.Lock()
    
    def try_reserve():
        nonlocal success_count, in_flight_errors
        try:
            repository.reserve_erp_export_atomic(
                document_id=doc.id,
                version_id=ver.id,
                idempotency_key=idempotency_key,
                erp_system="comarch_optima"
            )
            with lock:
                success_count += 1
        except ERPExportInFlightError:
            with lock:
                in_flight_errors += 1
        except Exception:
            pass

    threads = [threading.Thread(target=try_reserve) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
        
    assert success_count == 1, f"Oczekiwano dokładnie 1 zwycięskiego wątku, otrzymano: {success_count}"
    assert in_flight_errors == 9, f"Oczekiwano 9 błędów ERPExportInFlightError, otrzymano: {in_flight_errors}"


def test_outbox_lost_message_recovery_when_sent_but_pending():
    """
    Test 4: Odzyskiwanie porzuconych prób PENDING, gdy outbox został oznaczony jako SENT,
    ale wiadomość została utracona przez broker/sieć przed odebraniem przez workera.
    """
    doc_id = str(uuid.uuid4())
    doc, att, ev = repository.create_document_and_attempt_atomically(
        tenant_id="tenant_v63",
        operator_id="test_op",
        original_filename="lost_message.pdf",
        storage_path=f"/tmp/lost_{doc_id}.pdf",
        mime_type="application/pdf",
        sha256_hash=hashlib.sha256(b"lost").hexdigest()
    )
    
    # Symulacja: Outbox został oznaczony jako SENT 120 sekund temu, ale worker nigdy nie podjął próby (status wciąż PENDING)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            repository._format_sql("UPDATE outbox_events SET status = 'SENT', sent_at = %s WHERE id = %s"),
            (repository._format_dt(stale_time), ev.id)
        )
        cursor.execute(
            repository._format_sql("UPDATE processing_attempts SET created_at = %s WHERE id = %s"),
            (repository._format_dt(stale_time), att.id)
        )
        
    # Uruchomienie procedury odzyskiwania
    recovered = repository.recover_stuck_attempts(timeout_seconds=60)
    assert att.id in recovered
    
    # Zdarzenie outbox musi wrócić do statusu PENDING z inkrementowanym licznikiem retry
    updated_ev = repository.get_outbox_event(ev.id)
    assert updated_ev.status == "RETRY"
    assert updated_ev.retry_count == 1


def test_active_heartbeat_background_thread_renews_lease():
    """
    Test 5: Wątek active_heartbeat cyklicznie odświeża sygnał życia w bazie podczas długich operacji.
    """
    doc_id = str(uuid.uuid4())
    doc, att, ev = repository.create_document_and_attempt_atomically(
        tenant_id="tenant_v63",
        operator_id="test_op",
        original_filename="hb_test.pdf",
        storage_path=f"/tmp/hb_{doc_id}.pdf",
        mime_type="application/pdf",
        sha256_hash=hashlib.sha256(b"hb").hexdigest()
    )
    lease_token = repository.claim_attempt_atomically(att.id, "worker-hb")
    
    # Pobranie początkowego timestampu
    initial_att = repository.get_attempt(att.id)
    hb_0 = initial_att.heartbeat_at
    
    # Uruchomienie aktywnego heartbeat z interwałem 0.1s
    with active_heartbeat(att.id, lease_token, interval_sec=0.1):
        time.sleep(0.35)
        mid_att = repository.get_attempt(att.id)
        assert mid_att.heartbeat_at > hb_0
        hb_mid = mid_att.heartbeat_at
        
    # Po wyjściu z kontekstu wątek kończy pracę
    time.sleep(0.2)
    final_att = repository.get_attempt(att.id)
    assert final_att.heartbeat_at >= hb_mid


def test_sweeper_dual_table_consistency_and_immediate_reconciliation(auditor_auth_headers, dummy_approved_invoice):
    """
    Test 6: Spójność obu tabel (erp_exports i erp_export_events) po interwencji Sweepera.
    Sweeper ustawiający EXPORT_UNKNOWN zapisuje rekord w obu tabelach, co pozwala na natychmiastowe
    uzgodnienie przez endpoint /v1/invoice/erp-reconcile bez błędu 400.
    """
    doc, ver = dummy_approved_invoice
    idempotency_key = hashlib.sha256(f"{doc.id}:{ver.id}:comarch_optima".encode()).hexdigest()
    
    # Rezerwacja eksportu (IN_FLIGHT)
    exp_id, status, ref = repository.reserve_erp_export_atomic(
        document_id=doc.id,
        version_id=ver.id,
        idempotency_key=idempotency_key,
        erp_system="comarch_optima"
    )
    
    # Symulacja utknięcia IN_FLIGHT (crash procesu 600s temu)
    stale_time = datetime.now(timezone.utc) - timedelta(seconds=600)
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            repository._format_sql("UPDATE erp_exports SET updated_at = %s WHERE id = %s"),
            (repository._format_dt(stale_time), exp_id)
        )
        
    # Sweeper zamiata utknięty eksport
    swept = repository.sweep_stale_erp_exports(timeout_seconds=300)
    assert exp_id in swept
    
    # Weryfikacja spójności tabel: erp_exports ma EXPORT_UNKNOWN
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(repository._format_sql("SELECT status FROM erp_exports WHERE id = %s"), (exp_id,))
        row = cursor.fetchone()
        assert (row[0] if isinstance(row, tuple) else row["status"]) == ExportStatus.EXPORT_UNKNOWN.value
        
    # erp_export_events RÓWNIEŻ musi zawierać zdarzenie EXPORT_UNKNOWN
    latest_ev = repository.get_latest_erp_export(idempotency_key)
    assert latest_ev is not None
    assert latest_ev.status == ExportStatus.EXPORT_UNKNOWN
    
    # Endpoint /v1/invoice/erp-reconcile musi natychmiast przyjąć uzgodnienie (bez błędu 400!)
    rec_resp = client.post(
        f"/v1/invoice/erp-reconcile/{doc.id}/{ver.id}",
        json={
            "decision": "CONFIRM",
            "erp_reference_id": "RECONCILED-AFTER-CRASH",
            "notes": "Uzgodniono stan po awarii procesu eksportu"
        },
        headers=auditor_auth_headers
    )
    assert rec_resp.status_code == 200
    assert rec_resp.json()["status"] == ExportStatus.EXPORT_CONFIRMED.value
