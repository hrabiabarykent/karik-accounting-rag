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

# Initialize OpenAI SDK client for llama-server
local_ai_client = openai.OpenAI(base_url=LOCAL_AI_URL, api_key="not-needed")

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

def refresh_mask_ttl(task_id: str, ttl_seconds: int = 1800):
    if redis_client:
        try:
            redis_client.expire(f"mask:{task_id}", ttl_seconds)
            logger.info(f"Task {task_id}: Refreshed Redis TTL to {ttl_seconds}s")
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to refresh Redis TTL: {e}")

@celery_app.task(name="tasks.process_invoice_task", bind=True, max_retries=3, default_retry_countdown=60)
@observe(name="process_invoice_task")
def process_invoice_task(self, task_id: str, file_path: str):

    logger.info(f"Running processing pipeline for task {task_id}")
    
    mapping_dictionary = {}
    candidate_json = {}
    
    try:
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"File not found: {file_path}")

        sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=f"{LOCAL_AI_URL}/chat/completions")
        ext = os.path.splitext(file_path)[1].lower()

        raw_text = ""
        # Jeżeli plik to cyfrowy PDF lub KSeF XML – wyciągamy tekst bezpośrednio (oszczędność VRAM)
        if ext == ".pdf":
            raw_text = extract_pdf_text(file_path)
        elif ext == ".xml":
            raw_text = extract_ksef_xml_text(file_path)

        # ------------------------------------------------------------
        # KROK A: Hybrid Presidio Masking (SLM + Reguły Matematyczne)
        # ------------------------------------------------------------
        if raw_text:
            logger.info(f"Task {task_id}: Extracted native PDF text layer. Running Presidio Sanitizer...")
            masked_text, mapping_dictionary = sanitizer.sanitize(raw_text)
            image_url_data = None
        else:
            logger.info(f"Task {task_id}: Scanned document / Image. Starting Vision Ingest via {LOCAL_AI_URL}...")
            base64_image = encode_image_to_base64(file_path)
            mime_type = "application/pdf" if ext == ".pdf" else f"image/{ext.replace('.', '') if ext in ('.jpg', '.png', '.jpeg') else 'jpeg'}"
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

            # Faza Presidio Sanitizer na odczytanym tekście
            presidio_masked_text, presidio_map = sanitizer.sanitize(slm_masked_text)
            masked_text = presidio_masked_text
            mapping_dictionary = {**slm_mapping, **presidio_map}

        # Zapis słownika mapowania do Redis z początkowym TTL = 1800 sekund
        if redis_client:
            redis_client.setex(f"mask:{task_id}", 1800, json.dumps(mapping_dictionary))
            logger.info(f"Task {task_id}: RODO Presidio mapping saved to Redis with TTL=1800s.")

        # ------------------------------------------------------------
        # KROK B: Zaawansowana Analiza Finansowa (Chmura: Gemini API)
        # ------------------------------------------------------------
        logger.info(f"Task {task_id}: Starting Step B (Gemini API) using model {GEMINI_MODEL_NAME}...")
        gemini_prompt = f"{PROMPT_STEP_B}\n\nTekst wejściowy:\n{masked_text}"

        if HAS_NEW_GENAI and GEMINI_API_KEY:
            client = genai.Client(api_key=GEMINI_API_KEY)
            cloud_response = client.models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=gemini_prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json")
            )
            raw_cloud_json = cloud_response.text
        else:
            gemini_model = gemini.GenerativeModel(GEMINI_MODEL_NAME)
            cloud_response = gemini_model.generate_content(
                gemini_prompt,
                generation_config={"response_mime_type": "application/json"}
            )
            raw_cloud_json = cloud_response.text

        accounting_cloud_json = json.loads(raw_cloud_json)

        refresh_mask_ttl(task_id, 1800)

        # ------------------------------------------------------------
        # KROK C: Tolerancyjna Detokenizacja w Pamięci RAM
        # ------------------------------------------------------------
        logger.info(f"Task {task_id}: Starting Step C (Tolerant Detokenization)...")
        candidate_json, has_leftover_tokens = tolerant_detokenize(accounting_cloud_json, mapping_dictionary)
        refresh_mask_ttl(task_id, 1800)

        # ------------------------------------------------------------
        # KROK D: Pętla Weryfikacji Quality Assurance (Lokalny Audyt)
        # ------------------------------------------------------------
        logger.info(f"Task {task_id}: Starting Step D (Audit Loop)...")
        
        try:
            audit_messages = [
                {"role": "user", "content": f"{PROMPT_STEP_D}\n\nKandydacki JSON:\n{json.dumps(candidate_json, ensure_ascii=False)}"}
            ]
            if image_url_data:
                audit_messages = [
                    {"role": "user", "content": [
                        {"type": "text", "text": f"{PROMPT_STEP_D}\n\nKandydacki JSON:\n{json.dumps(candidate_json, ensure_ascii=False)}"},
                        {"type": "image_url", "image_url": {"url": image_url_data}}
                    ]}
                ]

            audit_response = local_ai_client.chat.completions.create(
                model=LOCAL_AI_MODEL,
                messages=audit_messages,
                response_format={"type": "json_object"}
            )
            
            final_verified_json_content = audit_response.choices[0].message.content
            final_verified_json = json.loads(final_verified_json_content)
            
            final_json_detokenized, leftover_in_d = tolerant_detokenize(final_verified_json, mapping_dictionary)
            
            if leftover_in_d or has_leftover_tokens:
                logger.warning(f"Task {task_id}: Unreplaced tokens detected post-audit. Flagging for manual verification.")
                status = "REQUIRES_MANUAL_VERIFICATION"
            else:
                status = "VERIFIED"
                
            logger.info(f"Task {task_id}: Step D completed with status: {status}.")
            
            # Sukces - bezpieczne posprzątanie zasobów
            _cleanup_task_resources(task_id, file_path)
            return {
                "verification_status": status,
                "invoice_data": final_json_detokenized
            }
            
        except Exception as audit_err:
            logger.warning(f"Task {task_id}: Step D verification failed ({str(audit_err)}). Falling back to FAIL-SAFE.")
            _cleanup_task_resources(task_id, file_path)
            return {
                "verification_status": "REQUIRES_MANUAL_VERIFICATION",
                "invoice_data": candidate_json
            }

    except Exception as exc:
        logger.error(f"Task {task_id}: Critical error in pipeline: {str(exc)}")
        # Jeśli osiągnięto limit powtórzeń, czyścimy zasoby przed ostatecznym błędem
        retries = getattr(self.request, "retries", 0)
        max_retries = getattr(self, "max_retries", 3)
        if retries >= max_retries:
            _cleanup_task_resources(task_id, file_path)
        try:
            raise self.retry(exc=exc)
        except Exception as retry_exc:
            raise retry_exc

def _cleanup_task_resources(task_id: str, file_path: str):
    """Bezpieczne czyszczenie kluczy Redis i plików tymczasowych po zakończeniu zadania."""
    if redis_client:
        try:
            redis_client.delete(f"mask:{task_id}")
            logger.info(f"Task {task_id}: Cleared RODO mapping from Redis.")
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to clear Redis mapping: {str(e)}")
            
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            logger.info(f"Task {task_id}: Removed temporary file {file_path}")
        except Exception as e:
            logger.error(f"Task {task_id}: Failed to remove file {file_path}: {str(e)}")

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

