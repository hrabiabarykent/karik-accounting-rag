import os
import json
import base64
import time
import logging
import yaml
from celery import Celery
import redis
import openai

# Configure Gemini SDK (support new google.genai with safe fallback)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

try:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        import google.generativeai as gemini
        if GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
            gemini.configure(api_key=GEMINI_API_KEY)
except Exception:
    gemini = None

try:
    from google import genai
    from google.genai import types
    HAS_NEW_GENAI = True
except ImportError:
    genai = None
    types = None
    HAS_NEW_GENAI = False


from pii_sanitizer import PresidioInvoiceSanitizer, extract_pdf_text, extract_ksef_xml_text, tolerant_detokenize

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Safely import LangFuse observe decorator with a no-op fallback
try:
    from langfuse.decorators import observe
except Exception:
    def observe(*args, **kwargs):
        def decorator(func):
            return func
        return decorator if (args and callable(args[0])) else decorator

# Config
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
celery_app = Celery("tasks", broker=REDIS_URL, backend=REDIS_URL)

try:
    redis_client = redis.Redis.from_url(REDIS_URL)
except Exception as e:
    logger.error(f"Failed to connect to Redis: {str(e)}")
    redis_client = None

# Configure API clients
LOCAL_AI_URL = os.getenv("LOCAL_AI_URL", "http://localhost:8080/v1")
LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL", "gemma4-e4b")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# Initialize OpenAI SDK client for llama-server with strict timeout
local_ai_client = openai.OpenAI(base_url=LOCAL_AI_URL, api_key="not-needed", timeout=2.0, max_retries=0)

if GEMINI_API_KEY:
    if gemini is not None:
        gemini.configure(api_key=GEMINI_API_KEY)
else:
    logger.warning("GEMINI_API_KEY environment variable is not set!")

# Dynamic Prompt Loader from YAML
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

def load_prompt_yaml(filename: str, fallback_text: str) -> str:
    yaml_path = os.path.join(PROMPTS_DIR, filename)
    if os.path.exists(yaml_path):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                if isinstance(data, dict) and "template" in data:
                    return data["template"].strip()
        except Exception as e:
            logger.error(f"Failed to load prompt from {filename}: {e}")
    return fallback_text.strip()

_DEFAULT_PROMPT_A = """Jesteś lokalnym, bezpiecznym strażnikiem prywatności RODO w module księgowym..."""
_DEFAULT_PROMPT_B = """Jesteś zaawansowanym systemem eksperckim ds. polskiej rachunkowości..."""
_DEFAULT_PROMPT_D = """Jesteś Głównym Audytorem i Kontrolerem Jakości w zautomatyzowanym biurze rachunkowym..."""

PROMPT_STEP_A = load_prompt_yaml("prompt_step_a.yaml", _DEFAULT_PROMPT_A)
PROMPT_STEP_B = load_prompt_yaml("prompt_step_b.yaml", _DEFAULT_PROMPT_B)
PROMPT_STEP_D = load_prompt_yaml("prompt_step_d.yaml", _DEFAULT_PROMPT_D)

def encode_image_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as img_file:
        return base64.b64encode(img_file.read()).decode("utf-8")

from datetime import datetime, date
from decimal import Decimal
from ksef_parser import SafeKsefXmlParser

from validators.invoice_math import InvoiceMathValidator
from security.egress_guard import ScopedEgressGuard, seal_sanitized_envelope, SecurityPrivacyViolationError
from accounting.db import repository, active_heartbeat
from accounting.models import (
    DocumentStatus,
    AttemptStatus,
    SourceType,
    InvoiceSourceData,
    InvoiceParty,
    InvoiceLineItem,
    TaxGroupSummary,
    TaxRateGroup,
    AISuggestions,
    ValidationReport,
)

def refresh_mask_ttl(task_id: str, ttl_seconds: int = 600):
    """Odświeżenie TTL słownika RODO w ulotnej pamięci RAM workerów (bez persystencji na dysk)."""
    if redis_client:
        try:
            redis_client.expire(f"mask:{task_id}", ttl_seconds)
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to refresh Redis TTL: {e}")

