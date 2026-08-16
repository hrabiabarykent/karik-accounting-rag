# Dokumentacja Architektoniczna i Karta Wdrożenia Technologicznego
## Tytuł Projektu: Hybrydowy, Skonteneryzowany Moduł Księgowy z Warstwą Izolacji RODO i Pętlą Samokontroli Quality Assurance (Wersja v3 - Silnik llama.cpp)

---

## 1. Wstęp i Założenia Biznesowe

Niniejsza specyfikacja definiuje architekturę systemu klasy Enterprise służącego do automatycznej interpretacji, anonimizacji oraz dekretacji dokumentów księgowych (PDF, OCR, e-faktury, wiadomości E-mail). Głównym celem systemu jest zapewnienie bezkompromisowego poziomu bezpieczeństwa danych osobowych (zgodność z RODO) przy jednoczesnym wykorzystaniu potężnych, chmurowych modeli językowych (LLM) do zaawansowanej analityki finansowo-księgowej. 

### Kluczowe wyróżniki biznesowe:
*   **Zero-Trust Cloud Architecture**: Dane wrażliwe nigdy nie opuszczają lokalnej infrastruktury firmy w formie jawnej.
*   **Smart Context Preservation**: Zamiast klasycznej, destruktywnej anonimizacji, system stosuje maskowanie kontekstowe (semantyczne), co pozwala modelom chmurowym zachować pełną zdolność wnioskowania biznesowego.
*   **Reconciliation Loop (Pętla Samokontroli)**: Zastosowanie lokalnego audytu krzyżowego eliminuje ryzyko halucynacji AI, gwarantując zerową tolerancję dla błędów matematycznych oraz pominięć pozycji faktury.

---

## 2. Architektura Systemu i Przepływ Danych

System opiera się na asynchronicznym, sterowanym zdarzeniami potoku przetwarzania (Event-Driven Pipeline), eliminującym problem blokowania wątków HTTP podczas długotrwałych operacji AI.

### 2.1. Schemat Przepływu Danych (Data Flow Matrix)

```
[Klient / Aplikacja Główna]
       │
       ▼ (POST /v1/invoice/process) -> Natychmiastowa odpowiedź: {"task_id": "UUID", "status": "pending"}
[FastAPI Gateway] ──(Zapis fizycznego pliku)──► [Współdzielony Wolumen /tmp/accounting_storage]
       │
       ▼ (Wypchnięcie ID zadania do kolejki)
[Redis Message Broker]
       │
       ▼ (Pobranie zadania do realizacji)
[Celery Worker Pool]
       │
       ├─► [KROK A - Lokalny AI: llama-server] ◄──(Pobiera obraz/skan faktury z wolumenu)
       │     │ 
       │     ▼ (Smart Masking: Generuje zanonimizowany tekst + słownik mapowania w pamięci RAM)
       │
       ├─► [Redis Cache (State Store)] ──(Zapis słownika mapowania; flagi RODO z TTL = 600s)
       │
       ├─► [KROK B - Chmura: Google Gemini API] ◄──(Odbiera bezpieczny, zanonimizowany tekst)
       │     │
       │     ▼ (Analiza finansowa: Generuje strukturę dekretacji na bazie tokenów branżowych)
       │
       ├─► [KROK C - Detokenizator] ◄──(Pobiera słownik mapowania z Redis)
       │     │
       │     ▼ (Łączenie danych w odizolowanej pamięci RAM: Powstaje "Kandydacki JSON")
       │
       ├─► [KROK D - Weryfikator: llama-server] ◄──(Odbiera oryginalny obraz ORAZ Kandydacki JSON)
       │     │
       │     ▼ (Audyt Krzyżowy: Weryfikacja spójności kwot i pozycji; ewentualna samokorekta)
       │
[Baza Danych / Wynik] ◄──(Zapis ostatecznego, zweryfikowanego JSON)
       │
       └─► [FINALLY: Blok Sprzątający] ──► Bezpowrotne czyszczenie Redis (DEL) oraz wolumenu (RM)
```

