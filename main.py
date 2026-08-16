import os
import json
import uuid
import tempfile
from typing import Dict, Any
from fastapi import FastAPI, UploadFile, File, status, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from celery import Celery

app = FastAPI(title="KARIK - Hybrid Async Accounting & Legal RAG Engine", version="3.0")

# Obsługa serwowania plików statycznych (np. pełne teksty aktów prawnych z kotwicami HTML)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Read Redis URL from environment, fallback to localhost for development/testing
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)

STORAGE_PATH = os.getenv("STORAGE_PATH", os.path.join(tempfile.gettempdir(), "accounting_storage"))
os.makedirs(STORAGE_PATH, exist_ok=True)

class TaskResponse(BaseModel):
    task_id: str
    status: str

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "application/pdf",
    "application/xml",
    "text/xml"
}

@app.get("/", response_class=HTMLResponse)
@app.get("/ui", response_class=HTMLResponse)
async def serve_ui():
    """Serwuje interaktywny panel testowy RODO dla użytkownika."""
    ui_path = os.path.join(os.path.dirname(__file__), "ui.html")
    if os.path.exists(ui_path):
        with open(ui_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>Panel UI nie został znaleziony</h1>"

@app.post("/v1/invoice/process", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_invoice(file: UploadFile = File(...)):
    # Validate MIME type
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {file.content_type}. Only JPEG, PNG, PDF, and KSeF XML files are allowed."
        )

    ext = os.path.splitext(file.filename)[1]
    # If the filename does not have an extension, try to map from content-type
    if not ext:
        if file.content_type == "image/jpeg":
            ext = ".jpg"
        elif file.content_type == "image/png":
            ext = ".png"
        elif file.content_type == "application/pdf":
            ext = ".pdf"
        elif file.content_type in ("application/xml", "text/xml"):
            ext = ".xml"

    task_id = str(uuid.uuid4())
    file_name = f"{task_id}{ext}"
    target_destination = os.path.join(STORAGE_PATH, file_name)
    
    # Write file stream asynchronously
    try:
        with open(target_destination, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):  # Read in 1MB chunks
                buffer.write(chunk)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to write file to storage: {str(e)}"
        )
        
    # Dispatch asynchronous task to Celery
    try:
        from tasks import process_invoice_task
        process_invoice_task.apply_async(args=[task_id, target_destination], task_id=task_id)
    except Exception as e:
        # Clean up file on task dispatch failure
        if os.path.exists(target_destination):
            os.remove(target_destination)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to queue celery task: {str(e)}"
        )
    
    return TaskResponse(task_id=task_id, status="pending")

@app.get("/v1/invoice/status/{task_id}")
async def get_task_status(task_id: str):
    try:
        res = celery_app.AsyncResult(task_id)
        if res.state == "PENDING":
            return {"task_id": task_id, "status": "pending"}
        elif res.state == "SUCCESS":
            return {"task_id": task_id, "status": "completed", "data": res.result}
        elif res.state == "FAILURE":
            return {"task_id": task_id, "status": "failed", "error": str(res.info)}
        return {"task_id": task_id, "status": res.state.lower()}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Error checking task status: {str(e)}"
        )

class ConsultRequest(BaseModel):
    query: str

@app.post("/v1/consult/ask")
async def consult_ask(request: ConsultRequest):
    """
    Synchroniczne zapytanie doradcze dla księgowego.
    Wykonuje pełny potok RAG: RODO Anonymization -> pgvector HNSW -> Polish Reranker CUDA -> Gemini 3.5 -> Detokenizacja.
    """
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Treść zapytania nie może być pusta.")
    try:
        from rag.pipeline import run_rag_pipeline
        result = run_rag_pipeline(user_query=request.query)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd przetwarzania potoku RAG: {str(e)}")

