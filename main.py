import os
import json
import uuid
import tempfile
import logging
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional, List, Union
from fastapi import FastAPI, UploadFile, File, status, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from celery import Celery

from accounting.db import repository

logger = logging.getLogger(__name__)

async def background_worker_loop():
    while True:
        try:
            await asyncio.to_thread(repository.dispatch_outbox_events)
            await asyncio.to_thread(repository.recover_stuck_attempts)
            await asyncio.to_thread(repository.sweep_stale_erp_exports)
        except Exception as e:
            logger.error(f"Błąd pętli w tle (dispatcher/sweeper): {e}")
        await asyncio.sleep(float(os.getenv("KARIK_DISPATCHER_INTERVAL_SEC", "2.0")))

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = None
    enable_dispatcher = os.getenv("KARIK_ENABLE_BACKGROUND_DISPATCHER")
    # Uruchamiamy w tle jeśli KARIK_ENABLE_BACKGROUND_DISPATCHER=1 lub domyślnie w produkcji (poza testami)
    should_run = (enable_dispatcher == "1") or (enable_dispatcher is None and os.getenv("KARIK_ENVIRONMENT") != "test")
    if should_run:
        task = asyncio.create_task(background_worker_loop())
    yield
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

app = FastAPI(title="KARIK - Hybrid Async Accounting & Legal RAG Engine", version="3.0", lifespan=lifespan)

# Obsługa serwowania plików statycznych (np. pełne teksty aktów prawnych z kotwicami HTML)
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Read Redis URL from environment, fallback to localhost for development/testing
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)

STORAGE_PATH = os.getenv("STORAGE_BASE_DIR", os.getenv("STORAGE_PATH", os.path.join(tempfile.gettempdir(), "accounting_storage")))
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

from fastapi import Header, Depends
from fastapi.responses import FileResponse
from accounting.auth import get_current_operator, AuthContext, create_access_token
from accounting.storage import stream_and_save_upload
from accounting.db import repository
from accounting.models import (
    DocumentStatus, AttemptStatus, SourceType, ExportStatus,
    ERPExportEvent, InvoiceSourceData, AISuggestions, ValidationReport
)
from validators.invoice_math import InvoiceMathValidator

class TaskResponse(BaseModel):
    document_id: str
    attempt_id: Optional[str] = None
    status: str
    sha256: Optional[str] = None

class CorrectionRequest(BaseModel):
    notes: Optional[str] = None
    corrected_invoice_data: Dict[str, Any]

class ApproveRequest(BaseModel):
    notes: Optional[str] = None

class ERPExportRequest(BaseModel):
    erp_system: str = "OPTIMA"
    simulate_network_timeout: bool = False

