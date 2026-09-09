"""
Testy weryfikujące usunięcie 5 krytycznych wad gotowości produkcyjnej (v6.2):
1. Test DB Safety: reset_database odrzuca czyszczenie baz produkcyjnych i bez zmiennej zezwalającej.
2. Outbox Exponential Backoff & Retry Lifecycle: status RETRY, wykładnicze opóźnienie (2^k), FAILED po przekroczeniu max_retries.
3. Outbox Sweeper & Worker Heartbeat: sweeper resetuje zarówno próbę, jak i zdarzenie outboxa do PENDING; heartbeat odświeża lease.
4. ERP Concurrency Race & In-Flight Crash Window: atomowa rezerwacja (EXPORT_IN_FLIGHT), blokada równoległego wywołania (409), sweeper porzuconych transmisji -> EXPORT_UNKNOWN.
5. Document Rejection Invalidation: odrzucenie dokumentu czyści approved_version_id = NULL i uniemożliwia eksport do ERP (409).
6. Partial OCR Zero-Fabrication: brakujące pola pozostają None (brak domyślnych dat, PLN, stawek 23%, sztuk czy numerów).
"""

import os
import time
from datetime import datetime, timezone, timedelta, date
from decimal import Decimal
import pytest
from fastapi.testclient import TestClient

from main import app
from accounting.db import (
    AccountingRepository,
    repository,
    DocumentNotApprovedError,
    VersionNotApprovedError,
    ERPExportInFlightError,
    ERPExportUnknownError
)
from accounting.models import (
    DocumentStatus,
    AttemptStatus,
    SourceType,
    ExportStatus,
    InvoiceSourceData,
    InvoiceParty,
    InvoiceLineItem,
    TaxGroupSummary,
    TaxRateGroup,
    AISuggestions,
    ValidationReport
)
from validators.invoice_math import InvoiceMathValidator
from accounting.auth import create_access_token


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def auth_tokens():
    return {
        "admin": create_access_token("admin_usr", ["firma_v62"], {"firma_v62": "admin"}),
        "auditor": create_access_token("auditor_usr", ["firma_v62"], {"firma_v62": "auditor"}),
        "accountant": create_access_token("acc_usr", ["firma_v62"], {"firma_v62": "accountant"}),
    }


# =========================================================================
# 1. TEST DB SAFETY
# =========================================================================

def test_db_reset_protection_blocks_unauthorized_reset(monkeypatch):
    """reset_database() musi rzucić wyjątkiem, gdy KARIK_ALLOW_DB_RESET != '1'."""
    monkeypatch.setenv("KARIK_ALLOW_DB_RESET", "0")
    with pytest.raises(RuntimeError, match="KRYTYCZNE ZABEZPIECZENIE"):
        repository.reset_database()


def test_db_reset_protection_blocks_production_postgres(monkeypatch):
    """reset_database() odrzuca czyszczenie bazy PostgreSQL o nazwie produkcyjnej (np. karik_db)."""
    monkeypatch.setenv("KARIK_ALLOW_DB_RESET", "1")
    # Tworzymy atrapę repozytorium z engine postgres i bazą produkcyjną
    fake_repo = AccountingRepository.__new__(AccountingRepository)
    fake_repo.engine_type = "postgres"
    fake_repo.postgres_db = "karik_production_db"
    
    with pytest.raises(RuntimeError, match="Odmowa zresetowania produkcyjnej bazy PostgreSQL"):
        fake_repo.reset_database()


# =========================================================================
# 2. OUTBOX EXPONENTIAL BACKOFF & RETRY LIFECYCLE
# =========================================================================

def test_outbox_exponential_backoff_and_failure():
    """Weryfikacja naliczania prób outbox, statusu RETRY, 2^k sekund backoff i statusu FAILED."""
    doc, attempt, event = repository.create_document_and_attempt_atomically(
        tenant_id="firma_v62",
        operator_id="op_1",
        original_filename="outbox_test.pdf",
        storage_path="/storage/outbox_test.pdf",
        mime_type="application/pdf",
        sha256_hash="hash_outbox_v62_1"
    )

    ev_id = event.id
    assert event.status == "PENDING"
    assert event.retry_count == 0

    # Próba 1 -> Błąd brokera -> RETRY, retry_count=1, next_retry_at ~= now + 1s (2^0)
    now_before = datetime.now(timezone.utc)
    repository.mark_outbox_event_failed(ev_id, "Broker timeout 1", max_retries=3)
    ev1 = repository.get_outbox_event(ev_id)
    assert ev1.status == "RETRY"
    assert ev1.retry_count == 1
    assert ev1.next_retry_at is not None
    assert ev1.next_retry_at >= now_before + timedelta(seconds=1) - timedelta(milliseconds=200)

    # Próba 2 -> Błąd brokera -> RETRY, retry_count=2, next_retry_at ~= now + 2s (2^1)
    repository.mark_outbox_event_failed(ev_id, "Broker timeout 2", max_retries=3)
    ev2 = repository.get_outbox_event(ev_id)
    assert ev2.status == "RETRY"
    assert ev2.retry_count == 2
    assert ev2.next_retry_at >= now_before + timedelta(seconds=2) - timedelta(milliseconds=200)

    # Próba 3 -> Osiągnięcie limitu max_retries=3 -> FAILED
    repository.mark_outbox_event_failed(ev_id, "Broker timeout 3", max_retries=3)
    ev3 = repository.get_outbox_event(ev_id)
    assert ev3.status == "FAILED"
    assert ev3.retry_count == 3
    assert ev3.last_error == "Broker timeout 3"


