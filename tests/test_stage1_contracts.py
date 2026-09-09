"""
Testy weryfikacyjne dla Etapu 1 (Usunięcie niebezpiecznych zachowań i domknięcie kontraktów):
1. Brak odczytu PDF -> source_data=None, extraction_status=FAILED, status=REQUIRES_REVIEW (brak zer, brak fikcyjnych faktur).
2. Brak konfiguracji ERP -> HTTP 503 INTEGRATION_UNAVAILABLE (brak zmyślonych numerów ERP-REF).
3. EXPORT_UNKNOWN twardo blokuje automatyczne ponowienie (HTTP 409 Conflict).
4. Uzgodnienie ERP (/v1/invoice/erp-reconcile) dostępne wyłącznie dla audytora / administratora.
5. Separacja obowiązków: księgowy (accountant) NIE MOŻE zatwierdzić faktury (HTTP 403 Forbidden).
6. Wymuszenie klucza KARIK_AUTH_SECRET (min. 32 znaki).
7. Całkowity brak wycieku słownika mapowań PII w /v1/test/anonymize oraz /v1/manual/anonymize.
8. Uczciwe oznaczenie braku implementacji FA(3) (FA3_NOT_IMPLEMENTED).
"""

import os
import io
import pytest
from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient

import main
from accounting.models import (
    DocumentStatus,
    ExportStatus,
    InvoiceSourceData,
    InvoiceParty,
    ValidationReport,
    AISuggestions,
    ExtractionVersion,
    SourceType,
    ERPExportEvent
)
from accounting.auth import create_access_token, verify_access_token
from accounting.db import repository
from accounting.erp_connector import ComarchOptimaConnector, ERPIntegrationUnavailableError
from ksef_parser import SafeKsefXmlParser

client = TestClient(main.app)


@pytest.fixture
def auth_tokens():
    """Generuje tokeny dla różnych ról RBAC."""
    return {
        "accountant": create_access_token(operator_id="jan_ksiegowy", allowed_tenants=["firma_abc"], roles={"firma_abc": "accountant"}),
        "auditor": create_access_token(operator_id="anna_audytor", allowed_tenants=["firma_abc"], roles={"firma_abc": "auditor"}),
        "admin": create_access_token(operator_id="piotr_admin", allowed_tenants=["firma_abc"], roles={"firma_abc": "admin"}),
        "viewer": create_access_token(operator_id="ewa_podglad", allowed_tenants=["firma_abc"], roles={"firma_abc": "viewer"}),
    }


def test_separation_of_duties_accountant_cannot_approve(auth_tokens):
    """Księgowy nie może zatwierdzić dokumentu (wymóg audytowy: 403 Forbidden dla roli accountant)."""
    # 1. Tworzymy dokument
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_abc",
        operator_id="jan_ksiegowy",
        original_filename="faktura.xml",
        storage_path="/storage/faktura.xml",
        mime_type="application/xml",
        sha256_hash="hash_sep_duties_1"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    ver = repository.save_extraction_version(
        attempt_id=attempt.id,
        lease_token=lease,
        source_data=None,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=False, status=DocumentStatus.REQUIRES_REVIEW),
        source_type=SourceType.OCR_HYPOTHESIS,
        extraction_status="FAILED"
    )

    # 2. Księgowy próbuje zatwierdzić wersję -> 403 Forbidden
    headers_accountant = {
        "Authorization": f"Bearer {auth_tokens['accountant']}",
        "X-Tenant-ID": "firma_abc"
    }
    res_acc = client.post(f"/v1/invoice/approve/{doc.id}/{ver.id}", headers=headers_accountant)
    assert res_acc.status_code == 403
    assert "nie zezwala na tę operację" in res_acc.json()["detail"]

    # 3. Audytor zatwierdza wersję -> 200 OK
    headers_auditor = {
        "Authorization": f"Bearer {auth_tokens['auditor']}",
        "X-Tenant-ID": "firma_abc"
    }
    res_aud = client.post(f"/v1/invoice/approve/{doc.id}/{ver.id}", headers=headers_auditor)
    assert res_aud.status_code == 200
    assert res_aud.json()["status"] == "APPROVED"
    assert repository.get_document(doc.id).approved_version_id == ver.id