### 2.2. Architektoniczny Rejestr Decyzji (Architectural Decision Records)

*   **Zastąpienie Ollamy przez `llama.cpp` (`llama-server`)**: Wykorzystanie natywnego, skompilowanego w C/C++ kontenera z obsługą CUDA. Zapewnia to bezpośrednią kontrolę nad alokacją warstw w VRAM (`-ngl 99`), optymalizację pamięci oraz wymuszenie obsługi pełnego okna kontekstowego modelu Gemma 4 e4b wynoszącego 128k tokenów (`-c 131072`).
*   **Natywna Multimodalność (Vision Ingest)**: Rezygnacja z zewnętrznych, klasycznych bibliotek OCR (np. Tesseract) na rzecz przetwarzania obrazu bezpośrednio przez warstwę wizyjną lokalnego modelu. Zapobiega to utracie struktury tabelarycznej dokumentu.
*   **Bezstanowość (Stateless Gateway)**: Słowniki de-anonimizacyjne są przechowywane wyłącznie w pamięci operacyjnej Redis z automatycznym czasem wygaszania (TTL = 10 minut). Brak utrwalania danych RODO na dyskach twardych w bazach relacyjnych.

---

## 3. Środowisko i Konteneryzacja (`docker-compose.yml`)

Poniższa konfiguracja zapewnia pełną izolację komponentów oraz sprzętowe przekazywanie zasobów GPU NVIDIA (CUDA Passthrough) do kontenera przetwarzania lokalnego.

```yaml
version: '3.8'

services:
  redis-broker:
    image: redis:7.2-alpine
    container_name: accounting-redis
    ports:
      - "6379:6379"
    command: redis-server --appendonly no --maxmemory 512mb --maxmemory-policy allkeys-lru
    restart: unless-stopped
    networks:
      - accounting-network

  local-ai:
    image: ghcr.io/ggerganov/llama.cpp:server-cuda
    container_name: accounting-local-ai
    volumes:
      - ./models:/models
    ports:
      - "8080:8080"
    environment:
      - CUDA_VISIBLE_DEVICES=0
    # Parametryzacja silnika pod kątem multimodalnej wersji Gemma 4 e4b
    command: >
      -m /models/gemma-4-e4b-Q4_K_M.gguf
      --mmproj /models/gemma-4-e4b-mmproj.gguf
      --host 0.0.0.0
      --port 8080
      -c 131072
      -ngl 99
      --alias gemma4-e4b
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    restart: unless-stopped
    networks:
      - accounting-network

  app-module:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: accounting-gateway
    ports:
      - "8000:8000"
    volumes:
      - shared-storage:/tmp/accounting_storage
    environment:
      - REDIS_URL=redis://redis-broker:6379/0
      - GEMINI_API_KEY=${GEMINI_API_KEY}
    depends_on:
      - redis-broker
    restart: unless-stopped
    networks:
      - accounting-network

  processing-worker:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: accounting-worker
    command: celery -A tasks worker --loglevel=info -c 4
    volumes:
      - shared-storage:/tmp/accounting_storage
    environment:
      - REDIS_URL=redis://redis-broker:6379/0
      - LOCAL_AI_URL=http://local-ai:8080/v1
      - GEMINI_API_KEY=${GEMINI_API_KEY}
    depends_on:
      - redis-broker
      - local-ai
    restart: unless-stopped
    networks:
      - accounting-network

volumes:
  shared-storage:

networks:
  accounting-network:
    driver: bridge
```

---

## 4. Referencyjne Prompty i Struktury Danych

### 4.1. Krok A: Prompt dla `llama-server` (Smart Masking)
**Zastosowanie**: Analiza wejścia (obraz/tekst) oraz cenzura RODO z zachowaniem kontekstu rynkowego.

