import os
import io
import pytest
from fastapi.testclient import TestClient
import main
from accounting.auth import create_access_token

client = TestClient(main.app)

@pytest.fixture
def auth_headers():
    token = create_access_token(
        operator_id="test_operator",
        allowed_tenants=["test_tenant"]
    )
    return {
        "Authorization": f"Bearer {token}",
        "X-Tenant-ID": "test_tenant"
    }

def test_invoice_process_unauthorized():
    # Brak tokena autoryzacji zwraca 401
    files = {"file": ("invoice.pdf", b"%PDF-1.4 dummy", "application/pdf")}
    response = client.post("/v1/invoice/process", files=files)
    assert response.status_code == 401

def test_invoice_process_invalid_mime(auth_headers):
    # Nieobsługiwany typ MIME zwraca 400
    files = {"file": ("invoice.txt", b"plain text content", "text/plain")}
    response = client.post("/v1/invoice/process", files=files, headers=auth_headers)
    assert response.status_code == 400
    assert "Unsupported file type" in response.json()["detail"]

def test_invoice_process_success_and_lifecycle(auth_headers):
    # Poprawny upload pliku PDF
    file_content = b"%PDF-1.4 mock pdf data"
    files = {"file": ("invoice.pdf", file_content, "application/pdf")}

    response = client.post("/v1/invoice/process", files=files, headers=auth_headers)
    assert response.status_code == 202
    data = response.json()
    assert "document_id" in data
    assert data["status"] == "RECEIVED"
    doc_id = data["document_id"]

    # Pobranie statusu dokumentu przez autoryzowanego operatora
    status_response = client.get(f"/v1/invoice/status/{doc_id}", headers=auth_headers)
    assert status_response.status_code == 200
    status_data = status_response.json()
    assert status_data["document_id"] == doc_id
    assert status_data["tenant_id"] == "test_tenant"
    assert status_data["status"] == "RECEIVED"

    # Pobranie oryginalnego pliku dokumentu
    doc_file_res = client.get(f"/v1/invoice/document/{doc_id}", headers=auth_headers)
    assert doc_file_res.status_code == 200
    assert doc_file_res.content == file_content

def test_invoice_process_forbidden_for_other_tenant(auth_headers):
    file_content = b"%PDF-1.4 mock pdf data"
    files = {"file": ("invoice.pdf", file_content, "application/pdf")}

    # Próba wysłania dokumentu dla firmy tenant_alien (brak uprawnień)
    headers_alien = auth_headers.copy()
    headers_alien["X-Tenant-ID"] = "tenant_alien"

    response = client.post("/v1/invoice/process", files=files, headers=headers_alien)
    assert response.status_code == 403
    assert "Brak uprawnień" in response.json()["detail"]
