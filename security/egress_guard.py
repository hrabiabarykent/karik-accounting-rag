"""
Kontekstowy strażnik wyjścia do chmury (Scoped Cloud Egress Guard).
Zamknięty kontrakt: Guard przyjmuje wyłącznie zapieczętowaną kopertę SanitizedPayloadEnvelope
i sam przekazuje jej zawartość do dedykowanego adaptera chmurowego (Gemini SDK / Context Cache).
Brak swobodnego przyjmowania funkcji transportowych ani parametrów *args/**kwargs.

UWAGA AUDYTOWA:
Skrót kryptograficzny SHA-256 potwierdza niezmienność i nienaruszalność treści względem
zapieczętowanego wyniku procedury sanitizera. Nie stanowi on dowodu merytorycznej skuteczności
pseudonimizacji RODO (skuteczność zależy od kompletności detekcji reguł Presidio i modeli NER).
"""

import os
import re
import json
import hashlib
import logging
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, ConfigDict

logger = logging.getLogger(__name__)

# Opcjonalne importy SDK Gemini (Google GenAI / google.generativeai)
try:
    from google import genai
    from google.genai import types
    HAS_NEW_GENAI = True
except ImportError:
    HAS_NEW_GENAI = False
    try:
        import google.generativeai as gemini
    except ImportError:
        gemini = None


class SecurityPrivacyViolationError(Exception):
    """Wyjątek rzucany przy próbie wysłania payloadu niezabezpieczonego lub ze statusem błędu."""
    pass


class SanitizedPayloadEnvelope(BaseModel):
    """
    Niezmienna, zapieczętowana koperta przygotowana przez moduł sanityzacji (Presidio / Gemma SLM).
    Zawiera skrót kryptograficzny SHA256 treści, co uniemożliwia podmianę tekstu po sprawdzeniu.
    """
    model_config = ConfigDict(frozen=True)

    payload_text: str
    sha256_hash: str
    attempt_id: str
    tenant_id: str
    privacy_cleared: bool
    has_unresolved_tokens: bool = False
    sanitizer_engine: str = "Presidio+SpaCyPL+GemmaLocal"
    created_at: str = ""


def seal_sanitized_envelope(
    payload_text: str,
    attempt_id: str,
    tenant_id: str,
    privacy_cleared: bool,
    has_unresolved_tokens: bool = False,
    sanitizer_engine: str = "Presidio+SpaCyPL+GemmaLocal"
) -> SanitizedPayloadEnvelope:
    """Tworzy zapieczętowaną kopertę z wyliczonym hashem SHA-256."""
    calc_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
    return SanitizedPayloadEnvelope(
        payload_text=payload_text,
        sha256_hash=calc_hash,
        attempt_id=attempt_id,
        tenant_id=tenant_id,
        privacy_cleared=privacy_cleared,
        has_unresolved_tokens=has_unresolved_tokens,
        sanitizer_engine=sanitizer_engine,
        created_at=datetime.now(timezone.utc).isoformat()
    )


class BaseCloudAdapter:
    """Interfejs adaptera chmurowego."""
    def generate_completion(
        self,
        payload_text: str,
        system_instruction: str,
        model_name: str,
        response_mime_type: str = "application/json"
    ) -> str:
        raise NotImplementedError


