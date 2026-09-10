"""
database/exceptions.py
Wspólne wyjątki domenowe dla warstwy bazodanowej KARIK (accounting i rag).
"""

class DatabaseConfigurationError(ValueError):
    """Wyjątek rzucany w przypadku błędnej konfiguracji bazy danych lub poświadczeń."""
    pass


class DatabaseConnectionError(RuntimeError):
    """Wyjątek rzucany w przypadku problemów z fizycznym połączeniem z bazą PostgreSQL."""
    pass


class IngestionValidationError(ValueError):
    """Wyjątek rzucany w przypadku niespełnienia kryteriów walidacji przed aktywacją staged ingest."""
    pass