@app.post("/v1/invoice/process", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_invoice(
    file: UploadFile = File(...),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    # 1. Walidacja tożsamości i praw do firmy
    tenant_id = auth.check_tenant_access(x_tenant_id)
    operator_id = auth.operator_id

    # 2. Walidacja typu MIME
    if file.content_type not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {file.content_type}. Only JPEG, PNG, PDF, and KSeF XML files are allowed."
        )

    temp_doc_id = str(uuid.uuid4())

    # 3. Bezpieczny streaming upload ze zliczaniem bajtów w locie (max 25MB, usuwanie częściowe przy przekroczeniu)
    storage_path, sha256_hash, total_bytes = await stream_and_save_upload(file, tenant_id, temp_doc_id)

    # 4. Atomowa rejestracja w bazie: Document (RECEIVED) + ProcessingAttempt (PENDING) + OutboxEvent (PENDING)
    doc, attempt, event = repository.create_document_and_attempt_atomically(
        tenant_id=tenant_id,
        operator_id=operator_id,
        original_filename=file.filename or f"invoice_{temp_doc_id}",
        storage_path=storage_path,
        mime_type=file.content_type or "application/octet-stream",
        sha256_hash=sha256_hash,
        payload_meta={"total_bytes": total_bytes}
    )

    # 5. Bezpieczny Dispatch zadania do Celery (Transactional Outbox Dispatcher)
    if attempt and event:
        try:
            from tasks import process_invoice_task
            process_invoice_task.apply_async(args=[attempt.id, storage_path, tenant_id], task_id=attempt.task_id)
            repository.mark_outbox_event_sent(event.id)
        except Exception as dispatch_err:
            # W razie awarii brokera, rekord w DB pozostaje nienaruszony (PENDING) dla procesu odzyskiwania (Sweepera)
            repository.mark_outbox_event_failed(event.id, str(dispatch_err))

    return TaskResponse(
        document_id=doc.id,
        attempt_id=attempt.id if attempt else None,
        status=doc.status.value,
        sha256=doc.sha256_hash
    )

@app.get("/v1/invoice/status/{document_id}")
async def get_document_status(
    document_id: str,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    # Weryfikacja dostępu operatora do tenanta tego dokumentu
    auth.check_tenant_access(doc.tenant_id)

    versions = repository.get_versions(document_id)
    return {
        "document_id": doc.id,
        "tenant_id": doc.tenant_id,
        "status": doc.status.value,
        "approved_version_id": doc.approved_version_id,
        "filename": doc.original_filename,
        "sha256": doc.sha256_hash,
        "versions_count": len(versions),
        "versions": [
            {
                "version_id": v.id,
                "version_number": v.version_number,
                "source_type": v.source_type.value,
                "parent_version_id": v.parent_version_id,
                "is_valid": v.validation_report.is_valid,
                "calculation_method": v.validation_report.calculation_method_used,
                "reconciliation_errors": v.validation_report.reconciliation_errors,
                "warnings": v.validation_report.warnings,
                "invoice_summary": {
                    "number": v.immutable_source_data.invoice_number,
                    "net": str(v.immutable_source_data.total_net),
                    "vat": str(v.immutable_source_data.total_tax),
                    "gross": str(v.immutable_source_data.total_gross),
                } if v.immutable_source_data else None,
                "ai_suggestions": v.ai_suggestions.model_dump() if v.ai_suggestions else None
            } for v in versions
        ]
    }

@app.get("/v1/invoice/document/{document_id}")
async def get_document_original_file(
    document_id: str,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    # Twarda kontrola uprawnień
    auth.check_tenant_access(doc.tenant_id)

    if not os.path.exists(doc.storage_path):
        raise HTTPException(status_code=404, detail="Plik źródłowy nie istnieje w magazynie.")

    return FileResponse(
        doc.storage_path,
        media_type=doc.mime_type,
        filename=doc.original_filename
    )

@app.post("/v1/invoice/approve/{document_id}/{version_id}")
async def approve_document_version(
    document_id: str,
    version_id: str,
    req: ApproveRequest = ApproveRequest(),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    # Separacja obowiązków: wyłącznie audytor lub admin może zatwierdzić dokument!
    auth.require_role(doc.tenant_id, ["auditor", "admin"])

    success = repository.approve_document_version(
        document_id=document_id,
        version_id=version_id,
        operator_id=auth.operator_id,
        notes=req.notes
    )
    if not success:
        raise HTTPException(status_code=400, detail="Nie można zatwierdzić wskazanej wersji dokumentu.")

    return {
        "document_id": document_id,
        "approved_version_id": version_id,
        "status": DocumentStatus.APPROVED.value,
        "operator_id": auth.operator_id
    }

@app.post("/v1/invoice/reject/{document_id}")
async def reject_document(
    document_id: str,
    reason: str = "Odrzucono przez audytora",
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    auth.require_role(doc.tenant_id, ["auditor", "admin"])

    from accounting.db import ERPExportInFlightError
    try:
        success = repository.reject_document(
            document_id=document_id,
            operator_id=auth.operator_id,
            reason=reason
        )
    except ERPExportInFlightError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not success:
        raise HTTPException(status_code=400, detail="Nie można odrzucić dokumentu.")

    return {
        "document_id": document_id,
        "status": DocumentStatus.REJECTED.value,
        "operator_id": auth.operator_id
    }

@app.post("/v1/invoice/correct/{document_id}/{version_id}")
async def correct_document_version(
    document_id: str,
    version_id: str,
    req: CorrectionRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    """
    Korekta człowieka:
    Tworzy NOWĄ wersję ekstrakcji (v2) z oznaczeniem OPERATOR_CORRECTED.
    Nie nadpisuje w miejscu wersji bazowej v1!
    """
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    auth.require_role(doc.tenant_id, ["accountant", "admin"])

    try:
        corrected_source = InvoiceSourceData(**req.corrected_invoice_data)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Błędne dane faktury: {str(e)}")

    validation_report = InvoiceMathValidator.validate(corrected_source)

    new_version = repository.create_operator_correction(
        document_id=document_id,
        target_version_id=version_id,
        operator_id=auth.operator_id,
        corrected_source_data=corrected_source,
        validation_report=validation_report,
        notes=req.notes
    )
    if not new_version:
        raise HTTPException(status_code=400, detail="Nie udało się utworzyć wersji korygującej.")

    return {
        "document_id": document_id,
        "new_version_id": new_version.id,
        "version_number": new_version.version_number,
        "status": validation_report.status.value,
        "is_valid": validation_report.is_valid,
        "reconciliation_errors": validation_report.reconciliation_errors
    }

@app.post("/v1/invoice/erp-export/{document_id}/{version_id}")
async def export_to_erp(
    document_id: str,
    version_id: str,
    req: ERPExportRequest = ERPExportRequest(),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    auth.require_role(doc.tenant_id, ["accountant", "auditor", "admin"])

    # Obliczenie deterministycznego klucza idempotencji
    import hashlib
    idempotency_raw = f"{document_id}:{version_id}:{req.erp_system}"
    idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

    from accounting.db import (
        DocumentNotApprovedError,
        VersionNotApprovedError,
        ERPExportInFlightError,
        ERPExportUnknownError
    )
    from accounting.erp_connector import (
        erp_connector,
        ERPIntegrationUnavailableError,
        ERPConnectionTimeoutError,
        ERPBusinessRejectionError
    )

    # Atomowa rezerwacja w bazie z kontrolą zatwierdzenia i współbieżności
    try:
        export_id, exp_status, ref_id = repository.reserve_erp_export_atomic(
            document_id=document_id,
            version_id=version_id,
            idempotency_key=idempotency_key,
            erp_system=req.erp_system
        )
    except DocumentNotApprovedError as dne:
        raise HTTPException(status_code=409, detail=str(dne))
    except VersionNotApprovedError as vne:
        raise HTTPException(status_code=400, detail=str(vne))
    except ERPExportInFlightError as ife:
        raise HTTPException(status_code=409, detail=str(ife))
    except ERPExportUnknownError as uke:
        raise HTTPException(status_code=409, detail=str(uke))
    except KeyError as ke:
        raise HTTPException(status_code=404, detail=str(ke))

    # Jeśli dokument został już wcześniej pomyślnie wyeksportowany (idempotencja)
    if exp_status == ExportStatus.EXPORT_CONFIRMED:
        logger.info(f"Idempotencja ERP: Dokument {document_id} został już pomyślnie wyeksportowany.")
        return {
            "status": ExportStatus.EXPORT_CONFIRMED.value,
            "idempotency_key": idempotency_key,
            "erp_reference_id": ref_id,
            "message": "Dokument został już wcześniej pomyślnie wyeksportowany (idempotencja)."
        }

    ver = repository.get_version(document_id, version_id)
    if not ver or not ver.immutable_source_data:
        repository.fail_erp_export(export_id, "Brak danych źródłowych faktury do eksportu.")
        raise HTTPException(status_code=400, detail="Brak danych źródłowych faktury do eksportu.")

    # Próba faktycznej transmisji przez konektor ERP
    try:
        trans_res = erp_connector.transmit_invoice(
            invoice=ver.immutable_source_data,
            idempotency_key=idempotency_key,
            simulate_network_timeout=req.simulate_network_timeout
        )
        repository.record_erp_export_result(export_id, trans_res.status, trans_res.erp_reference_id)
        return {
            "status": trans_res.status.value,
            "idempotency_key": idempotency_key,
            "erp_reference_id": trans_res.erp_reference_id
        }
    except ERPConnectionTimeoutError as timeout_err:
        logger.error(f"Utrata łączności podczas eksportu ERP (idempotency_key={idempotency_key}): {timeout_err}")
        repository.set_erp_export_unknown(export_id, str(timeout_err))
        return {
            "status": ExportStatus.EXPORT_UNKNOWN.value,
            "idempotency_key": idempotency_key,
            "message": "Wynik nieznany - połączenie z ERP zerwane w trakcie transmisji. Wymagane uzgodnienie stanu."
        }
    except ERPBusinessRejectionError as rej_err:
        logger.error(f"System ERP odrzucił dokument: {rej_err}")
        repository.fail_erp_export(export_id, str(rej_err))
        raise HTTPException(status_code=422, detail=f"Odrzucenie przez ERP: {rej_err}")
    except ERPIntegrationUnavailableError as unav_err:
        logger.error(f"Eksport ERP niemożliwy: {unav_err}")
        repository.fail_erp_export(export_id, str(unav_err))
        raise HTTPException(status_code=503, detail=str(unav_err))


class ReconcileRequest(BaseModel):
    decision: str  # "CONFIRM" lub "REJECT"
    erp_reference_id: Optional[str] = None
    notes: str


@app.post("/v1/invoice/erp-reconcile/{document_id}/{version_id}")
async def reconcile_erp_status(
    document_id: str,
    version_id: str,
    req: ReconcileRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    """
    Ręczne uzgodnienie stanu po awarii sieci (EXPORT_UNKNOWN).
    Wymaga uprawnień audytora lub administratora.
    """
    doc = repository.get_document(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Dokument nie został znaleziony.")

    auth.require_role(doc.tenant_id, ["auditor", "admin"])

    import hashlib
    idempotency_raw = f"{document_id}:{version_id}:comarch_optima"
    idempotency_key = hashlib.sha256(idempotency_raw.encode("utf-8")).hexdigest()

    existing_export = repository.get_latest_erp_export(idempotency_key)
    if not existing_export or existing_export.status not in (ExportStatus.EXPORT_UNKNOWN, ExportStatus.EXPORT_TRANSMITTED):
        raise HTTPException(status_code=400, detail="Brak eksportu w stanie EXPORT_UNKNOWN lub EXPORT_TRANSMITTED wymagającego uzgodnienia.")

    new_status = ExportStatus.EXPORT_CONFIRMED if req.decision.upper() == "CONFIRM" else ExportStatus.EXPORT_GENERATED
    reconciled_event = ERPExportEvent(
        document_id=document_id,
        extraction_version_id=version_id,
        erp_system=existing_export.erp_system,
        idempotency_key=idempotency_key,
        status=new_status,
        erp_reference_id=req.erp_reference_id or existing_export.erp_reference_id,
        error_message=f"Uzgodnienie audytora ({auth.operator_id}): {req.notes}"
    )
    repository.record_erp_export_step(reconciled_event)

    return {
        "document_id": document_id,
        "status": new_status.value,
        "erp_reference_id": reconciled_event.erp_reference_id,
        "operator_id": auth.operator_id
    }


class ConsultRequest(BaseModel):
    query: str

@app.post("/v1/consult/ask")
async def consult_ask(
    request: ConsultRequest,
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    """
    Synchroniczne zapytanie doradcze dla księgowego.
    Wykonuje pełny potok RAG z ochroną Egress Guard.
    """
    auth.check_tenant_access(x_tenant_id)
    if not request.query or not request.query.strip():
        raise HTTPException(status_code=400, detail="Treść zapytania nie może być pusta.")
    try:
        from rag.pipeline import run_rag_pipeline
        result = run_rag_pipeline(user_query=request.query)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Błąd przetwarzania potoku RAG: {str(e)}")

@app.post("/v1/consult/process-email", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def consult_process_email(
    file: UploadFile = File(...),
    x_tenant_id: Optional[str] = Header(None, alias="X-Tenant-ID"),
    auth: AuthContext = Depends(get_current_operator)
):
    """
    Asynchroniczny upload pliku e-mail / PDF / TXT do przetworzenia w tle przez Celery worker.
    """
    auth.check_tenant_access(x_tenant_id)
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
async def test_anonymize(
    file: UploadFile = File(...),
    auth: AuthContext = Depends(get_current_operator)
):
    """Endpoint pomocniczy do testowania anonimizacji z twardą ochroną PII."""
    if file.content_type not in ALLOWED_MIME_TYPES and file.content_type != "text/plain":
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {file.content_type}")
        
    ext = os.path.splitext(file.filename)[1].lower()
    temp_path = os.path.join(STORAGE_PATH, f"test_{uuid.uuid4()}{ext}")
    
    try:
        with open(temp_path, "wb") as buffer:
            while chunk := await file.read(1024 * 1024):
                buffer.write(chunk)
                
        from pii_sanitizer import PresidioInvoiceSanitizer, extract_pdf_text, extract_ksef_xml_text
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
        
        # BEZPIECZEŃSTWO AUDYTOWE: Nie ujawniamy słownika tokenów ani deanonimizowanych wartości
        return {
            "original_text_length": len(raw_text),
            "anonymized_text": anonymized_text,
            "tokens_found_count": len(mapping_dict),
            "operator_id": auth.operator_id
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)


class ManualAnonymizeRequest(BaseModel):
    raw_text: str

class ManualDetokenizeRequest(BaseModel):
    session_id: str
    cloud_json_text: str

_manual_sessions: Dict[str, Dict[str, str]] = {}


@app.post("/v1/manual/anonymize")
async def manual_anonymize(
    req: ManualAnonymizeRequest,
    auth: AuthContext = Depends(get_current_operator)
):
    """Krok 1 w trybie ręcznym: Anonimizuje tekst i przechowuje tokeny po stronie serwera."""
    try:
        from pii_sanitizer import PresidioInvoiceSanitizer
        local_ai_url = os.getenv("LOCAL_AI_URL", "http://localhost:8080/v1")
        if not local_ai_url.endswith("/chat/completions"):
            local_ai_url = f"{local_ai_url.rstrip('/')}/chat/completions"

        sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=local_ai_url)
        anonymized_text, mapping_dict = sanitizer.sanitize(req.raw_text)

        session_id = str(uuid.uuid4())
        _manual_sessions[session_id] = mapping_dict

        # BEZPIECZEŃSTWO: Słownik mapowania nie opuszcza serwera!
        return {
            "session_id": session_id,
            "anonymized_text": anonymized_text,
            "tokens_found_count": len(mapping_dict)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/v1/manual/detokenize")
async def manual_detokenize(
    req: ManualDetokenizeRequest,
    auth: AuthContext = Depends(get_current_operator)
):
    """Krok 2 w trybie ręcznym: Odczytuje odpowiedź z Chatu i deanonimizuje ją na podstawie session_id."""
    try:
        import re
        from pii_sanitizer import tolerant_detokenize, detokenize_text

        raw_str = req.cloud_json_text.strip()
        if not raw_str:
            raise HTTPException(status_code=400, detail="Brak tekstu odpowiedzi z chatu.")

        mapping_dict = _manual_sessions.get(req.session_id)
        if not mapping_dict:
            raise HTTPException(status_code=404, detail="Sesja anonimizacji wygasła lub nie istnieje.")

        # 1. Jeśli użytkownik wkleił JSON (lub kod w bloku ```json ... ```), dekodujemy i deanonimizujemy jako JSON
        json_match = re.search(r'\{.*\}', raw_str, re.DOTALL)
        if json_match:
            try:
                parsed_json = json.loads(json_match.group(0))
                detokenized_dict, has_leftovers = tolerant_detokenize(parsed_json, mapping_dict)
                return {
                    "mode": "json",
                    "detokenized_result": detokenized_dict,
                    "has_leftover_tokens": has_leftovers
                }
            except json.JSONDecodeError:
                pass

        # 2. Jeśli tekst z chatu nie jest obiektem JSON, deanonimizujemy surowy tekst/markdown!
        detokenized_text, has_leftovers = detokenize_text(raw_str, mapping_dict)
        return {
            "mode": "text",
            "detokenized_result": detokenized_text,
            "has_leftover_tokens": has_leftovers
        }

    except HTTPException as h_err:
        raise h_err
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

