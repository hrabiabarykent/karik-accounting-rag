import os
import json
import pytest
from unittest.mock import MagicMock, patch
import tasks

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
def mock_gemini_sdk(monkeypatch):
    monkeypatch.setattr(tasks, "HAS_NEW_GENAI", False)

@pytest.fixture
def dummy_invoice_file(tmp_path):

    file_path = tmp_path / "invoice.jpg"
    with open(file_path, "wb") as f:
        f.write(b"mock-image-data")
    return str(file_path)

def test_pipeline_success(dummy_invoice_file, mock_redis):
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
        "data_wystawienia": "2026-07-18",
        "sprzedawca_token": "<COMPANY_NAME_1>",
        "sprzedawca_nip_token": "<PL_NIP_1>",
        "pozycje_faktury": [
            {
                "opis": "Zakup paliwa do samochodu służbowego",
                "netto": 300.00,
                "vat_stawka": "23%",
                "vat_kwota": 69.00,
                "brutto": 369.00,
                "konto_wn": "401-02",
                "konto_ma": "210"
            }
        ],
        "podsumowanie": {
            "suma_netto": 300.00,
            "suma_vat": 69.00,
            "suma_brutto": 369.00
        },
        "sugerowany_kod_gtu": "GTU_02",
        "uzasadnienie_ksiegowe": "Koszt IT"
    }
    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps(step_b_content)

    step_d_content = {
        "data_wystawienia": "2026-07-18",
        "sprzedawca_token": "Firma Testowa Sp. z o.o. (Audited)",
        "sprzedawca_nip_token": "7740001454",
        "pozycje_faktury": [
            {
                "opis": "Zakup paliwa do samochodu służbowego",
                "netto": 300.00,
                "vat_stawka": "23%",
                "vat_kwota": 69.00,
                "brutto": 369.00,
                "konto_wn": "401-02",
                "konto_ma": "210"
            }
        ],
        "podsumowanie": {
            "suma_netto": 300.00,
            "suma_vat": 69.00,
            "suma_brutto": 369.00
        },
        "sugerowany_kod_gtu": "GTU_02",
        "uzasadnienie_ksiegowe": "Koszt IT"
    }
    mock_choice_d = MagicMock()
    mock_choice_d.message.content = json.dumps(step_d_content)
    mock_completion_d = MagicMock()
    mock_completion_d.choices = [mock_choice_d]

    with patch.object(tasks.local_ai_client.chat.completions, 'create') as mock_openai_create, \
         patch('google.generativeai.GenerativeModel') as mock_gemini_model:
        
        mock_openai_create.side_effect = [mock_completion_a, mock_completion_d]
        
        mock_model_instance = MagicMock()
        mock_model_instance.generate_content.return_value = mock_gemini_response
        mock_gemini_model.return_value = mock_model_instance
        
        result = tasks.process_invoice_task("test_task_id_123", dummy_invoice_file)
        
        assert result["verification_status"] == "VERIFIED"
        assert result["invoice_data"]["sprzedawca_token"] == "Firma Testowa Sp. z o.o. (Audited)"
        assert result["invoice_data"]["sprzedawca_nip_token"] == "7740001454"
        
        # Redis clean-up assertion
        assert f"mask:test_task_id_123" not in mock_redis.store
        assert not os.path.exists(dummy_invoice_file)

def test_pipeline_fail_safe(dummy_invoice_file, mock_redis):
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
        "data_wystawienia": "2026-07-18",
        "sprzedawca_token": "<COMPANY_NAME_1>",
        "sprzedawca_nip_token": "<PL_NIP_1>",
        "pozycje_faktury": [],
        "podsumowanie": {"suma_netto": 0, "suma_vat": 0, "suma_brutto": 0},
        "sugerowany_kod_gtu": "GTU_02",
        "uzasadnienie_ksiegowe": "Koszt IT"
    }
    mock_gemini_response = MagicMock()
    mock_gemini_response.text = json.dumps(step_b_content)

    with patch.object(tasks.local_ai_client.chat.completions, 'create') as mock_openai_create, \
         patch('google.generativeai.GenerativeModel') as mock_gemini_model:
        
        mock_openai_create.side_effect = [mock_completion_a, Exception("OOM or timeout in Step D")]
        
        mock_model_instance = MagicMock()
        mock_model_instance.generate_content.return_value = mock_gemini_response
        mock_gemini_model.return_value = mock_model_instance
        
        result = tasks.process_invoice_task("test_task_id_456", dummy_invoice_file)
        
        assert result["verification_status"] == "REQUIRES_MANUAL_VERIFICATION"
        assert result["invoice_data"]["sprzedawca_token"] == "Firma Testowa Sp. z o.o."
        assert result["invoice_data"]["sprzedawca_nip_token"] == "7740001454"
        assert not os.path.exists(dummy_invoice_file)
