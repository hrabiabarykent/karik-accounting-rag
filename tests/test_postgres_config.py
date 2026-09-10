import os
from pathlib import Path
import pytest
from database.config import PostgresConfig, load_env_file, mask_password_in_url
from database.exceptions import DatabaseConfigurationError


def test_postgres_config_from_env(monkeypatch):
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "db.example.internal")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "prod_accounting")
    monkeypatch.setenv("POSTGRES_USER", "prod_operator")
    monkeypatch.setenv("POSTGRES_PASSWORD", "SuperSecurePassword123!")
    monkeypatch.setenv("POSTGRES_SSLMODE", "require")

    cfg = PostgresConfig.from_env(require_password=True)
    assert cfg.host == "db.example.internal"
    assert cfg.port == 5433
    assert cfg.dbname == "prod_accounting"
    assert cfg.user == "prod_operator"
    assert cfg.password == "SuperSecurePassword123!"
    assert cfg.sslmode == "require"

    params = cfg.to_conn_params()
    assert params["host"] == "db.example.internal"
    assert params["port"] == 5433
    assert params["password"] == "SuperSecurePassword123!"
    assert params["sslmode"] == "require"


def test_postgres_config_missing_password_raises(monkeypatch):
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    with pytest.raises(DatabaseConfigurationError, match="POSTGRES_PASSWORD must be explicitly configured"):
        PostgresConfig.from_env(require_password=True)


def test_postgres_config_optional_password_allowed(monkeypatch):
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "5432")

    cfg = PostgresConfig.from_env(require_password=False)
    assert cfg.password is None
    params = cfg.to_conn_params()
    assert "password" not in params


def test_postgres_config_from_database_url():
    """Weryfikuje parsowanie poprawnego DATABASE_URL z parametrami query."""
    url = "postgresql://myuser:mypassword@db.cloud.internal:5434/accounting_db?sslmode=require&connect_timeout=8"
    cfg = PostgresConfig.from_url(url, require_password=True)

    assert cfg.host == "db.cloud.internal"
    assert cfg.port == 5434
    assert cfg.dbname == "accounting_db"
    assert cfg.user == "myuser"
    assert cfg.password == "mypassword"
    assert cfg.sslmode == "require"
    assert cfg.connect_timeout == 8


