import os
import sys
import tempfile
import uuid
import shutil
from pathlib import Path

# 1. Ustawienie bezpiecznych kluczy testowych i twardej izolacji bazy
os.environ["KARIK_ALLOW_EPHEMERAL_KEY"] = "1"
os.environ.setdefault("KARIK_AUTH_SECRET", "test-secret-key-at-least-32-chars-long-for-pytests")
os.environ.setdefault("ERP_TEST_MODE", "true")
os.environ.setdefault("REDIS_URL", "memory://")
os.environ["KARIK_ENVIRONMENT"] = "test"
os.environ["KARIK_ALLOW_DB_RESET"] = "1"

# Wymuszenie unikalnej dla sesji bazy SQLite oraz basetemp wewnątrz .cache/pytest-sessions projektu.
# Zapobiega konfliktom między równoległymi sesjami pytest oraz problemom uprawnień do systemowego %TEMP%.
BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_DIR = BASE_DIR / ".cache" / "pytest-sessions" / uuid.uuid4().hex
SESSION_DIR.mkdir(parents=True, exist_ok=False)
TEST_DB_PATH = SESSION_DIR / "test_karik.db"

os.environ["KARIK_DB_ENGINE"] = "sqlite"
os.environ["ACCOUNTING_DATABASE_URL"] = f"sqlite:///{TEST_DB_PATH.as_posix()}"
if os.environ.get("DATABASE_URL", "").startswith("sqlite://"):
    os.environ.pop("DATABASE_URL", None)
os.environ.setdefault("POSTGRES_DB", "karik_db_test")


def pytest_configure(config):
    """Izoluje basetemp pytest wewnątrz unikalnego katalogu sesji projektu."""
    basetemp_dir = SESSION_DIR / "tmp"
    basetemp_dir.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(basetemp_dir)

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


def pytest_sessionfinish(session, exitstatus):
    """Zamyka połączenia z bazą danych i bezpiecznie usuwa sesyjny katalog bazy."""
    import warnings
    import gc
    import time
    try:
        from accounting.db import repository
        repository.close()
    except Exception as exc:
        warnings.warn(f"Błąd podczas zamykania repozytorium: {exc}", RuntimeWarning)

    gc.collect()

    if SESSION_DIR.exists():
        for attempt in range(3):
            try:
                shutil.rmtree(SESSION_DIR)
                break
            except OSError as exc:
                if attempt == 2:
                    warnings.warn(
                        f"Nie udało się usunąć katalogu sesji {SESSION_DIR}: {exc}",
                        RuntimeWarning,
                    )
                else:
                    time.sleep(0.05)
                    gc.collect()
