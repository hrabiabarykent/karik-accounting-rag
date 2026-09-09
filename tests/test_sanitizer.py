import os
import json

import pytest
from unittest.mock import patch, MagicMock
from pii_sanitizer import (
    validate_nip,
    validate_pesel,
    validate_iban_pl,
    PresidioInvoiceSanitizer,
    tolerant_detokenize,
    extract_pdf_text
)

def test_nip_checksum():
    assert validate_nip("7740001454") is True
    assert validate_nip("774-000-14-54") is True
    assert validate_nip("1234567890") is False
    assert validate_nip("7740001455") is False

def test_pesel_checksum():
    assert validate_pesel("02261602498") is True
    assert validate_pesel("12345678901") is False

def test_iban_checksum():
    assert validate_iban_pl("PL61109010140000071219812874") is True
    assert validate_iban_pl("61 1090 1014 0000 0712 1981 2874") is True
    assert validate_iban_pl("PL12345678901234567890123456") is False

def test_presidio_invoice_sanitizer_deterministic():
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url="http://mock-url/v1/chat/completions")
    sample_text = (
        "Faktura od firmy z NIP: 7740001454. "
        "Rachunek bankowy: PL61109010140000071219812874. "
        "Kwota netto wynosi 123.45 zł, a numer faktury to 1234567890."
    )
    
    # Mock SLM call inside Presidio
    with patch('requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps([
                {"entity_type": "COMPANY_NAME", "text": "Firma Testowa Sp. z o.o."}
            ])}}]
        }
        mock_post.return_value = mock_resp

        masked_text, mapping = sanitizer.sanitize(sample_text)
        
        assert "<PL_NIP_" in masked_text
        assert ("<PL_IBAN_" in masked_text or "<IBAN_CODE_" in masked_text)
        assert any("7740001454" in v for v in mapping.values())
        assert "1234567890" in masked_text  # Invoice number preserved!

def test_tolerant_detokenization_corrupted_tokens():
    cloud_json = {
        "sprzedawca": "<COMPANY NAME 1>",
        "nip": "<pl_nip_1>:",
        "email": "<EMAIL 1>"
    }
    
    mapping_dict = {
        "<COMPANY_NAME_1>": "Firma Testowa Sp. z o.o.",
        "<PL_NIP_1>": "7740001454",
        "<EMAIL_1>": "jan.kowalski@example.com"
    }
    
    detokenized, has_leftovers = tolerant_detokenize(cloud_json, mapping_dict)
    
    assert has_leftovers is False
    assert detokenized["sprzedawca"] == "Firma Testowa Sp. z o.o."
    assert detokenized["nip"] == "7740001454"
    assert detokenized["email"] == "jan.kowalski@example.com"

def test_tolerant_detokenization_detects_leftovers():
    cloud_json = {
        "sprzedawca": "Firma Testowa",
        "nieznany_token": "<UNMAPPED_TOKEN_99>"
    }
    
    mapping_dict = {
        "<PL_NIP_1>": "7740001454"
    }
    
    detokenized, has_leftovers = tolerant_detokenize(cloud_json, mapping_dict)
    
    assert has_leftovers is True
    assert detokenized["nieznany_token"] == "<UNMAPPED_TOKEN_99>"

def test_presidio_invoice_sanitizer_gemma_failure():
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url="http://mock-url/v1/chat/completions")
    with patch('requests.post', side_effect=Exception("Connection refused")):
        with pytest.raises(RuntimeError) as exc_info:
            sanitizer.sanitize("Test faktury NIP 7740001454")
        assert "Gemma SLM Recognizer (port 8080) jest niedostępna" in str(exc_info.value)

def test_deterministic_company_and_email_masking():
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url="http://mock-url/v1/chat/completions")
    text = (
        "SELLER: Tech-Distrib EU B.V. Email: billing@techdistrib-eu.com "
        "BUYER: INNOVATECH SP. Z O.O. NIP: PL5213894012"
    )
    with patch('requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "choices": [{"message": {"content": json.dumps([
                {"entity_type": "COMPANY_NAME", "text": "Tech-Distrib EU B.V."},
                {"entity_type": "COMPANY_NAME", "text": "INNOVATECH SP. Z O.O."}
            ])}}]
        }
        mock_post.return_value = mock_resp

        masked_text, mapping = sanitizer.sanitize(text)
        assert "<COMPANY_NAME_" in masked_text
        assert "<EMAIL_ADDRESS_" in masked_text
        assert "<PL_NIP_" in masked_text
        assert "billing@techdistrib-eu.com" in mapping.values()
        assert any("PL5213894012" in v for v in mapping.values())

def test_extract_ksef_xml_text():
    from pii_sanitizer import extract_ksef_xml_text
    base = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".cache", "test_sanitizer"))
    os.makedirs(base, exist_ok=True)
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
    <Faktura>
        <Podmiot1><NIP>7740001454</NIP><Nazwa>ORLEN S.A.</Nazwa></Podmiot1>
    </Faktura>"""
    xml_file = os.path.join(base, "ksef_faktura.xml")
    with open(xml_file, "w", encoding="utf-8") as f:
        f.write(xml_content)
    
    extracted = extract_ksef_xml_text(xml_file)
    assert "7740001454" in extracted
    assert "ORLEN S.A." in extracted


def test_ksef_id_anonymization():
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url="http://mock-url/v1/chat/completions")
    text = "Numer KSeF: 7822910384-20260720-H8I9J0K1L2M3-08"
    with patch('requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        mock_post.return_value = mock_resp

        masked_text, mapping = sanitizer.sanitize(text)
        assert "<KSEF_ID_" in masked_text
        assert "7822910384-20260720-H8I9J0K1L2M3-08" in mapping.values()

def test_polish_diacritics_company_recognition():
    """Weryfikuje deterministyczne rozpoznawanie nazw spółek z polskimi znakami (np. Ą, Ę, Ł, Ó, Ś, Ź, Ż)."""
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url="http://mock-url/v1/chat/completions")
    text = "Dostawca: ŻURAW-DŹWIG ŁÓDŹ Sp. z o.o. oraz Odbiorca: PRZEDSIĘBIORSTWO ŚLĄSK S.A."
    with patch('requests.post') as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"choices": [{"message": {"content": "[]"}}]}
        mock_post.return_value = mock_resp

        masked_text, mapping = sanitizer.sanitize(text)
        assert "<COMPANY_NAME_" in masked_text
        assert "ŻURAW-DŹWIG ŁÓDŹ Sp. z o.o." in mapping.values()
        assert "PRZEDSIĘBIORSTWO ŚLĄSK S.A." in mapping.values()




