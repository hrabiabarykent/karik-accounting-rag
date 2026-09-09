"""
Testy odporności wieloprocesowej, trwałości bazy danych oraz zabezpieczeń transakcyjnych (Etap 2):
1. Dwa niezależne procesy systemu operacyjnego (subprocess) współdzielą dane w bazie (brak utraty danych po restarcie).
2. Wyścig o przejęcie zadania (Race Condition): dwa współbieżne procesy próbują przejąć tę samą próbę.
   Dokładnie JEDEN proces uzyskuje lease_token, a drugi zostaje atomowo odrzucony (zwraca None).
3. Fencing token: worker ze starym/błędnym lease_tokenem nie ma prawa zapisać wersji (fencing violation -> None).
4. Restart procesu: nowa instancja repozytorium odtwarza pełny stan z dysku.
5. Jawny błąd konfiguracji PostgreSQL: niepoprawny host/baza podnosi DatabaseConfigurationError,
   nie przechodząc po cichu w tryb SQLite ani słowników w pamięci.
"""

import os
import sys
import subprocess
from decimal import Decimal
from datetime import date
import pytest

from accounting.db import AccountingRepository, DatabaseConfigurationError
from accounting.models import (
    DocumentStatus,
    AttemptStatus,
    InvoiceSourceData,
    InvoiceParty,
    ValidationReport,
    AISuggestions,
)


@pytest.fixture
def test_dir():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache", "mp_test"))
    os.makedirs(base, exist_ok=True)
    return base