class GeminiCloudAdapter(BaseCloudAdapter):
    """
    Dedykowany adapter chmurowy dla Google Gemini (obsługuje new genai SDK, context cache i legacy SDK).
    Gwarantuje, że do chmury trafia wyłącznie zapieczętowany tekst z koperty.
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")

    def generate_completion(
        self,
        payload_text: str,
        system_instruction: str,
        model_name: str,
        response_mime_type: str = "application/json"
    ) -> str:
        if not self.api_key or self.api_key in ("your_gemini_api_key_here", "dummy_key"):
            raise RuntimeError("GEMINI_API_KEY nie jest skonfigurowany w środowisku.")

        full_prompt = f"{system_instruction}\n\nTekst wejściowy:\n{payload_text}" if system_instruction else payload_text

        # 1. Ścieżka New GenAI SDK (google.genai)
        if HAS_NEW_GENAI:
            client = genai.Client(api_key=self.api_key)

            # Buforowanie kontekstu prawnego (Context Caching), jeśli dostępne
            cached_content_name = None
            try:
                from rag.context_cache import get_or_create_context_cache
                cached_content_name = get_or_create_context_cache(
                    client=client,
                    context_text=full_prompt,
                    display_name=f"karik_cache_{hashlib.sha256(full_prompt.encode('utf-8')).hexdigest()[:8]}",
                    model_name=model_name
                )
            except Exception as cache_err:
                logger.debug(f"Pominięto Gemini context cache: {cache_err}")

            gen_config = types.GenerateContentConfig(response_mime_type=response_mime_type)
            if cached_content_name:
                gen_config.cached_content = cached_content_name

            response = client.models.generate_content(
                model=model_name,
                contents=full_prompt,
                config=gen_config
            )
            return response.text or ""

        # 2. Ścieżka Legacy SDK (google.generativeai)
        if gemini is not None:
            gemini.configure(api_key=self.api_key)
            model = gemini.GenerativeModel(
                model_name=model_name,
                generation_config={"response_mime_type": response_mime_type}
            )
            response = model.generate_content(full_prompt)
            return response.text or ""

        raise RuntimeError("Brak zainstalowanej biblioteki SDK Google Gemini (google-genai lub google-generativeai).")


class ScopedEgressGuard:
    """
    Kontekstowy strażnik wywołań chmurowych powiązany z daną próbą (attempt_id) i firmą (tenant_id).
    Posiada zamknięty kontrakt: przyjmuje wyłącznie SanitizedPayloadEnvelope i sam przekazuje
    zapieczętowany payload do adaptera chmury.
    """

    def __init__(self, attempt_id: str, tenant_id: str, adapter: Optional[BaseCloudAdapter] = None):
        self.attempt_id = attempt_id
        self.tenant_id = tenant_id
        self.adapter = adapter or GeminiCloudAdapter()
        self.total_calls_attempted = 0
        self.calls_blocked = 0
        self.calls_executed = 0

    def execute_completion(
        self,
        envelope: SanitizedPayloadEnvelope,
        system_instruction: str = "",
        model_name: Optional[str] = None,
        response_mime_type: str = "application/json"
    ) -> str:
        """
        Zamknięty punkt wejścia do wywołania modelu chmurowego.
        Weryfikuje:
        1. Czy koperta należy do kontekstu (attempt_id, tenant_id).
        2. Czy status prywatności to True (privacy_cleared=True).
        3. Czy brak nierozwiązanych tokenów (has_unresolved_tokens=False).
        4. Czy hash SHA-256 payloadu zgadza się z pieczęcią.
        W przypadku jakiegokolwiek błędu rzuca SecurityPrivacyViolationError.
        """
        self.total_calls_attempted += 1
        target_model = model_name or os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

        # Weryfikacja 1: Kontekst tenanta i próby
        if envelope.attempt_id != self.attempt_id or envelope.tenant_id != self.tenant_id:
            self.calls_blocked += 1
            err_msg = (
                f"Naruszenie kontekstu bezpieczeństwa: koperta należy do ({envelope.tenant_id}, {envelope.attempt_id}), "
                f"a guard obsługuje ({self.tenant_id}, {self.attempt_id}). Blokada wysyłki."
            )
            logger.error(err_msg)
            raise SecurityPrivacyViolationError(err_msg)

        # Weryfikacja 2: Status prywatności
        if not envelope.privacy_cleared:
            self.calls_blocked += 1
            err_msg = f"Koperta dla próby {self.attempt_id} nie ma zatwierdzonego statusu prywatności (privacy_cleared=False). Blokada."
            logger.error(err_msg)
            raise SecurityPrivacyViolationError(err_msg)

        # Weryfikacja 3: Nierozwiązane tokeny
        if envelope.has_unresolved_tokens:
            self.calls_blocked += 1
            err_msg = f"Koperta dla próby {self.attempt_id} zawiera nierozwiązane tokeny PII. Blokada wysyłki do chmury."
            logger.error(err_msg)
            raise SecurityPrivacyViolationError(err_msg)

        # Weryfikacja 4: Integralność hasha (Ochrona przed podmianą payloadu)
        current_hash = hashlib.sha256(envelope.payload_text.encode("utf-8")).hexdigest()
        if current_hash != envelope.sha256_hash:
            self.calls_blocked += 1
            err_msg = f"Naruszenie integralności: hash payloadu ({current_hash}) nie zgadza się z pieczęcią ({envelope.sha256_hash}). Blokada."
            logger.error(err_msg)
            raise SecurityPrivacyViolationError(err_msg)

        # Wszystkie kontrole zaliczone - Guard sam deleguje wykonanie do adaptera
        self.calls_executed += 1
        logger.info(
            f"ScopedEgressGuard ({self.tenant_id}/{self.attempt_id}): Wywołanie chmurowe zatwierdzone. "
            f"Model: {target_model}, Payload SHA256: {envelope.sha256_hash[:12]}..."
        )
        return self.adapter.generate_completion(
            payload_text=envelope.payload_text,
            system_instruction=system_instruction,
            model_name=target_model,
            response_mime_type=response_mime_type
        )
