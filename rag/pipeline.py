import os
import re
import json
import logging
from typing import Dict, Any, List, Tuple, Optional
try:
    from dotenv import load_dotenv
    dotenv_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if os.path.exists(dotenv_path):
        load_dotenv(dotenv_path, override=False)
    else:
        load_dotenv()
except ImportError:
    pass

from pii_sanitizer import PresidioInvoiceSanitizer, detokenize_text
from rag.retriever import retrieve_and_rerank

logger = logging.getLogger(__name__)


# Model Gemini dla syntezy prawnej
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")


try:
    from google import genai
    from google.genai import types
    HAS_NEW_GENAI = True
except ImportError:
    HAS_NEW_GENAI = False
    try:
        import google.generativeai as gemini
        if GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
            gemini.configure(api_key=GEMINI_API_KEY)
    except Exception:
        pass


ACT_FILE_MAP = {
    "PIT": "PIT.html",
    "CIT": "CIT.html",
    "VAT": "VAT.html",
    "ORDYNACJA": "Ordynacja_Podatkowa.html",
    "UOR": "UoR_Rachunkowosc.html",
    "ZUS": "ZUS_System_Ubezpieczen.html",
    "PP": "Prawo_Przedsiebiorcow.html"
}


from urllib.parse import quote

def generate_article_file_link(act_code: str, article_number: str) -> str:
    """Generuje klikalne hiperłącze HTTP do artykułu serwowane przez serwer FastAPI pod /static/pobrane_ustawy/."""
    file_name = ACT_FILE_MAP.get(act_code.upper(), f"{act_code}.html")
    clean_art_id = article_number.split()[0] if article_number else ""
    gateway_url = os.getenv("GATEWAY_URL", "http://localhost:8000")
    return f"{gateway_url}/static/pobrane_ustawy/{file_name}#arti_{clean_art_id}"




def format_context_for_llm(retrieved_articles: List[Dict[str, Any]], max_context_chars: int = 26000) -> Tuple[str, bool]:
    """
    Format wyjściowy CAŁOŚCIOWYCH (nieuciętych) artykułów do promptu Gemini.
    Zwraca (tekst_kontekstu, czy_znaleziono_w_lokalnej_bazie).
    Mniej istotne artykuły są w całości pomijane, gdy przekraczają łączny limit.
    """
    if not retrieved_articles:
        return "Brak artykułów w bazie pasujących semantycznie do zapytania.", False

    top_score = retrieved_articles[0].get("rerank_score", 0.0)
    found_in_local_db = top_score >= 0.30

    formatted_chunks = []
    current_len = 0

    for art in retrieved_articles:
        act = art.get("act_code", "USTAWA")
        art_num = art.get("article_number", "")
        title = art.get("full_title", f"{act} Art. {art_num}")
        score = art.get("rerank_score", 0.0)
        content = art.get("content", "").strip()

        chunk_text = f"--- ARTYKUŁ: {title} (Trafność: {score:.3f}) ---\n{content}\n"

        # Jeśli dodanie CAŁEGO artykułu przekroczyłoby limit, pomijamy ten mniej istotny artykuł i kończymy
        if current_len + len(chunk_text) > max_context_chars and formatted_chunks:
            logger.info(f"Pomijam artykuł {title} (długość {len(chunk_text)} B), aby nie uciąć tekstu w połowie i zachować limit promptu.")
            break

        formatted_chunks.append(chunk_text)
        current_len += len(chunk_text)

    context_str = "\n".join(formatted_chunks)

    if not found_in_local_db:
        context_str = (
            "UWAGA: Żaden z poniższych artykułów nie osiągnął wysokiego progu dopasowania semantycznego (score < 0.30).\n"
            "Oznacza to, że precyzyjna podstawa prawna prawdopodobnie NIE znajduje się w lokalnej bazie zindeksowanych ustaw (PIT/CIT/VAT/Ordynacja/UoR/ZUS).\n\n"
            + context_str
        )

    return context_str, found_in_local_db