@celery_app.task(name="tasks.process_invoice_task", bind=True, max_retries=3, default_retry_countdown=60)
@observe(name="process_invoice_task")
def process_invoice_task(self, attempt_id: str, file_path: str, tenant_id: str = "default_tenant"):
    """
    Bezpieczny potok przetwarzania faktury:
    - KROK 0: Atomowe przejęcie próby z leasingiem (fencing token) i okresowym heartbeatem
    - KROK 1: Deterministyczny KSeF XML (FA2) lub OCR dla skanów
    - KROK 2: Ochrona kwot źródłowych przed manipulacją AI
    - KROK 3: Walidacja finansowa Decimal (Art. 106e ustawy o VAT)
    - KROK 4: Wersjonowanie w bazie (brak statusu VERIFIED, zamiast tego VALIDATED / REQUIRES_REVIEW)
    - KROK 5: Plik źródłowy NIE jest kasowany z magazynu.
    """
    logger.info(f"Rozpoczęcie potoku dla próby {attempt_id} (plik: {file_path})")

    # KROK 0: Atomowe przejęcie próby (Fencing)
    lease_token = repository.claim_attempt_atomically(attempt_id, worker_id=f"worker-{os.getpid()}")
    if not lease_token:
        logger.info(f"Próba {attempt_id} została już podjęta lub zakończona. Przerywanie wykonania (idempotent no-op).")
        return {"status": "SKIPPED_ALREADY_CLAIMED"}

    # Aktywacja okresowego sygnału życia (heartbeat) co 5s podczas całego potoku
    hb_ctx = active_heartbeat(attempt_id, lease_token, interval_sec=5.0)
    hb_ctx.__enter__()
    try:
        result = _execute_invoice_pipeline(attempt_id, file_path, lease_token, tenant_id)
        if result.get("status") in ("FAILED", "REQUIRES_REVIEW"):
            repository.fail_attempt(attempt_id, lease_token, result.get("error") or result.get("reason") or "PROCESSING_FAILED")
        return result
    except Exception as exc:
        retryable = isinstance(exc, (TimeoutError, ConnectionError))
        repository.fail_attempt(attempt_id, lease_token, type(exc).__name__, retryable=retryable)
        logger.exception("Invoice attempt failed: %s", attempt_id)
        return {"status": "FAILED", "error": type(exc).__name__, "retryable": retryable}
    finally:
        hb_ctx.__exit__(None, None, None)