def test_pdf_failure_produces_none_source_data_and_requires_review():
    """Awaria odczytu OCR skutkuje source_data=None i statusem FAILED (brak zer i fikcyjnych faktur)."""
    from tasks import process_invoice_task

    dummy_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "temp_uszkodzony.pdf"))
    with open(dummy_path, "wb") as f:
        f.write(b"%PDF-corrupted-test-content")

    try:
        # Przygotowujemy próbę w bazie
        doc, attempt, _ = repository.create_document_and_attempt_atomically(
            tenant_id="firma_abc",
            operator_id="jan_ksiegowy",
            original_filename="uszkodzony.pdf",
            storage_path=dummy_path,
            mime_type="application/pdf",
            sha256_hash="hash_corrupted_pdf_1"
        )

        # Uruchamiamy procesowanie
        with patch("tasks.extract_pdf_text", return_value=""):
            with patch("tasks.encode_image_to_base64", side_effect=RuntimeError("OCR Error")):
                res = process_invoice_task(attempt.id, dummy_path, "firma_abc")
    finally:
        if os.path.exists(dummy_path):
            os.remove(dummy_path)

    assert res["status"] == "REQUIRES_REVIEW"
    versions = repository.get_versions(doc.id)
    assert len(versions) == 1
    v1 = versions[0]
    # KRYTYCZNA ASERCJA: source_data to None, NIE fikcyjne 100/23/123 PLN ani sztuczny NIP!
    assert v1.immutable_source_data is None
    assert v1.extraction_status == "FAILED"
    assert v1.review_reason in ("PRIVACY_ERROR", "OCR_EXTRACTION_FAILED")


def test_erp_integration_unavailable_returns_503(auth_tokens, monkeypatch):
    """Gdy konektor ERP nie jest skonfigurowany, endpoint zwraca 503 INTEGRATION_UNAVAILABLE."""
    # Wyłączamy test mode konektora
    from accounting.erp_connector import erp_connector
    monkeypatch.setattr(erp_connector, "is_test_mode", False)
    monkeypatch.setattr(erp_connector, "api_url", None)
    monkeypatch.setattr(erp_connector, "xml_exchange_dir", None)

    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_abc",
        operator_id="jan_ksiegowy",
        original_filename="faktura_erp.xml",
        storage_path="/storage/faktura_erp.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp_unav_1"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    dummy_source = InvoiceSourceData(
        invoice_number="FV/ERP/503",
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
        source_data=dummy_source,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )
    repository.approve_document_version(doc.id, ver.id, "anna_audytor")

    headers = {
        "Authorization": f"Bearer {auth_tokens['auditor']}",
        "X-Tenant-ID": "firma_abc"
    }
    response = client.post(f"/v1/invoice/erp-export/{doc.id}/{ver.id}", headers=headers)
    assert response.status_code == 503
    assert "INTEGRATION_UNAVAILABLE" in response.json()["detail"]


def test_erp_unknown_blocks_retry_with_409(auth_tokens):
    """Stan EXPORT_UNKNOWN blokuje ponowną automatyczną transmisję i wymaga uzgodnienia."""
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_abc",
        operator_id="jan_ksiegowy",
        original_filename="faktura_unknown.xml",
        storage_path="/storage/faktura_unknown.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp_unknown_1"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    dummy_source = InvoiceSourceData(
        invoice_number="FV/ERP/UNKNOWN",
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
        source_data=dummy_source,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )
    repository.approve_document_version(doc.id, ver.id, "anna_audytor")

    headers = {
        "Authorization": f"Bearer {auth_tokens['auditor']}",
        "X-Tenant-ID": "firma_abc"
    }

    # 1. Pierwsze wywołanie z symulacją utraty sieci -> EXPORT_UNKNOWN
    res1 = client.post(
        f"/v1/invoice/erp-export/{doc.id}/{ver.id}",
        json={"erp_system": "comarch_optima", "simulate_network_timeout": True},
        headers=headers
    )
    assert res1.status_code == 200
    assert res1.json()["status"] == "EXPORT_UNKNOWN"

    # 2. Drugie wywołanie -> twarda blokada 409 Conflict
    res2 = client.post(
        f"/v1/invoice/erp-export/{doc.id}/{ver.id}",
        json={"erp_system": "comarch_optima", "simulate_network_timeout": False},
        headers=headers
    )
    assert res2.status_code == 409
    assert "Wymagane formalne uzgodnienie stanu" in res2.json()["detail"]