def build_prompt_and_context(user_query: str) -> Dict[str, Any]:
    """
    Krok 1 & 2 Potoku RAG:
    - Anonimizuje zapytanie RODO
    - Wyciąga artykuły z pgvector i przeprowadzanie rerankingu CUDA
    - Buduje gotowy prompt do skopiowania do Chata Gemini Online z limitem max 30 000 znaków.
    """
    if not user_query or not user_query.strip():
        raise ValueError("Treść zapytania nie może być pusta.")

    logger.info("Krok 1: Anonimizacja zapytania RODO (Presidio Engine)...")
    sanitizer = PresidioInvoiceSanitizer()
    anonymized_query, pii_mapping = sanitizer.sanitize(user_query)

    logger.info("Krok 1b: Query Rewriting - przekształcenie zapytania na słownik ustawowy (Gemma SLM)...")
    search_query = anonymized_query
    try:
        from rag.query_rewriter import rewrite_query
        rewritten = rewrite_query(anonymized_query)
        if rewritten:
            search_query = f"{rewritten} {anonymized_query}"
    except Exception as e:
        logger.warning(f"Błąd Query Rewritera (używam oryginalnego zapytania RODO): {e}")

    logger.info(f"Krok 2: Pobieranie kontekstu z bazy pgvector dla frazy: '{search_query[:100]}...'")
    retrieved_articles = retrieve_and_rerank(
        query=search_query,
        top_k=4,
        score_threshold=0.0
    )

    prompt_template_prefix = f"""Jesteś zaawansowanym asystentem i ekspertem prawa podatkowo-księgowego w Polsce.
Przeanalizuj zanonimizowane zapytanie użytkownika oraz dostarczony kontekst z artykułów ustaw.

Wiadomość po anonimizacji RODO (zachowaj tokeny <PERSON_X>, <COMPANY_NAME_X>, <PL_NIP_X> w niezmienionej formie):
<ZAPYTANIE>
{anonymized_query}
</ZAPYTANIE>

Kontekst artykułów prawnych z bazy:
<KONTEKST>
"""
    prompt_template_suffix = """
</KONTEKST>

Wygeneruj odpowiedź wyłącznie w czystym formacie JSON:
{
  "answer_text": "Treść odpowiedzi prawnej dla księgowego. Zawsze podawaj dokładne sygnatury artykułów (np. Art. 22 ust. 1 ustawy o PIT). Jeśli kontekst zawiera informację o braku dopasowania, poinformuj księgowego, że właściwy przepis nie znajduje się w lokalnej bazie aktów.",
  "risk_level": "NISKIE | ŚREDNIE | WYSOKIE",
  "risk_justification": "Krótkie uzasadnienie poziomu ryzyka podatkowego."
}
"""
    # Obliczenie dokładnego budżetu znaków na pełne, nieucięte artykuły (max 30 000 znaków łącznego promptu)
    MAX_PROMPT_LIMIT = 30000
    overhead_len = len(prompt_template_prefix) + len(prompt_template_suffix)
    max_allowed_context_len = MAX_PROMPT_LIMIT - overhead_len

    context_text, found_in_local_db = format_context_for_llm(retrieved_articles, max_context_chars=max_allowed_context_len)
    top_score = retrieved_articles[0].get("rerank_score", 0.0) if retrieved_articles else 0.0

    cited_articles_metadata = []
    for art in retrieved_articles[:3]:
        act = art.get("act_code", "USTAWA")
        art_num = art.get("article_number", "")
        link = generate_article_file_link(act, art_num)
        cited_articles_metadata.append({
            "act_code": act,
            "article_number": art_num,
            "full_title": art.get("full_title", f"{act} Art. {art_num}"),
            "rerank_score": art.get("rerank_score", 0.0),
            "file_link": link,
            "text": art.get("content") or art.get("clean_text") or art.get("chunk_text") or ""

        })


    prompt_to_copy = prompt_template_prefix + context_text + prompt_template_suffix



    return {
        "original_query": user_query,
        "anonymized_query": anonymized_query,
        "pii_mapping": pii_mapping,
        "found_in_local_db": found_in_local_db,
        "confidence_score": top_score,
        "cited_articles": cited_articles_metadata,
        "prompt_to_copy": prompt_to_copy
    }

