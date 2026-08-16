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



def generate_embedding(text: str) -> List[float]:
    """Generuje wektor dla pojedynczego tekstu."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        text = f"passage: {text}"
    vector = embedder.encode(text, normalize_embeddings=True)
    return vector.tolist()

def generate_embeddings_batch(texts: List[str], batch_size: int = 32) -> List[List[float]]:
    """Szybkie, wsadowe generowanie wektorów dla listy tekstów (używane podczas ingestu)."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        formatted_texts = [f"passage: {t}" for t in texts]
    else:
        formatted_texts = texts
    
    vectors = embedder.encode(formatted_texts, batch_size=batch_size, normalize_embeddings=True)
    return [v.tolist() for v in vectors]

def generate_query_embedding(query: str) -> List[float]:
    """Generuje wektor dla zapytania użytkownika."""
    embedder = get_embedder()
    if "e5" in MODEL_NAME.lower():
        query = f"query: {query}"
    vector = embedder.encode(query, normalize_embeddings=True)
    return vector.tolist()

def retrieve_and_rerank(query: str, top_k: int = 5, score_threshold: float = 0.70) -> List[Dict[str, Any]]:
    """
    1. Generuje embedding zapytania.
    2. Wykonuje wyszukiwanie hybrydowe w pgvector (HNSW + FTS RRF).
    3. Przeprowadza ocenę Cross-Encoderem z normalizacją Sigmoid i odrzuca dokumenty poniżej score_threshold.
    """
    from rag.db import search_hybrid_db

    if not query or not query.strip():
        return []

    query_clean = query.strip()
    query_vec = generate_query_embedding(query_clean)
    candidates = search_hybrid_db(query_embedding=query_vec, query_text=query_clean, top_k=top_k * 3)

    if not candidates:
        return []

    reranker = get_reranker()
    if reranker:
        # Przekazujemy pełną treść artykułu (do 8000 znaków) dzięki oknu kontekstowemu 8192 tokenów w polish-reranker-roberta-v3
        pairs = [[query_clean, (cand.get("content") or "")[:8000]] for cand in candidates]
        raw_scores = reranker.predict(pairs)


        for i, cand in enumerate(candidates):
            # Normalizacja surowych logitów do zakresu [0.0, 1.0] za pomocą funkcji Sigmoid
            raw_val = float(raw_scores[i])
            prob_val = sigmoid(raw_val)
            cand["rerank_raw_score"] = raw_val
            cand["rerank_score"] = round(prob_val, 4)

        # Filtrowanie wedle progu istotności RODO / Grounding (> 0.70)
        filtered = [c for c in candidates if c.get("rerank_score", 0.0) >= score_threshold]
        filtered.sort(key=lambda x: x.get("rerank_score", 0.0), reverse=True)
        return filtered[:top_k]
    else:
        # Fallback jeśli Cross-Encoder nie jest dostępny
        for cand in candidates:
            cand["rerank_score"] = round(float(cand.get("rrf_score", 0.0)), 4)
        return candidates[:top_k]

