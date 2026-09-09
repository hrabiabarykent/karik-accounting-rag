import os
import json
import pytest
from unittest.mock import MagicMock, patch
import tasks
from accounting.db import repository
from accounting.models import DocumentStatus

@pytest.fixture(autouse=True)
def mock_redis(monkeypatch):
    class MockRedis:
        def __init__(self):
            self.store = {}
            self.ttl_store = {}
        def setex(self, key, time_val, value):
            self.store[key] = value
            self.ttl_store[key] = time_val
        def expire(self, key, time_val):
            if key in self.store:
                self.ttl_store[key] = time_val
        def delete(self, key):
            self.store.pop(key, None)
            self.ttl_store.pop(key, None)
        def get(self, key):
            return self.store.get(key)
    
    mock_r = MockRedis()
    monkeypatch.setattr(tasks, "redis_client", mock_r)
    return mock_r

@pytest.fixture(autouse=True)
def mock_requests_post():
    with patch('requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps([
                {"entity_type": "COMPANY_NAME", "text": "Firma Testowa Sp. z o.o."}
            ])}}]
        }
        mock_post.return_value = mock_resp
        yield mock_post

@pytest.fixture(autouse=True)
def mock_gemini_client(monkeypatch):
    mock_client = MagicMock()
    mock_genai_module = MagicMock()
    mock_genai_module.Client.return_value = mock_client
    monkeypatch.setattr(tasks, "HAS_NEW_GENAI", True)
    monkeypatch.setattr(tasks, "genai", mock_genai_module)
    return mock_client

@pytest.fixture
def dummy_invoice_file():
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache", "test_pipeline"))
    os.makedirs(base, exist_ok=True)
    file_path = os.path.join(base, "invoice_test.jpg")
    with open(file_path, "wb") as f:
        f.write(b"mock-image-data")
    yield file_path
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
        except Exception:
            pass

def test_pipeline_success(dummy_invoice_file, mock_redis, mock_gemini_client):
    # Utworzenie rekordu dokumentu i próby w bazie
    doc, attempt, _ = repository.create_document_and_attempt_atomically(
        tenant_id="pipeline_tenant",
        operator_id="test_op",
        original_filename="invoice_test.jpg",
        storage_path=dummy_invoice_file,
        mime_type="image/jpeg",
        sha256_hash="hash_pipeline_test"
    )

    step_a_content = {
        "masked_text": "Firma <COMPANY_NAME_1> z NIP <PL_NIP_1> kupiła paliwo.",
        "mapping_dictionary": {
            "<COMPANY_NAME_1>": "Firma Testowa Sp. z o.o.",
            "<PL_NIP_1>": "7740001454"
        }
    }
    mock_choice_a = MagicMock()
    mock_choice_a.message.content = json.dumps(step_a_content)
    mock_completion_a = MagicMock()
    mock_completion_a.choices = [mock_choice_a]

    step_b_content = {
        "numer_faktury": "FV/2026/01",
        "data_wystawienia": "2026-07-18",
        "sprzedawca_token": "<COMPANY_NAME_1>",
        "sprzedawca_nip_token": "<PL_NIP_1>",
        "pozycje_faktury": [
            {
                "opis": "Zakup paliwa",
                "netto": 100.00,
                "vat_stawka": "23%",
                "vat_kwota": 23.00,
                "brutto": 123.00,
                "konto_wn": "401-02",
                "konto_ma": "210"
            }
        ],
        "podsumowanie": {
            "suma_netto": 100.00,
            "suma_vat": 23.00,
            "suma_brutto": 123.00
        },
        "sugerowany_kod_gtu": "GTU_02",
        "konto_wn": "401-02",
        "konto_ma": "210"
    }
    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps(step_b_content)
    mock_gemini_client.models.generate_content.return_value = mock_gemini_response

    with patch.object(tasks.local_ai_client.chat.completions, 'create') as mock_openai_create:
        mock_openai_create.return_value = mock_completion_a

        with patch("tasks.ScopedEgressGuard.execute_completion", return_value=json.dumps(step_b_content)):
            result = tasks.process_invoice_task(attempt.id, dummy_invoice_file, tenant_id="pipeline_tenant")

            # OCR to zawsze hipoteza wymagająca weryfikacji przez człowieka -> REQUIRES_REVIEW
            assert result["status"] == DocumentStatus.REQUIRES_REVIEW.value
            assert result["is_valid"] is True
            assert len(result["reconciliation_errors"]) == 0

            # Weryfikacja: plik źródłowy NIE został usunięty (wymóg zachowania oryginałów!)
            assert os.path.exists(dummy_invoice_file)

            # Weryfikacja: pamięć podręczna RAM w Redis została wyczyszczona
            assert f"mask:{attempt.id}" not in mock_redis.store