def _execute_invoice_pipeline(attempt_id: str, file_path: str, lease_token: str, tenant_id: str = "default_tenant"):
    if not os.path.exists(file_path):
        logger.error(f"Błąd: plik {file_path} nie istnieje w magazynie.")
        return {"status": "FAILED", "error": "FILE_NOT_FOUND"}

    ext = os.path.splitext(file_path)[1].lower()

    # ------------------------------------------------------------
    # ŚCIEŻKA A: DETERMINISTYCZNY PARSER KSEF XML (100% BEZ HALUCYNACJI AI)
    # ------------------------------------------------------------
    if ext == ".xml":
        logger.info(f"Próba {attempt_id}: Wykryto dokument XML. Uruchamianie bezpiecznego parsera KSeF...")
        parse_result = SafeKsefXmlParser.parse_file(file_path)

        if not parse_result.is_supported_subset:
            source_data = parse_result.invoice_data
            report = ValidationReport(
                is_valid=False,
                status=DocumentStatus.REQUIRES_REVIEW,
                reconciliation_errors=[f"Nieobsługiwany schemat/waluta/rodzaj KSeF: {parse_result.unsupported_reason}"]
            )
            repository.save_extraction_version(
                attempt_id=attempt_id,
                lease_token=lease_token,
                source_data=source_data,
                ai_suggestions=AISuggestions(accounting_notes="Wymaga ręcznej obsługi księgowej."),
                validation_report=report,
                source_type=SourceType.KSEF_DETERMINISTIC
            )
            _cleanup_transient_memory(attempt_id)
            return {"status": DocumentStatus.REQUIRES_REVIEW.value, "reason": parse_result.unsupported_reason}

        # Pełna zgodność ze standardem FA(2) Standard VAT
        source_data = parse_result.invoice_data

        # Deterministyczny audyt matematyczny Decimal (Art. 106e)
        validation_report = InvoiceMathValidator.validate(source_data)

        # Sugestia kont księgowych AI (wyłącznie sugestie, bez prawa do modyfikacji kwot!)
        ai_suggestions = AISuggestions(
            suggested_debit_account="401-01",
            suggested_credit_account="210-01",
            suggested_gtu_codes=[],
            accounting_notes="Prawidłowa faktura podstawowa KSeF FA(2). Kwoty pobrane w 100% ze źródła XML."
        )

        version = repository.save_extraction_version(
            attempt_id=attempt_id,
            lease_token=lease_token,
            source_data=source_data,
            ai_suggestions=ai_suggestions,
            validation_report=validation_report,
            source_type=SourceType.KSEF_DETERMINISTIC
        )

        _cleanup_transient_memory(attempt_id)
        return {
            "status": validation_report.status.value,
            "version_id": version.id if version else None,
            "is_valid": validation_report.is_valid,
            "reconciliation_errors": validation_report.reconciliation_errors
        }

    # ------------------------------------------------------------
    # ŚCIEŻKA B: DOKUMENTY NIELUKTURALNE (PDF / SKANY / OBRAZY)
    # ------------------------------------------------------------
    logger.info(f"Próba {attempt_id}: Dokument nielukturalny ({ext}). Uruchamianie ścieżki OCR z Fail-Closed Egress Guard...")
    guard = ScopedEgressGuard(attempt_id=attempt_id, tenant_id=tenant_id)
    sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=f"{LOCAL_AI_URL}/chat/completions")

    raw_text = ""
    if ext == ".pdf":
        raw_text = extract_pdf_text(file_path)

    privacy_cleared = False
    masked_text = ""
    mapping_dictionary = {}

    try:
        if raw_text:
            masked_text, mapping_dictionary = sanitizer.sanitize(raw_text)
            privacy_cleared = True
        else:
            base64_image = encode_image_to_base64(file_path)
            mime_type = "application/pdf" if ext == ".pdf" else "image/jpeg"
            image_url_data = f"data:{mime_type};base64,{base64_image}"

            local_response = local_ai_client.chat.completions.create(
                model=LOCAL_AI_MODEL,
                messages=[
                    {"role": "user", "content": [
                        {"type": "text", "text": PROMPT_STEP_A},
                        {"type": "image_url", "image_url": {"url": image_url_data}}
                    ]}
                ],
                response_format={"type": "json_object"}
            )
            masking_result = json.loads(local_response.choices[0].message.content)
            slm_masked_text = masking_result.get("masked_text", "")
            slm_mapping = masking_result.get("mapping_dictionary", {})
            presidio_masked_text, presidio_map = sanitizer.sanitize(slm_masked_text)
            masked_text = presidio_masked_text
            mapping_dictionary = {**slm_mapping, **presidio_map}
            privacy_cleared = True
    except Exception as san_err:
        logger.error(f"Próba {attempt_id}: Awaria modułu sanityzacji: {san_err}. Blokada wyjścia do chmury (Fail-Closed).")
        privacy_cleared = False

    # Pieczętowanie koperty dla Egress Guarda
    envelope = seal_sanitized_envelope(
        payload_text=masked_text,
        attempt_id=attempt_id,
        tenant_id=tenant_id,
        privacy_cleared=privacy_cleared,
        has_unresolved_tokens=False
    )

    if not privacy_cleared:
        # Twarda blokada - brak fałszywych faktur i kwot zerowych!
        report = ValidationReport(
            is_valid=False,
            status=DocumentStatus.REQUIRES_REVIEW,
            reconciliation_errors=["Błąd sanityzacji PII. Wywołanie chmury zostało bezwzględnie zablokowane (Fail-Closed)."]
        )
        repository.save_extraction_version(
            attempt_id=attempt_id,
            lease_token=lease_token,
            source_data=None,
            ai_suggestions=AISuggestions(accounting_notes="Awaria lokalnego modułu prywatności."),
            validation_report=report,
            source_type=SourceType.OCR_HYPOTHESIS,
            extraction_status="FAILED",
            review_reason="PRIVACY_ERROR"
        )
        _cleanup_transient_memory(attempt_id)
        return {"status": DocumentStatus.REQUIRES_REVIEW.value, "reason": "PRIVACY_ERROR"}

    # Zapis słownika tokenów w ulotnej pamięci RAM (TTL=600s, bez persystencji na dysk)
    if redis_client:
        try:
            redis_client.setex(f"mask:{attempt_id}", 600, json.dumps(mapping_dictionary))
        except Exception:
            pass

    # Wywołanie Gemini wyłącznie przez ScopedEgressGuard z zamkniętym kontraktem
    try:
        raw_cloud_json = guard.execute_completion(
            envelope=envelope,
            system_instruction=PROMPT_STEP_B,
            model_name=GEMINI_MODEL_NAME,
            response_mime_type="application/json"
        )
        cloud_json = json.loads(raw_cloud_json)
    except SecurityPrivacyViolationError as sec_err:
        logger.error(f"Egress Guard zablokował wywołanie chmury: {sec_err}")
        return {"status": DocumentStatus.REQUIRES_REVIEW.value, "reason": "EGRESS_BLOCKED"}
    except Exception as exc:
        logger.warning(f"Błąd wywołania Gemini: {exc}")
        cloud_json = {}

    repository.heartbeat_attempt(attempt_id, lease_token)
    candidate_json, has_leftovers = tolerant_detokenize(cloud_json, mapping_dictionary)

    # Budujemy obiekt InvoiceSourceData jako OCR_HYPOTHESIS bez podstawiania fikcyjnych kwot i danych
    ocr_source = None
    extraction_status = "FAILED"
    review_reason = "OCR_EXTRACTION_FAILED"
    val_report = None

    if candidate_json and isinstance(candidate_json, dict):
        try:
            podsumowanie = candidate_json.get("podsumowanie") or {}
            net_val = Decimal(str(podsumowanie["suma_netto"])) if ("suma_netto" in podsumowanie and podsumowanie["suma_netto"] is not None) else None
            vat_val = Decimal(str(podsumowanie["suma_vat"])) if ("suma_vat" in podsumowanie and podsumowanie["suma_vat"] is not None) else None
            gross_val = Decimal(str(podsumowanie["suma_brutto"])) if ("suma_brutto" in podsumowanie and podsumowanie["suma_brutto"] is not None) else None

            # Parsowanie pozycji faktury bez zgadywania brakujących pól
            raw_items = candidate_json.get("pozycje_faktury", [])
            items_list = []
            for idx, itm in enumerate(raw_items, 1):
                if isinstance(itm, dict):
                    i_qty = Decimal(str(itm["ilosc"])) if ("ilosc" in itm and itm["ilosc"] is not None) else None
                    i_unit_price = Decimal(str(itm["cena_jednostkowa_netto"])) if ("cena_jednostkowa_netto" in itm and itm["cena_jednostkowa_netto"] is not None) else None
                    i_net = Decimal(str(itm["netto"])) if ("netto" in itm and itm["netto"] is not None) else None
                    i_vat = Decimal(str(itm["vat_kwota"])) if ("vat_kwota" in itm and itm["vat_kwota"] is not None) else None
                    i_gross = Decimal(str(itm["brutto"])) if ("brutto" in itm and itm["brutto"] is not None) else None

                    raw_tax = str(itm.get("stawka_vat") or itm.get("vat_stawka") or itm.get("stawka") or "").strip()
                    i_tax_rate = None
                    for tr in TaxRateGroup:
                        if tr.value.lower() == raw_tax.lower() or tr.name.lower() == raw_tax.lower():
                            i_tax_rate = tr
                            break

                    items_list.append(
                        InvoiceLineItem(
                            position=idx,
                            description=str(itm.get("opis", f"Pozycja {idx}")),
                            quantity=i_qty,
                            unit_price_net=i_unit_price,
                            net_amount=i_net,
                            tax_rate=i_tax_rate,
                            tax_amount=i_vat,
                            gross_amount=i_gross
                        )
                    )

            seller_info = candidate_json.get("sprzedawca_token") or candidate_json.get("sprzedawca_nazwa")
            seller_nip = candidate_json.get("sprzedawca_nip_token") or candidate_json.get("sprzedawca_nip")
            buyer_info = candidate_json.get("nabywca_token") or candidate_json.get("nabywca_nazwa")
            buyer_nip = candidate_json.get("nabywca_nip_token") or candidate_json.get("nabywca_nip")

            # Numer i data bez wymyślania
            inv_num = candidate_json.get("numer_faktury")
            inv_num_clean = str(inv_num).strip() if inv_num else None

            inv_date = None
            raw_date = candidate_json.get("data_wystawienia")
            if raw_date:
                try:
                    inv_date = date.fromisoformat(str(raw_date).strip()[:10])
                except Exception:
                    inv_date = None

            inv_curr = candidate_json.get("waluta")
            inv_curr_clean = str(inv_curr).strip() if inv_curr else None

            # Podsumowania podatkowe tylko gdy rzeczywiście zadeklarowane
            tax_summaries = []
            if net_val is not None and vat_val is not None and gross_val is not None:
                dominant_rate = items_list[0].tax_rate if (items_list and items_list[0].tax_rate) else None
                if dominant_rate:
                    tax_summaries.append(
                        TaxGroupSummary(tax_rate=dominant_rate, net_amount=net_val, tax_amount=vat_val, gross_amount=gross_val)
                    )

            # Tworzymy ocr_source tylko jeśli wykryto jakiekolwiek dane
            if any([inv_num_clean, inv_date, inv_curr_clean, net_val, gross_val, items_list, seller_nip, buyer_nip]):
                ocr_source = InvoiceSourceData(
                    invoice_number=inv_num_clean,
                    issue_date=inv_date,
                    currency=inv_curr_clean,
                    seller=InvoiceParty(name=str(seller_info or "Nieznany Sprzedawca"), tax_id=str(seller_nip or ""), is_polish_nip=bool(seller_nip)),
                    buyer=InvoiceParty(name=str(buyer_info or "Nieznany Nabywca"), tax_id=str(buyer_nip or ""), is_polish_nip=bool(buyer_nip)),
                    items=items_list,
                    tax_summaries=tax_summaries,
                    total_net=net_val,
                    total_tax=vat_val,
                    total_gross=gross_val
                )

                is_complete = bool(
                    inv_num_clean
                    and inv_date
                    and inv_curr_clean
                    and net_val is not None
                    and vat_val is not None
                    and gross_val is not None
                    and items_list
                    and all(it.quantity is not None and it.unit_price_net is not None and it.tax_rate is not None for it in items_list)
                )
                if is_complete:
                    extraction_status = "SUCCESS"
                    review_reason = "OCR_REQUIRES_HUMAN_AUDIT"
                else:
                    extraction_status = "PARTIAL"
                    review_reason = "PARTIAL_EXTRACTION_INCOMPLETE_FIELDS"

                val_report = InvoiceMathValidator.validate(ocr_source)
        except Exception as parse_err:
            logger.warning(f"Błąd parsowania odczytu OCR: {parse_err}")
            ocr_source = None
            extraction_status = "FAILED"
            review_reason = "OCR_PARSE_ERROR"
            ocr_source = None
            extraction_status = "FAILED"
            review_reason = "OCR_PARSE_ERROR"

    if ocr_source is None:
        extraction_status = "FAILED"
        review_reason = "OCR_EXTRACTION_FAILED"
        val_report = ValidationReport(
            is_valid=False,
            status=DocumentStatus.REQUIRES_REVIEW,
            reconciliation_errors=["OCR_EXTRACTION_FAILED: Model/OCR nie odczytał wymaganych pól faktury ze skanu."]
        )

    # Hipoteza OCR bezwzględnie wymaga weryfikacji i zatwierdzenia przez człowieka
    val_report.status = DocumentStatus.REQUIRES_REVIEW
    if has_leftovers:
        val_report.reconciliation_errors.append("Wykryto nierozwiązane tokeny po detokenizacji.")
        val_report.is_valid = False

    ai_sugg = AISuggestions(
        suggested_debit_account=str(candidate_json.get("konto_wn", "401-01")),
        suggested_credit_account=str(candidate_json.get("konto_ma", "210-01")),
        suggested_gtu_codes=[str(candidate_json.get("sugerowany_kod_gtu", "GTU_12"))],
        accounting_notes="Ekstrakcja OCR/Vision. Wymaga weryfikacji i zatwierdzenia przez operatora przed eksportem."
    )

    ver = repository.save_extraction_version(
        attempt_id=attempt_id,
        lease_token=lease_token,
        source_data=ocr_source,
        ai_suggestions=ai_sugg,
        validation_report=val_report,
        source_type=SourceType.OCR_HYPOTHESIS,
        extraction_status=extraction_status,
        review_reason=review_reason
    )

    _cleanup_transient_memory(attempt_id)
    return {
        "status": val_report.status.value,
        "version_id": ver.id if ver else None,
        "is_valid": val_report.is_valid,
        "reconciliation_errors": val_report.reconciliation_errors
    }