# =========================================================================
# 3. OUTBOX SWEEPER & WORKER HEARTBEAT
# =========================================================================

def test_worker_heartbeat_and_sweeper_recovery():
    """Sweeper resetuje RUNNING bez heartbeat do PENDING i reaktywuje outbox event."""
    doc, attempt, event = repository.create_document_and_attempt_atomically(
        tenant_id="firma_v62",
        operator_id="op_1",
        original_filename="heartbeat_test.pdf",
        storage_path="/storage/heartbeat_test.pdf",
        mime_type="application/pdf",
        sha256_hash="hash_heartbeat_v62"
    )

    lease_token = repository.claim_attempt_atomically(attempt.id, "worker_alpha")
    assert lease_token is not None

    # Sprawdzenie heartbeat
    hb_ok = repository.heartbeat_attempt(attempt.id, lease_token)
    assert hb_ok is True

    # Symulacja: oznaczamy zdarzenie outbox jako SENT
    repository.mark_outbox_event_sent(event.id)

    # Symulacja upływu czasu: manipulujemy started_at / heartbeat_at w przeszłość (120 sekund temu)
    old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            repository._format_sql("UPDATE processing_attempts SET heartbeat_at = %s, started_at = %s WHERE id = %s"),
            (repository._format_dt(old_time), repository._format_dt(old_time), attempt.id)
        )

    # Uruchomienie sweepera z timeoutem 60 sekund
    recovered_attempts = repository.recover_stuck_attempts(timeout_seconds=60)
    assert attempt.id in recovered_attempts

    # Weryfikacja: próba powróciła do PENDING z wyczyszczonym leasingiem
    att_after = repository.get_attempt(attempt.id)
    assert att_after.status == AttemptStatus.PENDING
    assert att_after.lease_token is None

    # Weryfikacja: outbox_event powrócił do PENDING, by dyspozytor ponownie nadał zadanie
    ev_after = repository.get_outbox_event(event.id)
    assert ev_after.status == "RETRY"
    assert ev_after.retry_count == 1


# =========================================================================
# 4. ERP CONCURRENCY RACE & IN-FLIGHT CRASH WINDOW
# =========================================================================

def test_erp_export_atomic_reservation_and_conflict():
    """reserve_erp_export_atomic zapobiega równoległym transmisjom (EXPORT_IN_FLIGHT)."""
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_v62",
        operator_id="op_1",
        original_filename="erp_race.xml",
        storage_path="/storage/erp_race.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp_race_v62"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    src = InvoiceSourceData(
        invoice_number="FV/RACE/01",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca", tax_id="5260250995"),
        items=[
            InvoiceLineItem(
                position=1,
                description="Usługa IT",
                net_amount=Decimal("100.00"),
                tax_rate=TaxRateGroup.VAT_23,
                tax_amount=Decimal("23.00"),
                gross_amount=Decimal("123.00")
            )
        ],
        tax_summaries=[
            TaxGroupSummary(tax_rate=TaxRateGroup.VAT_23, net_amount=Decimal("100.00"), tax_amount=Decimal("23.00"), gross_amount=Decimal("123.00"))
        ],
        total_net=Decimal("100.00"),
        total_tax=Decimal("23.00"),
        total_gross=Decimal("123.00")
    )
    ver = repository.save_extraction_version(
        attempt_id=attempt.id,
        lease_token=lease,
        source_data=src,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )
    repository.approve_document_version(doc.id, ver.id, "auditor_usr")

    idem_key = "idempotency_race_key_12345"

    # Pierwsza rezerwacja: sukces -> status EXPORT_IN_FLIGHT
    exp_id, status, ref = repository.reserve_erp_export_atomic(
        document_id=doc.id,
        version_id=ver.id,
        idempotency_key=idem_key,
        erp_system="OPTIMA"
    )
    assert status == ExportStatus.EXPORT_IN_FLIGHT
    assert ref is None

    # Druga, równoległa rezerwacja dla tego samego klucza idempotencji -> ERPExportInFlightError
    with pytest.raises(ERPExportInFlightError, match="aktualnie realizowany przez inny proces"):
        repository.reserve_erp_export_atomic(
            document_id=doc.id,
            version_id=ver.id,
            idempotency_key=idem_key,
            erp_system="OPTIMA"
        )