```text
Jesteś lokalnym, bezpiecznym strażnikiem prywatności RODO w module księgowym.
Przeanalizuj przesłany obraz/tekst faktury, a następnie dokonaj pełnej anonimizacji danych wrażliwych.

Zasady zastępowania danych tokenami:
- Imiona, nazwiska, dane kontaktowe, telefony -> [OSOBA_X]
- Numery NIP, REGON, PESEL -> [NIP_X]
- Numery rachunków bankowych (IBAN) -> [KONTO_BANKOWE_X]
- Nazwy firm zastąp tokenem niosącym kontekst branżowy. Przykłady: [KONTRAHENT_BRANZA_PALIWOWA_1], [KONTRAHENT_BRANZA_TELEKOM_1], [KONTRAHENT_BRANZA_IT_1], [KONTRAHENT_BRANZA_BIUROWA_1]. Zawsze analizuj pozycje na fakturze, aby poprawnie sklasyfikować branżę!

Zwróć wynik wyłącznie jako czysty, poprawny obiekt JSON, wg struktury:
{
  "masked_text": "Treść faktury, w której wszystkie wrażliwe dane zastąpiono powyższymi tokenami...",
  "mapping_dictionary": {
    "[KONTRAHENT_BRANZA_PALIWOWA_1]": "Nazwa Oryginalna Sp. z o.o.",
    "[NIP_1]": "9998887766"
  }
}
```

### 4.2. Krok B: Prompt dla Google Gemini (Chmurowa Dekretacja)
**Zastosowanie**: Głębokie wnioskowanie podatkowe na bezpiecznym tekście.

```text
Jesteś zaawansowanym systemem eksperckim ds. polskiej rachunkowości. Przeanalizuj poniższy, zanonimizowany tekst dokumentu i przygotuj dekretację księgową kosztu. Kategoryzacja musi opierać się o kontekst tokenów branżowych (np. KONTRAHENT_BRANZA_PALIWOWA oznacza wydatek na paliwo/eksploatację). Zachowaj wszystkie tokeny w niezmienionej formie.

Wygeneruj strukturę JSON:
{
  "data_wystawienia": "YYYY-MM-DD",
  "sprzedawca_token": "[KONTRAHENT_BRANZA_PALIWOWA_1]",
  "sprzedawca_nip_token": "[NIP_1]",
  "pozycje_faktury": [
    {
      "opis": "Zakup paliwa do samochodu służbowego",
      "netto": 300.00,
      "vat_stawka": "23%",
      "vat_kwota": 69.00,
      "brutto": 369.00,
      "konto_wn": "401-02 (Zużycie paliwa)",
      "konto_ma": "210 (Rozrachunki z dostawcami)"
    }
  ],
  "podsumowanie": {
    "suma_netto": 300.00,
    "suma_vat": 69.00,
    "suma_brutto": 369.00
  },
  "sugerowany_kod_gtu": "GTU_02",
  "uzasadnienie_ksiegowe": "Koszt zakwalifikowany na podstawie profilu kontrahenta [KONTRAHENT_BRANZA_PALIWOWA_1] jako koszt zużycia materiałów i energii."
}
```

### 4.3. Krok D: Prompt dla `llama-server` (Reconciliation & Audit Loop)
**Zastosowanie**: Detekcja halucynacji, braków i błędów matematycznych.

```text
Jesteś Głównym Audytorem i Kontrolerem Jakości w zautomatyzowanym biurze rachunkowym.
Otrzymujesz dwa źródła danych:
1. OBRAZ/SKAN FAKTURY (Przesłany jako plik wejściowy Vision).
2. KANDYDACKI JSON (Wygenerowany przez zewnętrzny model, zawierający odtworzone dane).

Twoim krytycznym zadaniem jest przeprowadzenie audytu porównawczego (Reconciliation). Sprawdź:
1. Czy KAŻDA pozycja (towar/usługa) widoczna na obrazie faktury znajduje się w tablicy "pozycje_faktury" w JSON-ie? Jeśli czegoś brakuje – dopisz to, wyliczając poprawne konta księgowe na podstawie logiki innych pozycji.
2. Czy wartości netto, vat, brutto dla każdej pozycji oraz w podsumowaniu zgadzają się z dokumentem graficznym w 100%? Jeśli są rozbieżności matematyczne lub literówki – popraw je w JSON-ie zgodnie z obrazem.
3. Czy nagłówek dokumentu (Data, NIP sprzedawcy, NIP nabywcy) is kompletny i zgodny z obrazem?

Zwróć poprawiony, ostateczny i w 100% zweryfikowany dokument jako PURE JSON o dokładnie takiej samej strukturze jak Kandydacki JSON. Nie dodawaj żadnego komentarza tekstowego poza poprawnym obiektem JSON.
```

