import os
import sys

# 1. Ustawienie bezpiecznych kluczy testowych i twardej izolacji bazy
os.environ["KARIK_ALLOW_EPHEMERAL_KEY"] = "1"
os.environ.setdefault("KARIK_AUTH_SECRET", "test-secret-key-at-least-32-chars-long-for-pytests")
os.environ.setdefault("ERP_TEST_MODE", "true")
os.environ.setdefault("REDIS_URL", "memory://")
os.environ["KARIK_ENVIRONMENT"] = "test"
os.environ["KARIK_ALLOW_DB_RESET"] = "1"

# Wymuszenie izolowanej bazy SQLite dla testów, zapobiegając przypadkowemu połączeniu
# i wyczyszczeniu bazy PostgreSQL aplikacji.
test_db_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache", "test_karik.db"))
os.makedirs(os.path.dirname(test_db_path), exist_ok=True)
os.environ["KARIK_DB_ENGINE"] = "sqlite"
os.environ["DATABASE_URL"] = f"sqlite:///{test_db_path.replace(os.sep, '/')}"

# 2. Ustawienie lokalnych katalogów cache wewnątrz repozytorium (rozwiązanie błędu WinError 183 z .unsloth)
base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
cache_dir = os.path.join(base_dir, ".cache")
inductor_cache = os.path.join(cache_dir, "torch_inductor")
os.makedirs(inductor_cache, exist_ok=True)

os.environ["TORCHINDUCTOR_CACHE_DIR"] = inductor_cache
os.environ["TORCH_HOME"] = os.path.join(cache_dir, "torch")

if base_dir not in sys.path:
    sys.path.insert(0, base_dir)

# 3. Konfiguracja Celery w trybie pamięciowym (memory://)
# Eliminuje wiszenie na gniazdach TCP Redis podczas testów API Gateway
try:
    from tasks import celery_app
    celery_app.conf.update(
        broker_url="memory://",
        result_backend="memory://",
        task_always_eager=False,
    )
except Exception:
    pass

import pytest

@pytest.fixture(autouse=True)
def clean_database():
    """Czyści tabele bazy danych przed i po każdym teście."""
    from accounting.db import repository
    repository.reset_database()
    yield
    repository.reset_database()
