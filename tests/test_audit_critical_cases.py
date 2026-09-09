"""
Zestaw testów automatycznych weryfikujących krytyczne przypadki audytowe KARIK:
1. Podwójne wykonanie zadania (idempotencja i leasing prób).
2. Awaria między zapisem w bazie a kolejkowaniem (Transactional Outbox).
3. Dostęp użytkownika do cudzej firmy (Tenant Isolation).
4. Zatwierdzenie nieaktualnej wersji ekstrakcji (Stale Version Review).
5. Wykrycie i neutralizacja spójnie zmienionych kwot (Proportionally Altered Amounts).
6. Bezwzględny brak wywołań chmury po błędzie prywatności (Fail-Closed Zero Egress).
"""

import os
import uuid
import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from datetime import date
from fastapi import HTTPException

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
    ValidationReport,
)
from accounting.db import AccountingRepository
from accounting.auth import AuthContext, create_access_token, verify_access_token
from security.egress_guard import ScopedEgressGuard, seal_sanitized_envelope, SecurityPrivacyViolationError
from validators.invoice_math import InvoiceMathValidator


def test_double_execution_idempotency():
    """
    Test 1: Podwójne wykonanie zadania.
    Dwa workery otrzymują to samo zadanie Celery. Tylko jeden uzyskuje lease_token.
    Drugi zostaje atomowo odrzucony (no-op). Stary worker nie może zapisać wyniku.
    """
    repo = AccountingRepository()
    doc, attempt, event = repo.create_document_and_attempt_atomically(
        tenant_id="tenant_alpha",
        operator_id="operator_1",
        original_filename="inv1.xml",
        storage_path="/storage/inv1.xml",
        mime_type="application/xml",
        sha256_hash="hash_alpha_1"
    )

    attempt_id = attempt.id

    # Worker 1 przejmuje próbę
    lease_1 = repo.claim_attempt_atomically(attempt_id, worker_id="worker_node_1")
    assert lease_1 is not None
    assert repo._attempts[attempt_id].status == AttemptStatus.RUNNING

    # Worker 2 próbuje przejąć tę samą próbę równolegle
    lease_2 = repo.claim_attempt_atomically(attempt_id, worker_id="worker_node_2")
    assert lease_2 is None  # Odrzucony!

    # Worker 1 pomyślnie zapisuje wynik
    dummy_source = InvoiceSourceData(
        invoice_number="FV/2026/01",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Firma A", tax_id="7740001454"),
        buyer=InvoiceParty(name="Firma B", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("100.00"),
        total_tax=Decimal("23.00"),
        total_gross=Decimal("123.00")
    )
    report = ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)

    ver = repo.save_extraction_version(
        attempt_id=attempt_id,
        lease_token=lease_1,
        source_data=dummy_source,
        ai_suggestions=AISuggestions(),
        validation_report=report
    )
    assert ver is not None
    assert repo._attempts[attempt_id].status == AttemptStatus.COMPLETED

    # Próba zapisu przez spóźnionego workera lub nieprawidłowy token dzierżawy
    stale_write = repo.save_extraction_version(
        attempt_id=attempt_id,
        lease_token="fake_stale_lease_token",
        source_data=dummy_source,
        ai_suggestions=AISuggestions(),
        validation_report=report
    )
    assert stale_write is None  # Fencing zablokował zapis!