---

## 5. Szablony Implementacyjne (Target Blueprints)

### 5.1. Gateway API (`main.py` - FastAPI)

```python
from fastapi import FastAPI, UploadFile, File, status, HTTPException
from pydantic import BaseModel
from typing import Dict, Any
import uuid
import os
from celery import Celery

app = FastAPI(title="Accounting Hybrid Async Engine", version="3.0")
celery_app = Celery("tasks", broker=os.getenv("REDIS_URL"), backend=os.getenv("REDIS_URL"))

STORAGE_PATH = "/tmp/accounting_storage"
os.makedirs(STORAGE_PATH, exist_ok=True)

class TaskResponse(BaseModel):
    task_id: str
    status: str

@app.post("/v1/invoice/process", response_model=TaskResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_invoice(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename)[1]
    task_id = str(uuid.uuid4())
    file_name = f"{task_id}{ext}"
    target_destination = os.path.join(STORAGE_PATH, file_name)
    
    # Zapis asynchroniczny strumienia pliku na wolumen
    with open(target_destination, "wb") as buffer:
        buffer.write(await file.read())
        
    # Odpalenie asynchronicznego worker-pipeline w Celery
    celery_app.send_task("tasks.process_invoice_task", args=[task_id, target_destination])
    
    return TaskResponse(task_id=task_id, status="pending")

@app.get("/v1/invoice/status/{task_id}")
async def get_task_status(task_id: str):
    res = celery_app.AsyncResult(task_id)
    if res.state == "PENDING":
        return {"task_id": task_id, "status": "pending"}
    elif res.state == "SUCCESS":
        return {"task_id": task_id, "status": "completed", "data": res.result}
    elif res.state == "FAILURE":
        return {"task_id": task_id, "status": "failed", "error": str(res.info)}
    return {"task_id": task_id, "status": res.state.lower()}
```

### 5.2. Silnik Przetwarzania (`tasks.py` - Celery Worker)