def test_multiprocess_shared_state(test_dir):
    """Proces B (oddzielny proces systemu operacyjnego) natychmiast widzi dokument utworzony przez Proces A."""
    db_file = os.path.join(test_dir, "shared_multiprocess.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass

    repo_a = AccountingRepository(sqlite_db_path=db_file)

    doc, attempt, event = repo_a.create_document_and_attempt_atomically(
        tenant_id="firma_shared",
        operator_id="operator_a",
        original_filename="faktura_shared.xml",
        storage_path="/storage/shared.xml",
        mime_type="application/xml",
        sha256_hash="hash_shared_123"
    )

    # Uruchamiamy CAŁKOWICIE OSOBNY PROCES OS z osobnym interpreterem Pythona
    code = (
        f"from accounting.db import AccountingRepository\n"
        f"repo = AccountingRepository(sqlite_db_path=r'{db_file}')\n"
        f"doc = repo.get_document('{doc.id}')\n"
        f"print(doc.id if doc else 'NOT_FOUND')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    out = result.stdout.strip().splitlines()[-1]
    assert out == doc.id


def test_concurrent_claim_race_condition(test_dir):
    """Dwa współbieżne procesy OS próbują przejąć to samo zadanie. Dokładnie jeden wygrywa."""
    db_file = os.path.join(test_dir, "race_condition.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass

    repo = AccountingRepository(sqlite_db_path=db_file)

    doc, attempt, event = repo.create_document_and_attempt_atomically(
        tenant_id="firma_race",
        operator_id="operator_main",
        original_filename="faktura_race.xml",
        storage_path="/storage/race.xml",
        mime_type="application/xml",
        sha256_hash="hash_race_1"
    )

    # Skrypt dla workera
    code_tpl = (
        "import sys\n"
        "from accounting.db import AccountingRepository\n"
        "repo = AccountingRepository(sqlite_db_path=r'{db_file}')\n"
        "lease = repo.claim_attempt_atomically('{attempt_id}', worker_id='{worker_id}')\n"
        "if lease:\n"
        "    print('RESULT:CLAIMED:' + lease)\n"
        "else:\n"
        "    print('RESULT:REJECTED')\n"
    )

    code1 = code_tpl.format(db_file=db_file, attempt_id=attempt.id, worker_id="worker_alpha")
    code2 = code_tpl.format(db_file=db_file, attempt_id=attempt.id, worker_id="worker_beta")

    # Uruchamiamy dwa procesy współbieżnie
    p1 = subprocess.Popen([sys.executable, "-c", code1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    p2 = subprocess.Popen([sys.executable, "-c", code2], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    out1, err1 = p1.communicate(timeout=10)
    out2, err2 = p2.communicate(timeout=10)

    res1 = [line for line in out1.splitlines() if line.startswith("RESULT:")][0]
    res2 = [line for line in out2.splitlines() if line.startswith("RESULT:")][0]

    results = [res1, res2]
    claimed = [r for r in results if r.startswith("RESULT:CLAIMED:")]
    rejected = [r for r in results if r == "RESULT:REJECTED"]

    # KRYTYCZNY WARUNEK TRANSKACYJNY: dokładnie JEDEN worker zdobył dzierżawę
    assert len(claimed) == 1, f"Oczekiwano dokładnie jednego zwycięzcy, otrzymano: {results}"
    assert len(rejected) == 1, f"Oczekiwano dokładnie jednego odrzucenia, otrzymano: {results}"

    winning_lease = claimed[0].replace("RESULT:CLAIMED:", "")

    # Sprawdzenie stanu w bazie
    att_in_db = repo.get_attempt(attempt.id)
    assert att_in_db.status == AttemptStatus.RUNNING
    assert att_in_db.lease_token == winning_lease
    assert att_in_db.worker_id in ("worker_alpha", "worker_beta")


def test_process_restart_persistence(test_dir):
    """Restart procesu: nowa instancja repozytorium odtwarza pełny stan z dysku."""
    db_file = os.path.join(test_dir, "restart.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass

    repo1 = AccountingRepository(sqlite_db_path=db_file)

    doc, attempt, event = repo1.create_document_and_attempt_atomically(
        tenant_id="firma_restart",
        operator_id="operator_init",
        original_filename="faktura_restart.xml",
        storage_path="/storage/restart.xml",
        mime_type="application/xml",
        sha256_hash="hash_restart_1"
    )
    lease = repo1.claim_attempt_atomically(attempt.id, "worker_1")

    source = InvoiceSourceData(
        invoice_number="FV/PERSIST/01",
        issue_date=date(2026, 7, 1),
        currency="PLN",
        seller=InvoiceParty(name="Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("100.00"),
        total_tax=Decimal("23.00"),
        total_gross=Decimal("123.00")
    )
    rep = ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)
    ver = repo1.save_extraction_version(attempt.id, lease, source, AISuggestions(), rep)
    repo1.approve_document_version(doc.id, ver.id, "audytor_1", notes="Zatwierdzono przed restartem")

    # Symulacja restartu serwera / workera: niszczymy repo1 i tworzymy repo2
    del repo1
    repo2 = AccountingRepository(sqlite_db_path=db_file)

    doc_reloaded = repo2.get_document(doc.id)
    assert doc_reloaded is not None
    assert doc_reloaded.status == DocumentStatus.APPROVED
    assert doc_reloaded.approved_version_id == ver.id

    versions = repo2.get_versions(doc.id)
    assert len(versions) == 1
    assert versions[0].id == ver.id
    assert versions[0].immutable_source_data.invoice_number == "FV/PERSIST/01"
    assert versions[0].immutable_source_data.total_gross == Decimal("123.00")


def test_fencing_violation_rejection(test_dir):
    """Worker z nieważnym lease_tokenem nie może zapisać wersji dokumentu."""
    db_file = os.path.join(test_dir, "fencing.db")
    if os.path.exists(db_file):
        try:
            os.remove(db_file)
        except Exception:
            pass

    repo = AccountingRepository(sqlite_db_path=db_file)

    doc, attempt, _ = repo.create_document_and_attempt_atomically(
        tenant_id="firma_fencing",
        operator_id="operator_1",
        original_filename="faktura.xml",
        storage_path="/storage/faktura.xml",
        mime_type="application/xml",
        sha256_hash="hash_fencing_1"
    )
    valid_lease = repo.claim_attempt_atomically(attempt.id, "worker_valid")

    source = InvoiceSourceData(
        invoice_number="FV/FENCE/01",
        issue_date=date(2026, 7, 1),
        currency="PLN",
        seller=InvoiceParty(name="Sprzedawca", tax_id="7740001454"),
        buyer=InvoiceParty(name="Nabywca", tax_id="5260250995"),
        items=[],
        tax_summaries=[],
        total_net=Decimal("50.00"),
        total_tax=Decimal("11.50"),
        total_gross=Decimal("61.50")
    )
    rep = ValidationReport(is_valid=True, status=DocumentStatus.VALIDATED)

    # Próba zapisu przez impostora z fałszywym tokenem dzierżawy
    rejected_ver = repo.save_extraction_version(attempt.id, "fake_stale_token", source, AISuggestions(), rep)
    assert rejected_ver is None, "Zapis ze sfałszowanym/przedawnionym tokenem dzierżawy musi zostać odrzucony!"

    # Próba zapisu przez uprawnionego workera z poprawnym tokenem
    accepted_ver = repo.save_extraction_version(attempt.id, valid_lease, source, AISuggestions(), rep)
    assert accepted_ver is not None
    assert accepted_ver.immutable_source_data.invoice_number == "FV/FENCE/01"


def test_explicit_postgres_configuration_error(monkeypatch):
    """
    Gdy skonfigurowano PostgreSQL, ale baza jest niedostępna,
    system rzuca DatabaseConfigurationError zamiast cicho przechodzić na SQLite.
    """
    monkeypatch.setenv("KARIK_DB_ENGINE", "postgres")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "65534")  # port bez usługi
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(DatabaseConfigurationError) as exc_info:
        AccountingRepository(db_engine="postgres")

    assert "Nie można nawiązać połączenia z produkcyjną bazą PostgreSQL" in str(exc_info.value)