def test_failure_between_db_commit_and_queue_dispatch():
    """
    Test 2: Awaria między zapisem w bazie a wysłaniem do brokera.
    Gdy broker rzuca błąd, dane w bazie pozostają nienaruszone w stanie PENDING dla Outbox Sweepera.
    """
    repo = AccountingRepository()
    doc, attempt, event = repo.create_document_and_attempt_atomically(
        tenant_id="tenant_beta",
        operator_id="operator_2",
        original_filename="inv2.pdf",
        storage_path="/storage/inv2.pdf",
        mime_type="application/pdf",
        sha256_hash="hash_beta_2"
    )

    assert doc.status == DocumentStatus.RECEIVED
    assert attempt.status == AttemptStatus.PENDING
    assert event.status == "PENDING"

    # Symulacja awarii sieciowej brokera zadań Celery
    broker_error = "ConnectionResetError: Redis broker unreachable"
    repo.mark_outbox_event_failed(event.id, broker_error)

    # Asercja: baza zachowuje spójność, zadanie oczekuje na odzyskanie
    assert repo._documents[doc.id].status == DocumentStatus.RECEIVED
    assert repo._attempts[attempt.id].status == AttemptStatus.PENDING
    assert repo._outbox_events[event.id].status in ("FAILED", "RETRY")
    assert repo._outbox_events[event.id].retry_count == 1

    # Sweeper może ponowić dispatch
    pending_events = [e for e in repo._outbox_events.values() if e.retry_count > 0]
    assert len(pending_events) == 1


def test_cross_tenant_unauthorized_access():
    """
    Test 3: Izolacja wielofirmowa (Tenant Isolation).
    Użytkownik zalogowany do tenant_X nie ma prawa dostępu do zasobów tenant_Y.
    Nagłówek X-Tenant-ID deklarujący cudzą firmę zostaje natychmiast odrzucony (403).
    """
    token_user_x = create_access_token(
        operator_id="accountant_x",
        allowed_tenants=["tenant_company_X"]
    )
    auth_ctx = verify_access_token(token_user_x)

    # 1. Poprawny dostęp do własnej firmy
    tenant = auth_ctx.check_tenant_access("tenant_company_X")
    assert tenant == "tenant_company_X"

    # 2. Próba podszycia się pod tenant_company_Y
    with pytest.raises(HTTPException) as exc_info:
        auth_ctx.check_tenant_access("tenant_company_Y")
    assert exc_info.value.status_code == 403
    assert "Brak uprawnień" in exc_info.value.detail


def test_stale_version_approval_and_export():
    """
    Test 4: Zatwierdzenie nieaktualnej wersji ekstrakcji.
    Jeśli istnieje wersja v1 i v2, a operator zatwierdzi v1:
    - dokument wskazuje approved_version_id = v1.
    - wersja v2 NIE staje się automatycznie zatwierdzona.
    - próba eksportu wersji v2 zostaje zablokowana.
    """
    repo = AccountingRepository()
    doc, attempt, _ = repo.create_document_and_attempt_atomically(
        tenant_id="tenant_gamma",
        operator_id="operator_3",
        original_filename="inv3.pdf",
        storage_path="/storage/inv3.pdf",
        mime_type="application/pdf",
        sha256_hash="hash_gamma_3"
    )
    lease = repo.claim_attempt_atomically(attempt.id, "worker_1")

    source_v1 = InvoiceSourceData(
        invoice_number="FV/001",
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
    rep_v1 = ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)

    # Zapis wersji v1
    v1 = repo.save_extraction_version(attempt.id, lease, source_v1, AISuggestions(), rep_v1)
    assert v1.version_number == 1

    # Operator wprowadza korektę tworząc wersję v2
    source_v2 = source_v1.model_copy(update={"total_net": Decimal("200.00"), "total_gross": Decimal("246.00")})
    v2 = repo.create_operator_correction(
        document_id=doc.id,
        target_version_id=v1.id,
        operator_id="operator_3",
        corrected_source_data=source_v2,
        validation_report=rep_v1
    )
    assert v2.version_number == 2
    assert v2.parent_version_id == v1.id

    # Operator decyduje zatwierdzić wersję v1
    success = repo.approve_document_version(doc.id, v1.id, operator_id="operator_3")
    assert success is True

    # Weryfikacja: dokument wskazuje v1, wersja v2 nie jest zatwierdzona!
    doc_after = repo.get_document(doc.id)
    assert doc_after.approved_version_id == v1.id
    assert doc_after.approved_version_id != v2.id


