import os
import sys
import json
import logging
import socket
import requests
from typing import Optional

logger = logging.getLogger(__name__)

# Konfiguracja punktu końcowego serwera llama.cpp (Gemma SLM)
DEFAULT_LOCAL_AI_URL = os.getenv("LOCAL_AI_URL", "http://127.0.0.1:8088/v1")


def resolve_llama_url(base_url: str) -> str:
    """Zapewnia automatyczną i poprawną adresację hosta (zarówno wewnątrz kontenera Docker, jak i poza nim w Windows)."""
    if "local-ai" in base_url:
        try:
            socket.gethostbyname("local-ai")
            # Wewnątrz kontenera Docker -> adres 'local-ai:8080' istnieje w DNS Dockera
            return base_url
        except Exception:
            # Na zewnątrz kontenera (na Windowsie) -> używamy otwartego portu hosta 127.0.0.1:8088
            return base_url.replace("local-ai:8080", "127.0.0.1:8088").replace("local-ai", "127.0.0.1:8088")
    return base_url

def rewrite_query(user_query: str, llama_cpp_url: Optional[str] = None) -> str:
    """
    Dedykowany moduł Query Rewriter (HyDE / Keyword Expansion):
    Przekształca zanonimizowane pytania klientów na język oficjalnych pojęć ustawowych (PIT, CIT, VAT, Ordynacja, UoR, ZUS).
    """
    if not user_query or not user_query.strip():
        return ""

    raw_url = (llama_cpp_url or DEFAULT_LOCAL_AI_URL).rstrip("/")
    base_url = resolve_llama_url(raw_url)

    chat_url = f"{base_url}/chat/completions" if not base_url.endswith("/chat/completions") else base_url


    logger.info(f"[Query Rewriter] Łączenie z lokalnym serwerem LLM: {chat_url}")

    system_instruction = (
        "Jesteś ekspertowym analitykiem i słownikiem prawa podatkowego w Polsce. "
        "Twoim zadaniem jest przeanalizowanie zapytania klienta i zwrócenie bogatego ciągu precyzyjnych pojęć prawnych, "
        "definicji i terminów z ustaw podatkowych (PIT, CIT, VAT, Ordynacja Podatkowa, UoR, ZUS, Prawo Przedsiębiorców).\n"
        "Wypisz WYŁĄCZNIE kluczowe frazy, pojęcia prawne i limity kwotowe oddzielone spacjami. "
        "Nie zgaduj numerów artykułów ani paragrafów."
    )

    try:
        resp = requests.post(
            chat_url,
            json={
                "messages": [
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": f"Zapytanie klienta: {user_query}"}
                ],
                "temperature": 0.1,
                "max_tokens": 5000
            },
            timeout=30
        )

        if resp.status_code == 200:
            data = resp.json()
            if "choices" in data and data["choices"]:
                msg = data["choices"][0].get("message", {})
                content = msg.get("content", "").strip()
                
                # Jeśli content jest pusty, sprawdzamy pole reasoning_content
                if not content:
                    content = msg.get("reasoning_content", "").strip()
                
                if content:
                    logger.info(f"[Query Rewriter] Sukces! Wzbogacone zapytanie: '{content}'")
                    return content

        # Fallback do tradycyjnego punktu /v1/completions w llama.cpp
        completion_url = f"{base_url.rsplit('/chat', 1)[0]}/completions"
        logger.info(f"[Query Rewriter] Odpowiedź chatu pusta. Próba przez completions: {completion_url}")
        
        raw_prompt = f"{system_instruction}\n\nZapytanie klienta: {user_query}\n\nPojęcia prawne pod pgvector:"
        c_resp = requests.post(
            completion_url,
            json={
                "prompt": raw_prompt,
                "temperature": 0.1,
                "max_tokens": 5000,
                "stop": ["\n\n", "Zapytanie klienta:"]
            },
            timeout=30
        )



        if c_resp.status_code == 200:
            c_data = c_resp.json()
            if "choices" in c_data and c_data["choices"]:
                c_content = c_data["choices"][0].get("text", "").strip()
                if c_content:
                    logger.info(f"[Query Rewriter] Completions Sukces! Wzbogacone zapytanie: '{c_content}'")
                    return c_content

        raise RuntimeError(f"Lokalny serwer LLM zwrócił pustą treść. Odpowiedź Chat: {resp.text[:200]}")

    except Exception as e:
        logger.error(f"[Query Rewriter] Błąd wywołania lokalnego serwera Gemma SLM ({base_url}): {e}")
        raise RuntimeError(f"Błąd Query Rewritera (Gemma SLM pod {base_url}): {e}") from e





if __name__ == "__main__":
    # Niezależny moduł testowy / debugera w konsoli
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    
    print("=" * 75)
    print("  TESTER MODUŁU QUERY REWRITER (Gemma SLM na GPU CUDA)")
    print("=" * 75)

    test_query = "Jak zaksięgować amortyzację auta za 220 tysięcy zł netto i odliczyć 50% VAT od paliwa?"
    print(f"\n[INPUT] Zapytanie wejściowe:\n  \"{test_query}\"")

    try:
        output_keywords = rewrite_query(test_query)
        print(f"\n[OUTPUT] Przekształcone słowa kluczowe pod pgvector:\n  \"{output_keywords}\"\n")
    except Exception as err:
        print(f"\n❌ Błąd podczas testu Query Rewritera: {err}")
