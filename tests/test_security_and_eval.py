"""
Testy bezpieczeństwa uploadu, autoryzacji sesyjnej oraz cyklu życia eksportu ERP.
"""

import os
import io
import pytest
from unittest.mock import AsyncMock
from fastapi import UploadFile, HTTPException

from accounting.storage import stream_and_save_upload, MAX_UPLOAD_BYTES
from accounting.auth import create_access_token, verify_access_token, AuthContext
from accounting.db import AccountingRepository
from accounting.models import (
    DocumentStatus,
    InvoiceSourceData,
    InvoiceParty,
    ValidationReport,
    AISuggestions,
    ExportStatus,
    ERPExportEvent,
)
from decimal import Decimal
from datetime import date


@pytest.mark.asyncio
async def test_streaming_upload_limit_enforcement(monkeypatch):
    """
    Test weryfikujący, że upload zlicza odebrane bajty w locie
    i natychmiast przerywa przy przekroczeniu limitu oraz usuwa plik częściowy.
    """
    import accounting.storage as storage_mod
    local_tmp = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache", "test_storage"))
    os.makedirs(local_tmp, exist_ok=True)
    monkeypatch.setattr(storage_mod, "STORAGE_BASE_DIR", local_tmp)


    # Obniżamy limit na potrzeby testu do 100 KB
    monkeypatch.setattr(storage_mod, "MAX_UPLOAD_BYTES", 100 * 1024)

    # Przygotowujemy strumień o wielkości 150 KB
    oversized_data = b"X" * (150 * 1024)
    file_obj = io.BytesIO(oversized_data)

    upload_file = UploadFile(
        file=file_obj,
        filename="large_file.pdf"
    )

    with pytest.raises(HTTPException) as exc_info:
        await stream_and_save_upload(upload_file, "test_tenant", "doc_large")

    assert exc_info.value.status_code == 413
    assert "przekracza dopuszczalny limit" in exc_info.value.detail

    # Weryfikacja: Plik częściowy NIE może pozostać na dysku!
    target_dir = storage_mod.get_document_storage_dir("test_tenant", "doc_large")
    assert not os.path.exists(os.path.join(target_dir, "raw_source.pdf"))


def test_auth_token_tampering_rejected():
    """Weryfikacja odrzucenia sfałszowanego lub niepodpisanego tokena sesyjnego."""
    valid_token = create_access_token(
        operator_id="operator_valid",
        allowed_tenants=["tenant_1"]
    )
    auth = verify_access_token(valid_token)
    assert auth.operator_id == "operator_valid"

    # Próba modyfikacji payloadu tokena
    payload_part, sig_part = valid_token.split(".")
    tampered_token = f"{payload_part}modified.{sig_part}"

    with pytest.raises(HTTPException) as exc_info:
        verify_access_token(tampered_token)
    assert exc_info.value.status_code == 401


def test_erp_export_three_stage_lifecycle_and_unknown_timeout():
    """
    Test 3-fazowego cyklu życia eksportu ERP oraz obsługi stanu EXPORT_UNKNOWN
    z wyliczeniem klucza idempotencji uniemożliwiającego podwójne księgowanie.
    """
    repo = AccountingRepository()
    doc, attempt, _ = repo.create_document_and_attempt_atomically(
        tenant_id="tenant_erp",
        operator_id="accountant_1",
        original_filename="inv_erp.xml",
        storage_path="/storage/erp.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp"
    )
    lease = repo.claim_attempt_atomically(attempt.id, "worker_erp")

    source = InvoiceSourceData(
        invoice_number="FV/ERP/01",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Firma ERP", tax_id="7740001454"),
        buyer=InvoiceParty(name="Klient ERP", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("1000.00"),
        total_tax=Decimal("230.00"),
        total_gross=Decimal("1230.00")
    )
    rep = ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    ver = repo.save_extraction_version(attempt.id, lease, source, AISuggestions(), rep)

    # Zatwierdzenie wersji
    repo.approve_document_version(doc.id, ver.id, "accountant_1")
    assert repo._documents[doc.id].approved_version_id == ver.id

    import hashlib
    idempotency_key = hashlib.sha256(f"{doc.id}:{ver.id}:OPTIMA".encode("utf-8")).hexdigest()

    # Krok 1: Wygenerowanie eksportu
    step1 = ERPExportEvent(
        document_id=doc.id,
        extraction_version_id=ver.id,
        erp_system="OPTIMA",
        idempotency_key=idempotency_key,
        status=ExportStatus.EXPORT_GENERATED
    )
    repo.record_erp_export_step(step1)

    # Krok 2: Zerwane połączenie przed odebraniem odpowiedzi z ERP -> EXPORT_UNKNOWN
    step_timeout = ERPExportEvent(
        document_id=doc.id,
        extraction_version_id=ver.id,
        erp_system="OPTIMA",
        idempotency_key=idempotency_key,
        status=ExportStatus.EXPORT_UNKNOWN,
        error_message="Timeout połączenia HTTP z serwerem ERP."
    )
    repo.record_erp_export_step(step_timeout)

    events = repo._erp_events[doc.id]
    assert len(events) == 2
    assert events[-1].status == ExportStatus.EXPORT_UNKNOWN

    # Krok 3: Po weryfikacji w ERP, następuje potwierdzenie
    step_confirmed = ERPExportEvent(
        document_id=doc.id,
        extraction_version_id=ver.id,
        erp_system="OPTIMA",
        idempotency_key=idempotency_key,
        status=ExportStatus.EXPORT_CONFIRMED,
        erp_reference_id="OPTIMA-DOC-98765"
    )
    repo.record_erp_export_step(step_confirmed)

    final_events = repo._erp_events[doc.id]
    assert len(final_events) == 3
    assert final_events[-1].status == ExportStatus.EXPORT_CONFIRMED
    assert final_events[-1].erp_reference_id == "OPTIMA-DOC-98765"