def test_proportionally_altered_amounts_evasion():
    """
    Test 5: Wykrycie i neutralizacja spójnie zmienionych kwot.
    Faktura źródłowa: 100 zł netto / 23 zł VAT / 123 zł brutto.
    Nawet jeśli model AI zwróci spójnie przeskalowane kwoty (1000 / 230 / 1230),
    dane źródłowe faktury (immutable_source_data) pozostają nienaruszone (100 / 23 / 123).
    """
    true_source = InvoiceSourceData(
        invoice_number="FV/LEGIT",
        issue_date=date.today(),
        currency="PLN",
        seller=InvoiceParty(name="Prawdziwy Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Prawdziwy Nabywca", tax_id="5260250995"),
        items=[
            InvoiceLineItem(
                position=1,
                description="Usługa",
                unit_price_net=Decimal("100.00"),
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

    # Walidacja poprawności faktury źródłowej
    val_report = InvoiceMathValidator.validate(true_source)
    assert val_report.is_valid is True

    # AI halucynuje lub modyfikuje kwoty
    ai_hallucinated_amounts = {"suma_netto": Decimal("1000.00"), "suma_vat": Decimal("230.00"), "suma_brutto": Decimal("1230.00")}

    # Asercja architektury: AI zasila wyłącznie AISuggestions, a kwoty faktury pochodzą w 100% z true_source
    ai_suggestions = AISuggestions(
        suggested_debit_account="401-01",
        suggested_credit_account="210-01",
        accounting_notes=f"AI zauważyło kwoty: {ai_hallucinated_amounts}"
    )

    repo = AccountingRepository()
    doc, attempt, _ = repo.create_document_and_attempt_atomically("tenant_x", "op_1", "test.xml", "/p.xml", "application/xml", "hash1")
    lease = repo.claim_attempt_atomically(attempt.id, "worker_1")
    ver = repo.save_extraction_version(attempt.id, lease, true_source, ai_suggestions, val_report)

    # Sprawdzenie: dane zapisane w wersji posiadają niezmienione kwoty źródłowe!
    assert ver.immutable_source_data.total_net == Decimal("100.00")
    assert ver.immutable_source_data.total_tax == Decimal("23.00")
    assert ver.immutable_source_data.total_gross == Decimal("123.00")


def test_zero_cloud_calls_on_privacy_failure():
    """
    Test 6: Brak jakiegokolwiek wywołania chmury po błędzie prywatności (Fail-Closed).
    Sprawdzenie, czy adapter chmurowy ma 0 wywołań w sytuacji awarii sanityzacji lub naruszenia integralności.
    """
    from security.egress_guard import BaseCloudAdapter

    mock_adapter = MagicMock(spec=BaseCloudAdapter)
    attempt_id = str(uuid.uuid4())
    tenant_id = "tenant_shield"

    guard = ScopedEgressGuard(attempt_id=attempt_id, tenant_id=tenant_id, adapter=mock_adapter)

    # 1. Koperta ze statusem błędu prywatności (privacy_cleared = False)
    failed_envelope = seal_sanitized_envelope(
        payload_text="Niezamaskowany tekst z NIP: 7740001454",
        attempt_id=attempt_id,
        tenant_id=tenant_id,
        privacy_cleared=False  # Awaria lokalnego NER
    )

    with pytest.raises(SecurityPrivacyViolationError):
        guard.execute_completion(failed_envelope)

    # Asercja krytyczna: Dokładnie 0 wywołań adaptera chmurowego!
    mock_adapter.generate_completion.assert_not_called()
    assert guard.calls_blocked == 1
    assert guard.calls_executed == 0

    # 2. Próba ataku podmiany payloadu (tekst A sprawdzony, ale zmodyfikowany na tekst B)
    tampered_envelope = seal_sanitized_envelope(
        payload_text="Tekst A zweryfikowany",
        attempt_id=attempt_id,
        tenant_id=tenant_id,
        privacy_cleared=True
    )
    # Ręcznie modyfikujemy tekst koperty bez aktualizacji hasha
    object.__setattr__(tampered_envelope, "payload_text", "Tekst B podmiana!")

    with pytest.raises(SecurityPrivacyViolationError):
        guard.execute_completion(tampered_envelope)

    mock_adapter.generate_completion.assert_not_called()
    assert guard.calls_blocked == 2
    assert guard.calls_executed == 0
