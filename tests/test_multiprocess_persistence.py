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
from pathlib import Path
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


def get_test_document_hash(request: pytest.FixtureRequest) -> str:
    import uuid
    return uuid.uuid5(
        uuid.NAMESPACE_URL,
        request.node.nodeid,
    ).hex


@pytest.fixture
def sqlite_repository(tmp_path):
    repo = AccountingRepository(
        sqlite_db_path=str(tmp_path / "multiprocess.db")
    )
    try:
        yield repo
    finally:
        repo.close()


def test_multiprocess_shared_state(tmp_path, request, sqlite_repository):
    """Proces B (oddzielny proces systemu operacyjnego) natychmiast widzi dokument utworzony przez Proces A."""
    db_file = str(tmp_path / "multiprocess.db")
    repo_a = sqlite_repository

    doc, attempt, event = repo_a.create_document_and_attempt_atomically(
        tenant_id="firma_shared",
        operator_id="operator_a",
        original_filename="faktura_shared.xml",
        storage_path="/storage/shared.xml",
        mime_type="application/xml",
        sha256_hash=get_test_document_hash(request)
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


def test_concurrent_claim_race_condition(tmp_path, request, sqlite_repository):
    """Dwa współbieżne procesy OS próbują przejąć to samo zadanie. Dokładnie jeden wygrywa."""
    db_file = str(tmp_path / "multiprocess.db")
    repo = sqlite_repository

    doc, attempt, event = repo.create_document_and_attempt_atomically(
        tenant_id="firma_race",
        operator_id="operator_main",
        original_filename="faktura_race.xml",
        storage_path="/storage/race.xml",
        mime_type="application/xml",
        sha256_hash=get_test_document_hash(request)
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


def test_process_restart_persistence(tmp_path, request):
    """Restart procesu: nowa instancja repozytorium odtwarza pełny stan z dysku."""
    db_file = str(tmp_path / "multiprocess.db")
    repo1 = AccountingRepository(sqlite_db_path=db_file)
    try:
        doc, attempt, event = repo1.create_document_and_attempt_atomically(
            tenant_id="firma_restart",
            operator_id="operator_init",
            original_filename="faktura_restart.xml",
            storage_path="/storage/restart.xml",
            mime_type="application/xml",
            sha256_hash=get_test_document_hash(request)
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
    finally:
        repo1.close()

    # Symulacja restartu serwera / workera: niszczymy repo1 i tworzymy repo2
    repo2 = AccountingRepository(sqlite_db_path=db_file)
    try:
        doc_reloaded = repo2.get_document(doc.id)
        assert doc_reloaded is not None
        assert doc_reloaded.status == DocumentStatus.APPROVED
        assert doc_reloaded.approved_version_id == ver.id

        versions = repo2.get_versions(doc.id)
        assert len(versions) == 1
        assert versions[0].id == ver.id
        assert versions[0].immutable_source_data.invoice_number == "FV/PERSIST/01"
        assert versions[0].immutable_source_data.total_gross == Decimal("123.00")
    finally:
        repo2.close()


def test_fencing_violation_rejection(tmp_path, request, sqlite_repository):
    """Worker z nieważnym lease_tokenem nie może zapisać wersji dokumentu."""
    repo = sqlite_repository

    doc, attempt, _ = repo.create_document_and_attempt_atomically(
        tenant_id="firma_fencing",
        operator_id="operator_1",
        original_filename="faktura.xml",
        storage_path="/storage/faktura.xml",
        mime_type="application/xml",
        sha256_hash=get_test_document_hash(request)
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
    monkeypatch.delenv("ACCOUNTING_DATABASE_URL", raising=False)
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(DatabaseConfigurationError) as exc_info:
        AccountingRepository(db_engine="postgres")

    assert "Nie można nawiązać połączenia z produkcyjną bazą PostgreSQL" in str(exc_info.value)


def test_accounting_repo_explicit_database_url_argument(monkeypatch):
    """
    Weryfikuje, że jawny argument database_url="postgresql://..." jest poprawnie
    rozpoznawany i parsowany bez konieczności definiowania zmiennych środowiskowych POSTGRES_*.
    """
    monkeypatch.delenv("ACCOUNTING_DATABASE_URL", raising=False)
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.delenv("POSTGRES_USER", raising=False)
    monkeypatch.delenv("POSTGRES_DB", raising=False)

    captured = {}
    class DummyPool:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr("psycopg2.pool.ThreadedConnectionPool", DummyPool)
    monkeypatch.setattr("accounting.db.AccountingRepository._init_schema", lambda self: None)

    repo = AccountingRepository(database_url="postgresql://arg_user:arg_pass@arg_host:5439/arg_db")
    assert repo.engine_type == "postgres"
    assert repo.postgres_host == "arg_host"
    assert repo.postgres_port == 5439
    assert repo.postgres_user == "arg_user"
    assert repo.postgres_password == "arg_pass"
    assert repo.postgres_db == "arg_db"
    assert captured.get("dsn") == "postgresql://arg_user:arg_pass@arg_host:5439/arg_db"


def test_accounting_repo_only_accounting_database_url(monkeypatch):
    """
    Weryfikuje, że repozytorium korzysta z ACCOUNTING_DATABASE_URL, gdy nie podano jawnego argumentu.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.delenv("POSTGRES_HOST", raising=False)
    monkeypatch.setenv("ACCOUNTING_DATABASE_URL", "postgresql://acc_user:acc_pass@acc_host:5438/acc_db")

    captured = {}
    class DummyPool:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr("psycopg2.pool.ThreadedConnectionPool", DummyPool)
    monkeypatch.setattr("accounting.db.AccountingRepository._init_schema", lambda self: None)

    repo = AccountingRepository()
    assert repo.engine_type == "postgres"
    assert repo.postgres_host == "acc_host"
    assert repo.postgres_port == 5438
    assert repo.postgres_user == "acc_user"
    assert repo.postgres_password == "acc_pass"
    assert repo.postgres_db == "acc_db"
    assert captured.get("dsn") == "postgresql://acc_user:acc_pass@acc_host:5438/acc_db"


def test_accounting_repo_argument_priority_over_accounting_database_url(monkeypatch):
    """
    Weryfikuje, że jawny argument database_url ma bezwzględne pierwszeństwo nad ACCOUNTING_DATABASE_URL.
    """
    monkeypatch.setenv("ACCOUNTING_DATABASE_URL", "postgresql://env_user:env_pass@env_host:5437/env_db")

    captured = {}
    class DummyPool:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr("psycopg2.pool.ThreadedConnectionPool", DummyPool)
    monkeypatch.setattr("accounting.db.AccountingRepository._init_schema", lambda self: None)

    repo = AccountingRepository(database_url="postgresql://override_user:override_pass@override_host:5436/override_db")
    assert repo.engine_type == "postgres"
    assert repo.postgres_host == "override_host"
    assert repo.postgres_user == "override_user"
    assert repo.postgres_port == 5436
    assert captured.get("dsn") == "postgresql://override_user:override_pass@override_host:5436/override_db"


def test_accounting_repo_sqlite_via_accounting_database_url(monkeypatch, tmp_path):
    """
    Weryfikuje, że podanie adresu SQLite w ACCOUNTING_DATABASE_URL poprawnie inicjalizuje
    silnik SQLite, nawet jeśli w otoczeniu obecna jest zmienna POSTGRES_HOST.
    """
    custom_db = tmp_path / "custom_accounting.db"
    monkeypatch.setenv("ACCOUNTING_DATABASE_URL", f"sqlite:///{custom_db.as_posix()}")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")  # Nie powinno wywołać fałszywego przejścia na postgres!

    repo = AccountingRepository()
    assert repo.engine_type == "sqlite"
    assert Path(repo.sqlite_path).resolve() == custom_db.resolve()
    repo.close()


def test_accounting_repo_postgres_via_individual_env_vars(monkeypatch):
    """
    Weryfikuje poprawne działanie konfiguracji PostgreSQL opartej o indywidualne zmienne POSTGRES_*.
    """
    monkeypatch.delenv("KARIK_DB_ENGINE", raising=False)
    monkeypatch.delenv("ACCOUNTING_DATABASE_URL", raising=False)
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "ind_host")
    monkeypatch.setenv("POSTGRES_PORT", "5435")
    monkeypatch.setenv("POSTGRES_USER", "ind_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ind_pass")
    monkeypatch.setenv("POSTGRES_DB", "ind_db")

    captured = {}
    class DummyPool:
        def __init__(self, *args, **kwargs):
            captured.update(kwargs)
    monkeypatch.setattr("psycopg2.pool.ThreadedConnectionPool", DummyPool)
    monkeypatch.setattr("accounting.db.AccountingRepository._init_schema", lambda self: None)

    repo = AccountingRepository()
    assert repo.engine_type == "postgres"
    assert repo.postgres_host == "ind_host"
    assert repo.postgres_port == 5435
    assert repo.postgres_user == "ind_user"
    assert repo.postgres_password == "ind_pass"
    assert repo.postgres_db == "ind_db"
    assert captured.get("host") == "ind_host"
    assert captured.get("port") == 5435


def test_accounting_repo_conflicting_arguments_raise():
    """
    Weryfikuje, że sprzeczne argumenty konstruktora natychmiast rzucają DatabaseConfigurationError:
    - db_engine='sqlite' oraz PostgreSQL database_url
    - db_engine='postgres' oraz SQLite database_url
    - db_engine='postgres' oraz sqlite_db_path
    - sqlite_db_path oraz PostgreSQL database_url
    """
    # 1. db_engine='sqlite' i PostgreSQL database_url
    with pytest.raises(DatabaseConfigurationError, match="Sprzeczna konfiguracja: db_engine='sqlite' i PostgreSQL database_url"):
        AccountingRepository(db_engine="sqlite", database_url="postgresql://user:pass@host/db")

    # 2. db_engine='postgres' i SQLite database_url
    with pytest.raises(DatabaseConfigurationError, match="Sprzeczna konfiguracja: db_engine='postgres' i SQLite database_url"):
        AccountingRepository(db_engine="postgres", database_url="sqlite:///test.db")

    # 3. db_engine='postgres' i sqlite_db_path
    with pytest.raises(DatabaseConfigurationError, match="Sprzeczna konfiguracja: db_engine='postgres' i sqlite_db_path"):
        AccountingRepository(db_engine="postgres", sqlite_db_path="test.db")

    # 4. sqlite_db_path i PostgreSQL database_url
    with pytest.raises(DatabaseConfigurationError, match="Sprzeczna konfiguracja: sqlite_db_path i PostgreSQL database_url"):
        AccountingRepository(sqlite_db_path="test.db", database_url="postgresql://user:pass@host/db")