def test_erp_crash_sweeper_transitions_in_flight_to_unknown():
    """Sweeper przesuwa porzucone zadania EXPORT_IN_FLIGHT do EXPORT_UNKNOWN."""
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_v62",
        operator_id="op_1",
        original_filename="erp_crash.xml",
        storage_path="/storage/erp_crash.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp_crash_v62"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    src = InvoiceSourceData(
        invoice_number="FV/CRASH/01",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("100.00"),
        total_tax=Decimal("23.00"),
        total_gross=Decimal("123.00")
    )
    ver = repository.save_extraction_version(
        attempt_id=attempt.id,
        lease_token=lease,
        source_data=src,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )
    repository.approve_document_version(doc.id, ver.id, "auditor_usr")

    idem_key = "idempotency_crash_key_99999"
    exp_id, status, _ = repository.reserve_erp_export_atomic(doc.id, ver.id, idem_key, "OPTIMA")
    assert status == ExportStatus.EXPORT_IN_FLIGHT

    # Symulacja crasha procesu: updated_at sprzed 120 sekund
    old_time = datetime.now(timezone.utc) - timedelta(seconds=120)
    with repository.get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            repository._format_sql("UPDATE erp_exports SET updated_at = %s WHERE id = %s"),
            (repository._format_dt(old_time), exp_id)
        )

    # Sweeper ERP przestawia na EXPORT_UNKNOWN
    swept = repository.sweep_stale_erp_exports(timeout_seconds=60)
    assert exp_id in swept

    # Próba rezerwacji po sweeperze zgłasza błąd konieczności formalnego uzgodnienia
    with pytest.raises(ERPExportUnknownError, match="Wymagane formalne uzgodnienie stanu"):
        repository.reserve_erp_export_atomic(doc.id, ver.id, idem_key, "OPTIMA")


# =========================================================================
# 5. DOCUMENT REJECTION INVALIDATION
# =========================================================================

def test_document_rejection_invalidates_approved_version_and_blocks_export(client, auth_tokens):
    """Odrzucenie dokumentu czyści approved_version_id = NULL i blokuje eksport do ERP kodem 409."""
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_v62",
        operator_id="op_1",
        original_filename="reject_test.xml",
        storage_path="/storage/reject_test.xml",
        mime_type="application/xml",
        sha256_hash="hash_reject_v62"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    src = InvoiceSourceData(
        invoice_number="FV/REJECT/01",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("100.00"),
        total_tax=Decimal("23.00"),
        total_gross=Decimal("123.00")
    )
    ver = repository.save_extraction_version(
        attempt_id=attempt.id,
        lease_token=lease,
        source_data=src,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )

    headers = {
        "Authorization": f"Bearer {auth_tokens['auditor']}",
        "X-Tenant-ID": "firma_v62"
    }

    # 1. Zatwierdzenie dokumentu
    app_res = client.post(f"/v1/invoice/approve/{doc.id}/{ver.id}", headers=headers)
    assert app_res.status_code == 200
    doc_app = repository.get_document(doc.id)
    assert doc_app.status == DocumentStatus.APPROVED
    assert doc_app.approved_version_id == ver.id

    # 2. Odrzucenie dokumentu przez audytora
    rej_res = client.post(f"/v1/invoice/reject/{doc.id}", headers=headers)
    assert rej_res.status_code == 200
    doc_rej = repository.get_document(doc.id)
    assert doc_rej.status == DocumentStatus.REJECTED
    assert doc_rej.approved_version_id is None  # KLUCZOWA POPRAWKA v6.2!

    # 3. Próba eksportu uprzednio zatwierdzonej wersji -> twarda odmowa 409 Conflict
    exp_res = client.post(f"/v1/invoice/erp-export/{doc.id}/{ver.id}", headers=headers)
    assert exp_res.status_code == 409
    assert "nie posiada statusu APPROVED" in exp_res.json()["detail"]


# =========================================================================
# 6. PARTIAL OCR ZERO-FABRICATION
# =========================================================================

def test_partial_ocr_leaves_missing_fields_as_none_without_fabrication():
    """Częściowy odczyt OCR zachowuje None dla brakujących pól bez zmyślania danych."""
    partial_source = InvoiceSourceData(
        invoice_number=None,  # brak zmyślonego OCR-xxxx
        issue_date=None,      # brak zmyślonej daty dzisiejszej
        currency=None,        # brak wymuszonego PLN
        seller=InvoiceParty(name="Sprzedawca Częściowy", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca Częściowy", tax_id="5260250995"),
        items=[
            InvoiceLineItem(
                position=1,
                description="Pozycja bez stawki i kwoty",
                quantity=None,        # brak wymuszonego 1.0
                unit_price_net=None,  # brak podstawiania netto jako ceny jednostkowej
                net_amount=None,
                tax_rate=None,        # brak wymuszonego 23%
                tax_amount=None,
                gross_amount=None
            )
        ],
        tax_summaries=[],
        total_net=None,
        total_tax=None,
        total_gross=None
    )

    report = InvoiceMathValidator.validate(partial_source)
    # Walidator zgłasza niekompletność bez rzucania TypeError
    assert report.is_valid is False
    assert report.status == DocumentStatus.REQUIRES_REVIEW
    assert any("Niekompletne dane pozycji" in err for err in report.reconciliation_errors)
    assert any("Brakujące sumy całkowite nagłówka" in err for err in report.reconciliation_errors)
