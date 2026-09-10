import yaml
from pathlib import Path
import pytest


@pytest.fixture
def compose_config():
    compose_path = Path(__file__).resolve().parent.parent / "docker-compose.yml"
    assert compose_path.exists(), "docker-compose.yml not found"
    with open(compose_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_docker_compose_structure(compose_config):
    """Weryfikuje podstawową strukturę pliku docker-compose.yml."""
    assert "services" in compose_config
    assert "volumes" in compose_config
    assert "networks" in compose_config

    services = compose_config["services"]
    assert "redis-broker" in services
    assert "postgres-pgvector" in services
    assert "local-ai" in services
    assert "app-module" in services
    assert "processing-worker-cloud" in services
    assert "processing-worker-local" in services


def test_profile_core(compose_config):
    """Profil 'core' musi uruchamiać bazę, brokera i API Gateway (bez workerów i local-ai)."""
    services = compose_config["services"]
    core_services = {name for name, s in services.items() if "core" in s.get("profiles", [])}

    assert "redis-broker" in core_services
    assert "postgres-pgvector" in core_services
    assert "app-module" in core_services
    assert "local-ai" not in core_services
    assert "processing-worker-cloud" not in core_services
    assert "processing-worker-local" not in core_services


def test_profile_worker_cloud(compose_config):
    """Profil 'worker-cloud' uruchamia brokera, bazę i workera chmurowego bez local-ai."""
    services = compose_config["services"]
    cloud_services = {name for name, s in services.items() if "worker-cloud" in s.get("profiles", [])}

    assert "redis-broker" in cloud_services
    assert "postgres-pgvector" in cloud_services
    assert "processing-worker-cloud" in cloud_services
    assert "local-ai" not in cloud_services
    assert "processing-worker-local" not in cloud_services

    worker = services["processing-worker-cloud"]
    depends = worker.get("depends_on", [])
    assert "local-ai" not in depends
    assert "redis-broker" in depends
    assert "postgres-pgvector" in depends


def test_profile_worker_local(compose_config):
    """Profil 'worker-local' uruchamia workera ze wsparciem lokalnego LLM i zależnością do local-ai."""
    services = compose_config["services"]
    local_services = {name for name, s in services.items() if "worker-local" in s.get("profiles", [])}

    assert "redis-broker" in local_services
    assert "postgres-pgvector" in local_services
    assert "local-ai" in local_services
    assert "processing-worker-local" in local_services
    assert "processing-worker-cloud" not in local_services

    worker = services["processing-worker-local"]
    depends = worker.get("depends_on", [])
    assert "local-ai" in depends
    assert "redis-broker" in depends
    assert "postgres-pgvector" in depends


def test_profile_ml(compose_config):
    """Profil 'ml' dedykowany wyłącznie serwerowi modeli local-ai."""
    services = compose_config["services"]
    ml_services = {name for name, s in services.items() if "ml" in s.get("profiles", [])}

    assert ml_services == {"local-ai"}


def test_profile_all_cloud(compose_config):
    """Profil 'all-cloud' uruchamia kompletny stos bez obciążania GPU przez local-ai."""
    services = compose_config["services"]
    cloud_services = {name for name, s in services.items() if "all-cloud" in s.get("profiles", [])}

    assert cloud_services == {"redis-broker", "postgres-pgvector", "app-module", "processing-worker-cloud"}
    assert "local-ai" not in cloud_services
    assert "processing-worker-local" not in cloud_services


def test_profile_all_local(compose_config):
    """Profil 'all-local' uruchamia kompletny stos wraz z local-ai oraz właściwym workerem powiązanym z local-ai."""
    services = compose_config["services"]
    local_services = {name for name, s in services.items() if "all-local" in s.get("profiles", [])}

    assert local_services == {"redis-broker", "postgres-pgvector", "app-module", "local-ai", "processing-worker-local"}
    assert "processing-worker-cloud" not in local_services

    worker = services["processing-worker-local"]
    depends = worker.get("depends_on", {})
    assert "local-ai" in depends
    assert depends["local-ai"].get("condition") == "service_healthy"
    assert depends["postgres-pgvector"].get("condition") == "service_healthy"


def test_service_healthcheck_conditions(compose_config):
    """Weryfikuje, że usługi zależne oczekują na stan service_healthy, a nie jedynie start kontenera."""
    services = compose_config["services"]

    # Postgres i Redis mają zdefiniowane healthchecki
    assert "healthcheck" in services["postgres-pgvector"]
    assert "healthcheck" in services["redis-broker"]
    assert "healthcheck" in services["local-ai"]

    # Gateway oczekuje na healthy postgres i redis
    gateway_deps = services["app-module"].get("depends_on", {})
    assert gateway_deps["postgres-pgvector"].get("condition") == "service_healthy"
    assert gateway_deps["redis-broker"].get("condition") == "service_healthy"

    # Cloud worker oczekuje na healthy postgres i redis
    cloud_deps = services["processing-worker-cloud"].get("depends_on", {})
    assert cloud_deps["postgres-pgvector"].get("condition") == "service_healthy"
    assert cloud_deps["redis-broker"].get("condition") == "service_healthy"

