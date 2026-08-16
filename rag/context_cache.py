import os
import logging
from typing import Optional, Any

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False


def get_or_create_context_cache(
    client: Any,
    context_text: str,
    display_name: str = "tax_laws_cache",
    model_name: Optional[str] = None,
    ttl_seconds: int = 3600
) -> Optional[str]:
    """
    Zarządza pamięcią podręczną kontekstu (Context Caching) w Google GenAI API.
    Szuka istniejącego aktywnego bufora; jeśli brak - tworzy nowy zbuforowany obiekt (TTL = 1h).
    Zwraca identyfikator bufora (cache.name) lub None w przypadku awarii/braku uprawnień.
    """
    if not HAS_GENAI or client is None:
        return None

    try:
        # Sprawdzanie istniejących buforów w chmurze
        existing_caches = list(client.caches.list())
        for cache in existing_caches:
            if getattr(cache, "display_name", None) == display_name:
                logger.info(f"[Context Caching] Znaleziono aktywny bufor w chmurze: {cache.name}")
                return cache.name

        # Tworzenie nowego bufora jeśli nie istnieje
        target_model = model_name or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
        logger.info(f"[Context Caching] Tworzenie nowego bufora kontekstu '{display_name}' dla modelu {target_model} w chmurze Google (TTL={ttl_seconds}s)...")
        new_cache = client.caches.create(
            model=target_model,
            config=types.CreateCachedContentConfig(
                display_name=display_name,
                contents=[context_text],
                ttl=f"{ttl_seconds}s"
            )
        )
        logger.info(f"[Context Caching] Pomyślnie utworzono bufor: {new_cache.name}")
        return new_cache.name
    except Exception as e:
        logger.warning(f"[Context Caching] Nie udało się utworzyć bufora w chmurze ({e}). Kontynuuję w trybie bez buforowania.")
        return None