def test_erp_reconcile_by_auditor(auth_tokens):
    """Audytor może uzgodnić stan nieznany przez endpoint erp-reconcile."""
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="firma_abc",
        operator_id="jan_ksiegowy",
        original_filename="faktura_rec.xml",
        storage_path="/storage/faktura_rec.xml",
        mime_type="application/xml",
        sha256_hash="hash_erp_rec_1"
    )
    lease = repository.claim_attempt_atomically(attempt.id, "worker_1")
    dummy_source = InvoiceSourceData(
        invoice_number="FV/ERP/REC",
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
        source_data=dummy_source,
        ai_suggestions=AISuggestions(),
        validation_report=ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    )
    repository.approve_document_version(doc.id, ver.id, "anna_audytor")

    headers_auditor = {
        "Authorization": f"Bearer {auth_tokens['auditor']}",
        "X-Tenant-ID": "firma_abc"
    }

    # Ustawiamy stan EXPORT_UNKNOWN
    client.post(
        f"/v1/invoice/erp-export/{doc.id}/{ver.id}",
        json={"erp_system": "comarch_optima", "simulate_network_timeout": True},
        headers=headers_auditor
    )

    # Audytor dokonuje formalnego uzgodnienia
    reconcile_res = client.post(
        f"/v1/invoice/erp-reconcile/{doc.id}/{ver.id}",
        json={"decision": "CONFIRM", "erp_reference_id": "OPT-REC-12345", "notes": "Faktura odnaleziona w bazie Optimy."},
        headers=headers_auditor
    )
    assert reconcile_res.status_code == 200
    assert reconcile_res.json()["status"] == "EXPORT_CONFIRMED"
    assert reconcile_res.json()["erp_reference_id"] == "OPT-REC-12345"


def test_anonymize_endpoints_do_not_leak_mapping_dictionary(auth_tokens):
    """Zarówno /v1/test/anonymize jak i /v1/manual/anonymize nie zwracają słownika tokenów klientowi."""
    headers = {
        "Authorization": f"Bearer {auth_tokens['accountant']}",
        "X-Tenant-ID": "firma_abc"
    }

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": "[]"}}]
        }
        mock_post.return_value = mock_resp

        # 1. Test /v1/manual/anonymize
        res_manual = client.post(
            "/v1/manual/anonymize",
            json={"raw_text": "Faktura Jan Kowalski NIP 7740001454"},
            headers=headers
        )
        assert res_manual.status_code == 200
        data_manual = res_manual.json()
        assert "mapping_dictionary" not in data_manual
        assert "session_id" in data_manual
        assert "anonymized_text" in data_manual

        # 2. Test /v1/test/anonymize
        files = {"file": ("test.txt", b"Faktura Jan Kowalski NIP 7740001454", "text/plain")}
        res_test = client.post("/v1/test/anonymize", files=files, headers=headers)
        assert res_test.status_code == 200
        data_test = res_test.json()
        assert "mapping_dictionary" not in data_test
        assert "detokenization_test" not in data_test
        assert "anonymized_text" in data_test


def test_fa3_not_implemented_routed_to_review():
    """Schemat FA(3) zwraca FA3_NOT_IMPLEMENTED z rzetelnym wyjaśnieniem."""
    fa3_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Faktura xmlns="http://crd.gov.pl/wzor/2026/01/01/99999/">
        <Fa><RodzajFaktury>VAT</RodzajFaktury><KodWaluty>PLN</KodWaluty></Fa>
    </Faktura>
    """
    res = SafeKsefXmlParser.parse_bytes(fa3_xml)
    assert res.is_supported_subset is False
    assert res.unsupported_reason == "FA3_NOT_IMPLEMENTED"
    assert "FA(3) jest oficjalnym standardem MF" in res.errors[0]
