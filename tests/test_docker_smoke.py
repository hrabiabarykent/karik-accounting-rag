import os
import subprocess
import pytest
import httpx
import redis
import psycopg2
from celery import Celery


def is_docker_running() -> bool:
    """Sprawdza, czy Docker daemon jest uruchomiony."""
    try:
        res = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        return res.returncode == 0
    except Exception:
        return False


def is_live_stack_running() -> bool:
    """Sprawdza, czy podstawowe kontenery stosu KARIK są obecnie uruchomione."""
    if not is_docker_running():
        return False
    try:
        redis_port = int(os.getenv("REDIS_PORT", "16379"))
        r = redis.Redis(host="127.0.0.1", port=redis_port, socket_timeout=2)
        return r.ping() is True
    except Exception:
        return False


def is_local_ai_running() -> bool:
    """Sprawdza, czy lokalny serwer inferencji local-ai jest aktywny na porcie 8088."""
    try:
        res = httpx.get("http://127.0.0.1:8088/v1/models", timeout=2.0)
        return res.status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not is_live_stack_running(), reason="Środowisko Docker / kontenery KARIK nie są aktywne")
]


def test_docker_compose_config_validation():
    """Weryfikuje, że polecenie 'docker compose config' wykonuje się poprawnie bez błędów składniowych."""
    res = subprocess.run(
        ["docker", "compose", "--profile", "all-cloud", "config"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
    )
    assert res.returncode == 0
    assert "services:" in res.stdout


def test_docker_gateway_health():
    """Weryfikuje, że API Gateway odpowiada poprawnie na porcie 8000."""
    response = httpx.get("http://127.0.0.1:8000/docs", timeout=5.0)
    assert response.status_code == 200
    assert "Swagger UI" in response.text


def test_docker_redis_ping():
    """Weryfikuje dostępność brokera Redis na porcie 16379."""
    redis_port = int(os.getenv("REDIS_PORT", "16379"))
    r = redis.Redis(host="127.0.0.1", port=redis_port, socket_timeout=3)
    assert r.ping() is True


def test_docker_postgres_readiness():
    """
    Weryfikuje gotowość bazy PostgreSQL/pgvector na porcie 15432 bez zahardkodowanych poświadczeń.
    Sprawdza standardową bazę kontenera (karik_db) w trybie tylko do odczytu (SELECT 1;),
    nie wymagając uprzedniego tworzenia bazy testowej.
    """
    from database.config import PostgresConfig
    cfg = PostgresConfig.from_env(require_password=False)
    conn_params = cfg.to_conn_params()
    conn_params["port"] = int(os.getenv("POSTGRES_TEST_PORT", conn_params.get("port", 15432)))
    conn_params["host"] = os.getenv("POSTGRES_TEST_HOST", conn_params.get("host", "127.0.0.1"))
    conn_params["dbname"] = os.getenv("POSTGRES_DOCKER_DB", "karik_db")
    conn_params["connect_timeout"] = 5

    with psycopg2.connect(**conn_params) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1;")
            row = cur.fetchone()
            assert row[0] == 1


@pytest.mark.skipif(
    not is_local_ai_running(),
    reason="Serwer local-ai nie jest uruchomiony na porcie 8088 (profil chmurowy lub brak kontenera local-ai)"
)
def test_docker_local_ai_endpoint():
    """Weryfikuje dostępność serwera modeli local-ai i obecność załadowanego modelu."""
    response = httpx.get("http://127.0.0.1:8088/v1/models", timeout=5.0)
    assert response.status_code == 200
    data = response.json()
    assert "data" in data
    model_ids = [m.get("id") for m in data.get("data", [])]
    assert "gemma4-e4b" in model_ids


def test_docker_celery_worker_registration():
    """Weryfikuje, że Celery worker jest aktywny i odpowiada na sygnał ping."""
    broker_url = os.getenv("CELERY_BROKER_URL") or os.getenv("REDIS_TEST_URL")
    if not broker_url or broker_url.startswith("memory://"):
        redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
        redis_port = int(os.getenv("REDIS_PORT", "16379"))
        broker_url = f"redis://{redis_host}:{redis_port}/0"

    celery = Celery("smoke_tasks", broker=broker_url)
    insp = celery.control.inspect(timeout=3.0)
    ping_res = insp.ping()
    assert ping_res, "Brak odpowiedzi ping z żadnego aktywnego workera Celery!"
    assert any("pong" in str(v).lower() for v in ping_res.values())
