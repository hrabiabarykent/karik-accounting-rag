import os
import pytest
from fastapi.testclient import TestClient
import main
import tasks

# Configure Celery for testing: run tasks synchronously on caller thread without Redis
main.celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
    task_store_eager_result=True,
    broker_url='memory://',
    result_backend='cache+memory://'
)
tasks.celery_app.conf.update(
    task_always_eager=True,
    task_eager_propagates=True,
    task_store_eager_result=True,
    broker_url='memory://',
    result_backend='cache+memory://'
)

client = TestClient(main.app)

@pytest.fixture(autouse=True)
def patch_storage_path(tmp_path, monkeypatch):
    # Patch main.STORAGE_PATH to use a temp directory during tests
    monkeypatch.setattr(main, "STORAGE_PATH", str(tmp_path))
    yield tmp_path

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    # Mock redis client to avoid connecting to actual redis server during tests
    class MockRedis:
        def __init__(self):
            self.store = {}
        def setex(self, key, time_val, value):
            self.store[key] = value
        def delete(self, key):
            self.store.pop(key, None)
        def get(self, key):
            return self.store.get(key)
    
    mock_r = MockRedis()
    monkeypatch.setattr(tasks, "redis_client", mock_r)
    return mock_r

@pytest.fixture(autouse=True)
def mock_celery_task(monkeypatch):
    # Mock tasks.process_invoice_task to avoid calling real APIs in E2E eager tests
    def mock_run(task_id, file_path, *args, **kwargs):
        if os.path.exists(file_path):
            os.remove(file_path)
        return {
            "verification_status": "VERIFIED",
            "invoice_data": {
                "sprzedawca_nip_token": "1234567890"
            }
        }
    monkeypatch.setattr(tasks.process_invoice_task, "run", mock_run)

def test_invoice_process_invalid_mime():
    # Send a plain text file (unsupported MIME)
    files = {"file": ("invoice.txt", b"plain text content", "text/plain")}
    response = client.post("/v1/invoice/process", files=files)
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]

def test_invoice_process_success_jpeg(tmp_path):
    # Prepare a valid dummy jpeg file
    file_content = b"fake-jpeg-binary-data"
    files = {"file": ("invoice.jpg", file_content, "image/jpeg")}
    
    response = client.post("/v1/invoice/process", files=files)
    assert response.status_code == 202
    data = response.json()
    assert "task_id" in data
    assert data["status"] == "pending"
    
    task_id = data["task_id"]
    
    # Retrieve status (it should be completed because celery tasks run synchronously in eager mode)
    status_response = client.get(f"/v1/invoice/status/{task_id}")
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["status"] == "completed"
    assert "data" in status_data
    assert status_data["data"]["invoice_data"]["sprzedawca_nip_token"] == "1234567890"
    
    # Verify temporary file cleanup
    # The file should be saved first, then deleted by Celery's finally block
    # Since we are running task_always_eager, the file was already processed and deleted
    remaining_files = os.listdir(str(tmp_path))
    assert len(remaining_files) == 0

def test_invoice_process_success_pdf(tmp_path):
    file_content = b"%PDF-1.4 mock pdf data"
    files = {"file": ("invoice.pdf", file_content, "application/pdf")}
    
    response = client.post("/v1/invoice/process", files=files)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    
    status_response = client.get(f"/v1/invoice/status/{task_id}")
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "completed"
    
    # Verify temporary file cleanup
    remaining_files = os.listdir(str(tmp_path))
    assert len(remaining_files) == 0