@app.post("/v1/consult/process-email", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def consult_process_email(file: UploadFile = File(...)):
    """
    Asynchroniczny upload pliku e-mail / PDF / TXT do przetworzenia w tle przez Celery worker.
    """
    task_id = str(uuid.uuid4())
    ext = os.path.splitext(file.filename)[1].lower() or ".txt"
    file_name = f"consult_{task_id}{ext}"
    target_destination = os.path.join(STORAGE_PATH, file_name)

    try:
        with open(target_destination, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd zapisu pliku: {str(e)}")

    try:
        from tasks import process_consultation_task
        process_consultation_task.apply_async(args=[task_id, target_destination], task_id=task_id)
    except Exception as e:
        if os.path.exists(target_destination):
            os.remove(target_destination)
        raise HTTPException(status_code=500, detail=f"Błąd kolejkowania zadania Celery: {str(e)}")

    return TaskResponse(task_id=task_id, status="pending")



@app.post("/v1/test/anonymize")
async def test_anonymize(file: UploadFile = File(...)):
    """Endpoint pomocniczy do bezpośredniego testowania anonimizacji i detokenizacji (BEZ wywołania Gemini)."""
    if file.content_type not in ALLOWED_MIME_TYPES and file.content_type != "text/plain":
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")
        
    ext = os.path.splitext(file.filename)[1].lower()
    temp_path = os.path.join(STORAGE_PATH, f"test_{uuid.uuid4()}{ext}")
    
    try:
        with open(temp_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
                
        from pii_sanitizer import PresidioInvoiceSanitizer, extract_pdf_text, extract_ksef_xml_text, tolerant_detokenize
        local_ai_url = os.getenv("LOCAL_AI_URL", "http://localhost:8080/v1")
        if not local_ai_url.endswith("/chat/completions"):
            local_ai_url = f"{local_ai_url.rstrip('/')}/chat/completions"

        sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=local_ai_url)

        raw_text = ""
        if ext == ".pdf":
            raw_text = extract_pdf_text(temp_path)
        elif ext == ".xml":
            raw_text = extract_ksef_xml_text(temp_path)
        elif ext in (".txt", ".plain") or file.content_type == "text/plain":
            with open(temp_path, "r", encoding="utf-8", errors="ignore") as f:
                raw_text = f.read()

        if not raw_text:
            raw_text = "Faktura od firmy z NIP: 7740001454, IBAN: PL61109010140000071219812874"

        anonymized_text, mapping_dict = sanitizer.sanitize(raw_text)
        
        mock_cloud_json = {
            "dane_zanonimizowane": {token: token for token in mapping_dict.keys()}
        }
        detokenized, has_leftovers = tolerant_detokenize(mock_cloud_json, mapping_dict)

        return {
            "original_text": raw_text,
            "anonymized_text": anonymized_text,
            "mapping_dictionary": mapping_dict,
            "detokenization_test": detokenized,
            "has_leftover_tokens": has_leftovers
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

class ManualAnonymizeRequest(BaseModel):
    raw_text: str

class ManualDetokenizeRequest(BaseModel):
    cloud_json_text: str
    mapping_dictionary: Dict[str, str]

PROMPT_MANUAL_LLM = """Jesteś zaawansowanym systemem eksperckim ds. polskiej rachunkowości. Przeanalizuj poniższy, zanonimizowany tekst dokumentu i przygotuj dekretację księgową kosztu. Kategoryzacja musi opierać się o kontekst tokenów branżowych. Zachowaj wszystkie tokeny w niezmienionej formie.

Wygeneruj strukturę JSON:
{
  "data_wystawienia": "YYYY-MM-DD",
  "sprzedawca_token": "<COMPANY_NAME_1>",
  "sprzedawca_nip_token": "<PL_NIP_1>",
  "pozycje_faktury": [
    {
      "opis": "Opis usługi / towaru",
      "netto": 100.00,
      "vat_stawka": "23%",
      "vat_kwota": 23.00,
      "brutto": 123.00,
      "konto_wn": "401-01",
      "konto_ma": "210"
    }
  ],
  "podsumowanie": {
    "suma_netto": 100.00,
    "suma_vat": 23.00,
    "suma_brutto": 123.00
  },
  "sugerowany_kod_gtu": "GTU_12",
  "uzasadnienie_ksiegowe": "Uzasadnienie kwalifikacji kosztowej."
}

Tekst dokumentu do dekretacji:
"""

@app.post("/v1/manual/anonymize")
async def manual_anonymize(req: ManualAnonymizeRequest):
    """Krok 1 w trybie ręcznym: Anonimizuje tekst wprowadzony przez księgowego i zwraca sam zanonimizowany tekst faktury."""
    try:
        from pii_sanitizer import PresidioInvoiceSanitizer
        local_ai_url = os.getenv("LOCAL_AI_URL", "http://localhost:8080/v1")
        if not local_ai_url.endswith("/chat/completions"):
            local_ai_url = f"{local_ai_url.rstrip('/')}/chat/completions"

        sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=local_ai_url)
        anonymized_text, mapping_dict = sanitizer.sanitize(req.raw_text)

        return {
            "anonymized_text": anonymized_text,
            "mapping_dictionary": mapping_dict
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/manual/detokenize")
async def manual_detokenize(req: ManualDetokenizeRequest):
    """Krok 2 w trybie ręcznym: Odczytuje wklejoną odpowiedź z Chatu online (JSON lub zwykły tekst/markdown) i deanonimizuje ją."""
    try:
        import re
        from pii_sanitizer import tolerant_detokenize, detokenize_text

        raw_str = req.cloud_json_text.strip()
        if not raw_str:
            raise HTTPException(status_code=400, detail="Brak tekstu odpowiedzi z chatu.")

        # 1. Jeśli użytkownik wkleił JSON (lub kod w bloku ```json ... ```), dekodujemy i deanonimizujemy jako JSON
        json_match = re.search(r'\{.*\}', raw_str, re.DOTALL)
        if json_match:
            try:
                parsed_json = json.loads(json_match.group(0))
                detokenized_dict, has_leftovers = tolerant_detokenize(parsed_json, req.mapping_dictionary)
                return {
                    "mode": "json",
                    "detokenized_result": detokenized_dict,
                    "has_leftover_tokens": has_leftovers
                }
            except json.JSONDecodeError:
                pass

        # 2. Jeśli tekst z chatu nie jest obiektem JSON, deanonimizujemy surowy tekst/markdown!
        detokenized_text, has_leftovers = detokenize_text(raw_str, req.mapping_dictionary)
        return {
            "mode": "text",
            "detokenized_result": detokenized_text,
            "has_leftover_tokens": has_leftovers
        }

    except HTTPException as h_err:
        raise h_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