def _cleanup_transient_memory(attempt_id: str):
    """
    Czyszczenie ulotnych słowników mapowania PII z pamięci RAM workerów.
    UWAGA: Plik źródłowy dokumentu NIE jest kasowany z magazynu (wymóg audytowy).
    """
    if redis_client:
        try:
            redis_client.delete(f"mask:{attempt_id}")
            logger.info(f"Próba {attempt_id}: Wyczyszczono ulotną pamięć tokenów PII z RAM.")
        except Exception as e:
            logger.error(f"Próba {attempt_id}: Błąd czyszczenia klucza Redis: {e}")


@celery_app.task(bind=True, max_retries=3, default_retry_delay=10)
def process_consultation_task(self, task_id: str, file_path: str):
    """
    Asynchroniczne zadanie Celery do przetwarzania zapytania z pliku E-mail / PDF / TXT
    przez pełny potok RAG (RODO -> pgvector -> Reranker -> Gemini 3.5 -> Detokenizacja).
    """
    logger.info(f"Consultation Task {task_id}: Starting async processing for file {file_path}...")
    try:
        ext = os.path.splitext(file_path)[1].lower()
        extracted_text = ""

        if ext == ".pdf":
            extracted_text = extract_pdf_text(file_path)
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                extracted_text = f.read()

        if not extracted_text or not extracted_text.strip():
            raise ValueError(f"File {file_path} contains no readable text.")

        from rag.pipeline import run_rag_pipeline
        result = run_rag_pipeline(user_query=extracted_text)

        logger.info(f"Consultation Task {task_id}: Successfully processed RAG consultation.")
        _cleanup_task_resources(task_id, file_path)
        return result

    except Exception as exc:
        logger.error(f"Consultation Task {task_id}: Error processing RAG: {str(exc)}")
        retries = getattr(self.request, "retries", 0)
        max_retries = getattr(self, "max_retries", 3)
        if retries >= max_retries:
            _cleanup_task_resources(task_id, file_path)
        try:
            raise self.retry(exc=exc)
        except Exception as retry_exc:
            raise retry_exc