def test_database_url_priority_over_postgres_vars(monkeypatch):
    """Weryfikuje, że DATABASE_URL ma pierwszeństwo nad indywidualnymi zmiennymi POSTGRES_*."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://url_user:url_pass@url_host:5435/url_db")
    monkeypatch.setenv("POSTGRES_HOST", "ignored_host")
    monkeypatch.setenv("POSTGRES_PORT", "9999")
    monkeypatch.setenv("POSTGRES_USER", "ignored_user")
    monkeypatch.setenv("POSTGRES_PASSWORD", "ignored_pass")
    monkeypatch.setenv("POSTGRES_DB", "ignored_db")

    cfg = PostgresConfig.from_env(require_password=True)
    assert cfg.host == "url_host"
    assert cfg.port == 5435
    assert cfg.user == "url_user"
    assert cfg.password == "url_pass"
    assert cfg.dbname == "url_db"


def test_rag_database_url_priority(monkeypatch):
    """Weryfikuje, że RAG_DATABASE_URL ma bezwzględne pierwszeństwo nad DATABASE_URL i POSTGRES_*."""
    monkeypatch.setenv("RAG_DATABASE_URL", "postgresql://rag_u:rag_p@rag_host:5439/rag_db")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db_u:db_p@db_host:5438/db_db")
    monkeypatch.setenv("POSTGRES_HOST", "ignored_pg_host")
    monkeypatch.setenv("POSTGRES_PORT", "1234")

    cfg = PostgresConfig.from_env(require_password=True)
    assert cfg.host == "rag_host"
    assert cfg.port == 5439
    assert cfg.user == "rag_u"
    assert cfg.password == "rag_p"
    assert cfg.dbname == "rag_db"


def test_database_url_invalid_scheme_raises_without_fallback(monkeypatch):
    """Weryfikuje, że nieprawidłowy schemat w DATABASE_URL (np. sqlite://) rzuca wyjątek bez fallbacku na POSTGRES_*."""
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///test.db")
    monkeypatch.setenv("POSTGRES_HOST", "127.0.0.1")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")

    with pytest.raises(DatabaseConfigurationError, match="Nieobsługiwany schemat"):
        PostgresConfig.from_env(require_password=True)


def test_database_url_percent_encoded_and_special_chars():
    """Weryfikuje obsługę zakodowanych znaków specjalnych w haśle i nazwie bazy."""
    # Hasło zawiera znaki 'p@ss:w/ord!' -> 'p%40ss%3Aw%2Ford%21'
    url = "postgresql://karik_admin:p%40ss%3Aw%2Ford%21@127.0.0.1:5432/karik%2Ddb"
    cfg = PostgresConfig.from_url(url, require_password=True)

    assert cfg.password == "p@ss:w/ord!"
    assert cfg.dbname == "karik-db"


def test_database_url_ipv6():
    """Weryfikuje obsługę adresów IPv6 w DATABASE_URL."""
    url = "postgresql://admin:secret@[::1]:5432/karik_db"
    cfg = PostgresConfig.from_url(url, require_password=True)
    assert cfg.host == "::1"
    assert cfg.port == 5432


def test_database_url_invalid_scheme_raises():
    """Weryfikuje odrzucenie nieprawidłowego schematu w DATABASE_URL."""
    for invalid in ["mysql://user:pass@host/db", "sqlite:///test.db", "http://host/db"]:
        with pytest.raises(DatabaseConfigurationError, match="Nieobsługiwany schemat"):
            PostgresConfig.from_url(invalid)


def test_database_url_invalid_port_raises():
    """Weryfikuje, że błędny port rzuca DatabaseConfigurationError bez cichego fallbacku na 5432."""
    for invalid_port_url in [
        "postgresql://user:pass@host:99999/db",
        "postgresql://user:pass@host:notaport/db",
        "postgresql://user:pass@host:0/db",
    ]:
        with pytest.raises(DatabaseConfigurationError, match="port"):
            PostgresConfig.from_url(invalid_port_url)


def test_database_url_disallowed_query_param_raises():
    """Weryfikuje odrzucenie parametrów spoza allowlisty."""
    url = "postgresql://user:pass@host:5432/db?unsupported_injection=1"
    with pytest.raises(DatabaseConfigurationError, match="Niedozwolony parametr zapytania"):
        PostgresConfig.from_url(url)


def test_password_masking_in_repr_and_errors():
    """Weryfikuje, że hasło jest zawsze zamaskowane w DSN, repr i komunikatach błędów."""
    raw_secret = "SuperSecretPlainTextPasswordThatMustNeverLeak!"
    url = f"postgresql://user:{raw_secret}@host:notaport/db"

    # 1. Funkcja mask_password_in_url
    masked = mask_password_in_url(url)
    assert raw_secret not in masked
    assert "user:***@host" in masked

    # 2. Wyjątek przy błędnym URL nie ujawnia hasła
    with pytest.raises(DatabaseConfigurationError) as exc_info:
        PostgresConfig.from_url(url)
    err_str = str(exc_info.value)
    assert raw_secret not in err_str
    assert "***" in err_str

    # 3. Repr i to_masked_dsn obiektu konfiguracyjnego
    valid_url = f"postgresql://user:{raw_secret}@127.0.0.1:5432/testdb"
    cfg = PostgresConfig.from_url(valid_url, require_password=True)
    assert raw_secret not in repr(cfg)
    assert raw_secret not in cfg.to_masked_dsn()
    assert "***" in repr(cfg)
    assert "***" in cfg.to_masked_dsn()


def test_connect_timeout_must_be_positive():
    """Weryfikuje, że parametr connect_timeout musi być dodatni."""
    url = "postgresql://user:pass@host:5432/db?connect_timeout=-5"
    with pytest.raises(DatabaseConfigurationError, match="connect_timeout musi być dodatnią liczbą"):
        PostgresConfig.from_url(url)


def test_known_default_database_passwords_are_absent():
    """
    Test weryfikacji regresji znanych domyślnych haseł deweloperskich:
    Skanuje wszystkie pliki źródłowe .py w repozytorium (z wyłączeniem katalogów .venv, .git, .cache, __pycache__)
    i upewnia się, że nie powróciły znane domyślne hasła bazodanowe ('karik_password', 'wolf_password').
    Uwaga: Pełne skanowanie nieznanych sekretów (entropia/wzorce) jest delegowane do dedykowanych narzędzi CI (np. Gitleaks / TruffleHog).
    """
    base_dir = Path(__file__).resolve().parent.parent
    forbidden_strings = ["karik_password", "wolf_password"]
    excluded_dirs = {".venv", "venv", ".git", ".cache", "__pycache__"}

    violations = []
    current_file = Path(__file__).resolve()

    for py_file in base_dir.rglob("*.py"):
        if any(part in excluded_dirs for part in py_file.parts):
            continue
        if py_file.resolve() == current_file:
            continue

        try:
            content = py_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for forbidden in forbidden_strings:
            if forbidden in content:
                violations.append(f"{py_file.relative_to(base_dir)} zawiera niedozwolone hasło '{forbidden}'")

    assert not violations, (
        f"KRYTYCZNE ZAGROŻENIE BEZPIECZEŃSTWA: Wykryto zahardkodowane hasła w plikach źródłowych:\n"
        + "\n".join(violations)
    )