```python
import os
import json
import base64
import re
from celery import Celery
import redis
import openai
import google.generativeai as gemini

celery_app = Celery("tasks", broker=os.getenv("REDIS_URL"), backend=os.getenv("REDIS_URL"))
redis_client = redis.Redis.from_url(os.getenv("REDIS_URL"))

# Konfiguracja klientów API
local_ai_client = openai.OpenAI(base_url=os.getenv("LOCAL_AI_URL"), api_key="not-needed")
gemini.configure(api_key=os.getenv("GEMINI_API_KEY"))

def encode_image_to_base64(file_path: str) -> str:
    with open(file_path, "rb") as img_file:
        return base64.b64encode(img_file.read()).decode("utf-8")

def execute_detokenization(cloud_json: dict, mapping_dict: dict) -> dict:
    json_str = json.dumps(cloud_json, ensure_ascii=False)
    # Iteracyjna podmiana tokenów na wartości rzeczywiste w bezpiecznej pamięci RAM
    for token, original_value in mapping_dict.items():
        json_str = json_str.replace(token, original_value)
    return json.loads(json_str)

@celery_app.task(name="tasks.process_invoice_task", bind=True, max_retries=3, default_retry_countdown=60)
def process_invoice_task(self, task_id: str, file_path: str):
    try:
        base64_image = encode_image_to_base64(file_path)
        image_url_data = f"data:image/jpeg;base64,{base64_image}"

        # ------------------------------------------------------------
        # KROK A: Smart Masking (Lokalna Gemma 4 e4b via llama-server)
        # ------------------------------------------------------------
        local_response = local_ai_client.chat.completions.create(
            model="gemma4-e4b",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": "PROMPT_KROK_A_Z_SEKCJI_4.1"},
                    {"type": "image_url", "image_url": {"url": image_url_data}}
                ]}
            ],
            response_format={"type": "json_object"}
        )
        masking_result = json.loads(local_response.choices[0].message.content)
        
        masked_text = masking_result["masked_text"]
        mapping_dictionary = masking_result["mapping_dictionary"]

        # Zapis słownika mapowania do Redis z TTL = 600 sekund (RODO Safety Barrier)
        redis_client.setex(f"mask:{task_id}", 600, json.dumps(mapping_dictionary))

        # ------------------------------------------------------------
        # KROK B: Zaawansowana Analiza Finansowa (Chmura: Gemini API)
        # ------------------------------------------------------------
        gemini_model = gemini.GenerativeModel("gemini-1.5-pro")
        cloud_response = gemini_model.generate_content(
            f"PROMPT_KROK_B_Z_SEKCJI_4.2

Tekst wejściowy:
{masked_text}",
            generation_config={"response_mime_type": "application/json"}
        )
        accounting_cloud_json = json.loads(cloud_response.text)

        # ------------------------------------------------------------
        # KROK C: Detokenizacja w Pamięci RAM
        # ------------------------------------------------------------
        candidate_json = execute_detokenization(accounting_cloud_json, mapping_dictionary)

        # ------------------------------------------------------------
        # KROK D: Pętla Samokontroli Quality Assurance (Lokalny Audyt)
        # ------------------------------------------------------------
        audit_response = local_ai_client.chat.completions.create(
            model="gemma4-e4b",
            messages=[
                {"role": "user", "content": [
                    {"type": "text", "text": f"PROMPT_KROK_D_Z_SEKCJI_4.3

Kandydacki JSON:
{json.dumps(candidate_json, ensure_ascii=False)}"},
                    {"type": "image_url", "image_url": {"url": image_url_data}}
                ]}
            ],
            response_format={"type": "json_object"}
        )
        final_verified_json = json.loads(audit_response.choices[0].message.content)

        return final_verified_json

    except Exception as exc:
        # Automatyczny mechanizm ponawiania zadań w przypadku błędów sieciowych API
        raise self.retry(exc=exc)
        
    finally:
        # ------------------------------------------------------------
        # RODO SAFETY GUARD: Bezwzględne czyszczenie zasobów wrażliwych
        # ------------------------------------------------------------
        redis_client.delete(f"mask:{task_id}")
        if os.path.exists(file_path):
            os.remove(file_path)
```

---

## 6. Standardy Operacyjne i Bezpieczeństwo (Enterprise Guidelines)

1.  **Zasada Izolacji Pamięci**: Słownik zawierający rzeczywiste nazwy firm i numery NIP nigdy nie może zostać zapisany w logach aplikacji (`stdout`/`stderr`). Wszelkie operacje debugowania (logging) muszą być wykonywane wyłącznie na zanonimizowanym obiekcie `masked_text`.
2.  **Obsługa Błędów Krytycznych (Fail-Safe)**: Jeśli Krok D (Pętla Weryfikacji) zwróci błąd struktury JSON lub przekroczy dopuszczalny czas odpowiedzi, system automatycznie flaguje zadanie statusem `REQUIRES_MANUAL_VERIFICATION`, zwracając do bazy danych wersję z Kroku C, oznaczając ją jako niezweryfikowaną.
3.  **Zarządzanie Pamięcią GPU (VRAM OOM Guard)**: Flaga `--n-gpu-layers 99` w połączeniu z kwantyzacją `Q4_K_M` gwarantuje, że model Gemma 4 e4b zajmie stałą alokację rzędu ok. ~4.5 - 5.5 GB VRAM. Zabrania się uruchamiania na tej samej karcie graficznej innych procesów mogących dynamicznie alokować pamięć, co mogłoby doprowadzić do przerwania działania kontenera `local-ai`.
