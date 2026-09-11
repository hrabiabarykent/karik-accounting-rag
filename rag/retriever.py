import os
import math
import logging
from typing import List, Dict, Any, Optional

# Wymuszenie trybu 100% Offline bez zapytań sprawdzających do HuggingFace
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

logger = logging.getLogger(__name__)


# Leniwe ładowanie modeli
_embedder_model = None
_reranker_model = None
_reranker_failed = False

MODEL_NAME = os.getenv("EMBEDDING_MODEL", "sdadas/mmlw-e5-base")
RERANKER_NAME = os.getenv("RERANKER_MODEL", "sdadas/polish-reranker-roberta-v3")



def get_device() -> str:
    """Automatycznie wykrywa i aktywuje akcelerację sprzętową NVIDIA CUDA."""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            logger.info(f"Wykryto akcelerację sprzętową NVIDIA CUDA: {gpu_name}")
            return "cuda"
    except Exception:
        pass
    return "cpu"

def sigmoid(x: float) -> float:
    """Przekształca surowy logit z Cross-Encodera na prawdopodobieństwo z przedziału [0.0, 1.0]."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0

def get_embedder():
    global _embedder_model
    if _embedder_model is None:
        device = get_device()
        try:
            from sentence_transformers import SentenceTransformer
            logger.info(f"Ładowanie dedykowanego polskiego modelu embeddingów '{MODEL_NAME}' na urządzeniu: {device} (tryb offline)...")
            _embedder_model = SentenceTransformer(MODEL_NAME, device=device, local_files_only=True)
        except Exception as e:
            logger.error(f"KRYTYCZNY BŁĄD: Nie udało się załadować wymaganego modelu embeddingów '{MODEL_NAME}': {e}")
            raise RuntimeError(f"BŁĄD MODELU EMBEDDINGÓW: Wymagany model '{MODEL_NAME}' nie mógł zostać załadowany: {e}") from e
    return _embedder_model

def get_reranker():
    global _reranker_model
    if _reranker_model is None:
        device = get_device()
        try:
            from sentence_transformers import CrossEncoder
            logger.info(f"Ładowanie dedykowanego Cross-Encodera '{RERANKER_NAME}' na urządzeniu: {device} (tryb offline)...")
            _reranker_model = CrossEncoder(RERANKER_NAME, device=device, local_files_only=True)

        except Exception as e:
            logger.error(f"KRYTYCZNY BŁĄD: Nie udało się załadować wymaganego Cross-Encodera '{RERANKER_NAME}': {e}")
            raise RuntimeError(f"BŁĄD CROSS-ENCODERA: Wymagany model '{RERANKER_NAME}' nie mógł zostać załadowany: {e}") from e
    return _reranker_model



def _sanitize_vector(vec: Any) -> List[float]:
    """Zastępuje wartości NaN oraz Inf zerami i konwertuje na listę floatów."""
    if hasattr(vec, "tolist"):
        vec = vec.tolist()
    clean = []
    has_bad = False
    for x in vec:
        fx = float(x)
        if math.isnan(fx) or math.isinf(fx):
            clean.append(0.0)
            has_bad = True
        else:
            clean.append(fx)
    if has_bad:
        logger.warning("Wykryto i oczyszczono wartości NaN/Inf w wygenerowanym wektorze.")
    return clean


def generate_embedding(text: str) -> List[float]:
    """Generuje wektor dla pojedynczego tekstu."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        text = f"passage: {text}"
    vector = embedder.encode(text, normalize_embeddings=True)
    return _sanitize_vector(vector)

def generate_embeddings_batch(texts: List[str], batch_size: int = 32) -> List[List[float]]:
    """Szybkie, wsadowe generowanie wektorów dla listy tekstów (używane podczas ingestu)."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        formatted_texts = [f"passage: {t}" for t in texts]
    else:
        formatted_texts = texts
    
    vectors = embedder.encode(formatted_texts, batch_size=batch_size, normalize_embeddings=True)
    return [_sanitize_vector(v) for v in vectors]

def generate_query_embedding(query: str) -> List[float]:
    """Generuje wektor dla zapytania użytkownika."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        query = f"query: {query}"
    vector = embedder.encode(query, normalize_embeddings=True)
    return _sanitize_vector(vector)

def retrieve_and_rerank(
    query: str,
    top_k: int = 5,
    score_threshold: float = 0.0,
    enable_act_bias: bool = False
) -> List[Dict[str, Any]]:
    """
    1. Generuje embedding zapytania (na CUDA jeśli dostępne).
    2. Wykonuje wyszukiwanie hybrydowe w pgvector (HNSW + FTS RRF) pobierając 75 kandydatów.
    3. Przeprowadza ocenę Cross-Encoderem z normalizacją Sigmoid i opcjonalnym routing boostem.
    4. Deterministycznie sortuje i deduplikuje jednostki (maksymalnie 2 jednostki na artykuł w Top K).
    """
    from rag.db import search_hybrid_db, detect_act_bias

    if not query or not query.strip():
        return []

    query_clean = query.strip()
    query_vec = generate_query_embedding(query_clean)
    candidates = search_hybrid_db(
        query_embedding=query_vec,
        query_text=query_clean,
        top_k=75,
        enable_act_bias=enable_act_bias
    )

    if not candidates:
        return []

    bias_act = detect_act_bias(query_clean) if enable_act_bias else None
    reranker = get_reranker()
    if reranker:
        pairs = [[query_clean, f"{cand.get('full_title', '')}\n{(cand.get('content') or cand.get('clean_text') or '')[:2500]}"] for cand in candidates]
        raw_scores = reranker.predict(pairs)

        for i, cand in enumerate(candidates):
            raw_val = float(raw_scores[i])
            prob_val = sigmoid(raw_val)
            boost = 0.03 if (bias_act and cand.get("act_code") == bias_act) else 0.0
            cand["rerank_raw_score"] = raw_val
            cand["sort_score"] = prob_val + boost
            cand["rerank_score"] = round(prob_val + boost, 4)

        # Deterministyczne sortowanie ze stabilnym tie-breakerem po unit_id ASC
        candidates.sort(key=lambda x: x.get("unit_id") or "")
        candidates.sort(
            key=lambda x: (
                x.get("sort_score", 0.0),
                float(x.get("rrf_score", 0.0) or 0.0)
            ),
            reverse=True
        )
    else:
        for cand in candidates:
            cand["sort_score"] = float(cand.get("rrf_score", 0.0))
            cand["rerank_score"] = round(float(cand.get("rrf_score", 0.0)), 4)
        candidates.sort(key=lambda x: x.get("unit_id") or "")
        candidates.sort(
            key=lambda x: (
                x.get("sort_score", 0.0),
                float(x.get("rrf_score", 0.0) or 0.0)
            ),
            reverse=True
        )

    # Filtrowanie wedle progu oraz deduplikacja: max 2 jednostki z tego samego artykułu
    deduped = []
    art_counts = {}
    for cand in candidates:
        if cand.get("rerank_score", 0.0) < score_threshold:
            continue
        key = (cand.get("act_code"), str(cand.get("article_number", "")))
        if art_counts.get(key, 0) < 2:
            deduped.append(cand)
            art_counts[key] = art_counts.get(key, 0) + 1
        if len(deduped) >= top_k:
            break

    return deduped