def run_rag_pipeline(user_query: str, manual_llm_response: Optional[str] = None) -> Dict[str, Any]:
    """
    Kompletny potok RAG z obsługą wywołania API lub wklejenia odpowiedzi z Gemini Chat Online.
    """
    data = build_prompt_and_context(user_query)

    raw_llm_response = manual_llm_response or ""

    if not raw_llm_response and GEMINI_API_KEY and GEMINI_API_KEY != "your_gemini_api_key_here":
        try:
            import uuid
            from security.egress_guard import ScopedEgressGuard, seal_sanitized_envelope
            consult_session_id = f"consult-{uuid.uuid4().hex[:8]}"
            envelope = seal_sanitized_envelope(
                payload_text=data["prompt_to_copy"],
                attempt_id=consult_session_id,
                tenant_id="rag-consultation",
                privacy_cleared=True,
                has_unresolved_tokens=False
            )
            guard = ScopedEgressGuard(attempt_id=consult_session_id, tenant_id="rag-consultation")
            raw_llm_response = guard.execute_completion(
                envelope=envelope,
                model_name=GEMINI_MODEL_NAME,
                response_mime_type="application/json"
            )
        except Exception as e:
            logger.error(f"Błąd wywołania Gemini API przez Egress Guard: {e}")



    if raw_llm_response:
        try:
            # Próba wyciągnięcia JSON z tekstu odpowiedzi Gemini
            json_match = re.search(r'\{.*\}', raw_llm_response, re.DOTALL)
            if json_match:
                parsed_json = json.loads(json_match.group(0))
                if isinstance(parsed_json, dict) and "answer_text" in parsed_json:
                    anonymized_answer_json = parsed_json
                else:
                    anonymized_answer_json = {"answer_text": raw_llm_response, "risk_level": "ŚREDNIE", "risk_justification": "Odpowiedź w formacie tekstu"}
            else:
                anonymized_answer_json = {"answer_text": raw_llm_response, "risk_level": "ŚREDNIE", "risk_justification": "Odpowiedź w formacie tekstu"}
        except Exception:
            anonymized_answer_json = {"answer_text": raw_llm_response, "risk_level": "ŚREDNIE", "risk_justification": "Odpowiedź w formacie tekstu"}
    else:
        anonymized_answer_json = {
            "answer_text": "Tryb testowy (bez API Gemini). Skopiuj prompt do Gemini Online i wklej odpowiedź.",
            "risk_level": "NISKIE",
            "risk_justification": "Brak wywołania API Gemini."
        }

    raw_answer_text = anonymized_answer_json.get("answer_text", "")
    # Jeśli raw_answer_text sam w sobie jest łańcuchem JSON, wyciągamy z niego właściwą treść
    if isinstance(raw_answer_text, str) and raw_answer_text.strip().startswith("{") and "answer_text" in raw_answer_text:
        try:
            inner_json = json.loads(raw_answer_text)
            if isinstance(inner_json, dict) and "answer_text" in inner_json:
                raw_answer_text = inner_json["answer_text"]
                if "risk_level" in inner_json and inner_json["risk_level"]:
                    anonymized_answer_json["risk_level"] = inner_json["risk_level"]
                if "risk_justification" in inner_json and inner_json["risk_justification"]:
                    anonymized_answer_json["risk_justification"] = inner_json["risk_justification"]
        except Exception:
            pass

    detokenized_answer, _ = detokenize_text(raw_answer_text, data["pii_mapping"])


    if not data["found_in_local_db"]:
        disclaimer = "\n\n⚠️ **Informacja RAG**: W lokalnej bazie zindeksowanych ustaw (PIT, CIT, VAT, Ordynacja, UoR, ZUS) nie odnaleziono przepisu o wysokim dopasowaniu semantycznym (score < 0.30)."
        detokenized_answer += disclaimer

    return {
        "original_query": user_query,
        "anonymized_query": data["anonymized_query"],
        "pii_mapping_count": len(data["pii_mapping"]),
        "found_in_local_db": data["found_in_local_db"],
        "confidence_score": data["confidence_score"],
        "answer_text": detokenized_answer,
        "anonymized_answer_text": raw_answer_text,
        "risk_level": anonymized_answer_json.get("risk_level", "ŚREDNIE"),
        "risk_justification": anonymized_answer_json.get("risk_justification", ""),
        "cited_articles": data["cited_articles"],
        "prompt_to_copy": data["prompt_to_copy"]
    }

