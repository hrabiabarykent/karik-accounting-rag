# Projekt Wolf: Inteligentny Asystent Podatkowo-Księgowy
## Skonteneryzowany Moduł RAG z Warstwą Izolacji RODO i Google Gemini 3.5

---

## 1. Opis Projektu i Cel Biznesowy

**Wolf** to system ekspercki klasy Enterprise stworzony z myślą o biurach rachunkowych, księgowych oraz doradcach podatkowych. Głównym celem systemu jest automatyzacja analizy problemów podatkowych oraz zapytań E-mail przybywających od klientów biura.

Zamiast ręcznego przeszukiwania ustaw, rozporządzeń i interpretacji KIS, księgowy wkleja treść maila lub zadaje pytanie w języku naturalnym. System w ułamku sekundy przeprowadza anonimizację danych osobowych (Zero-Trust RODO), przeszukuje polskie prawo podatkowe za pomocą hybrydowego silnika RAG i generuje ugruntowaną odpowiedź z cytatami przepisów przy użyciu modelu **Google Gemini 3.5**.

---

## 2. Kluczowe Filary Architektury

```
[ Mail od Klienta / Pytanie Księgowego ]
                   │
                   ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 1. WARSTWA IZOLACJI RODO (Zero-Trust Privacy Layer)                   │
│ - Presidio Engine (wzorce NIP, PESEL, IBAN + walidatory matematyczne)  │
│ - Smart Masking (zamiana imion/firm na tokeny branżowe w pii_sanitizer)│
│ - Bezpieczny słownik de-anonimizacji w pamięci RAM                    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Zanonimizowany tekst)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 2. HYBRYDOWY SILNIK RAG (Segment 1: Prawo Podatkowe)                   │
│ - Query Rewriter: Przekształcenie języka potocznego na język ustawowy  │
│ - Baza Wektorowa ChromaDB (Article-Level Chunking)                     │
│ - Dedykowany polski model embeddingów (sdadas/mmlu-polish-e5-base)     │
│ - Cross-Encoder Reranker (Score > 0.70 / Odsiewanie śmieci)           │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ (Wyselekcjonowane Artykuły)
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 3. SYNTEZA CHMUROWA (Google Gemini 3.5 API)                            │
│ - Context Caching (zbuforowane ustawy w API Google, zniżka -75%)       │
│ - Strict Grounding Prompt (zakaz wymyślania sygnatur i przepisów)      │
│ - Struktura wyjściowa JSON (treść odpowiedzi, artykuły, ocena ryzyka)  │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────────┐
│ 4. INTERFEJS UŻYTKOWNIKA (Streamlit MVP)                               │
│ - Prezentacja zanonimizowanej treści, cytatów z ustaw i odpowiedzi    │
│ - Hiperłącza do aktów prawnych + Przycisk kopiowania dla księgowego    │
└───────────────────────────────────┬────────────────────────────────────┘
```

---

## 3. Tech Stack MVP (Minimalny i Skuteczny)

| Warstwa | Technologia | Rola w Projekcie |
| :--- | :--- | :--- |
| **Backend API** | `FastAPI (Python 3.11+)` | Główny serwer i potok przetwarzania zapytania |
| **Anonimizacja PII** | `Microsoft Presidio + pii_sanitizer.py` | Wykrywanie i maskowanie danych wrażliwych RODO |
| **Baza Wektorowa** | `ChromaDB` | Przechowywanie zindeksowanych artykułów ustaw |
| **Embeddingi PL** | `sdadas/mmlu-polish-e5-base` | Dedykowany model semantyczny dla języka polskiego |
| **Reranker** | `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1` | Odsiewanie artykułów o niskiej trafności (< 0.70) |
| **Chmurowy LLM** | `Google Gemini 3.5 API` | Wnioskowanie prawne z wykorzystaniem Context Caching |
| **Frontend MVP** | `Streamlit` | Szybki, interaktywny panel dla księgowego |
| **Orkiestracja** | `Docker Compose` | Jednolita konteneryzacja środowiska |

---

## 4. Zakres MVP (Etap 1 - Cel Główny)

### A. Baza Wiedzy (Segment 1)
* **Pobranie i przetworzenie ustaw**:
  * Ustawa o podatku dochodowym od osób fizycznych (PIT)
  * Ustawa o podatku dochodowym od osób prawnych (CIT)
  * Ustawa o podatku od towarów i usług (VAT)
  * Ordynacja Podatkowa
* **Article-Level Chunking**: Każdy artykuł (wraz z ustępami i punktami) tworzy odrębny dokument z bogatymi metadanymi:
  ```json
  {
    "akt": "Ustawa o podatku dochodowym od osób fizycznych",
    "artykuł": "22",
    "rok": 2026,
    "status": "obowiązujący"
  }
  ```

### B. Prompt Strict Grounding (Absolutny zakaz halucynacji)
```text
Jesteś zaawansowanym asystentem podatkowym. Odpowiadaj TYLKO na podstawie dostarczonych w sekcji <KONTEKST> artykułów ustaw.
Zawsze podawaj dokładną sygnaturę artykułu (np. Art. 22 ust. 1 ustawy o PIT).
Jeśli w przesłanym kontekście brak jest jednoznacznej podstawy prawnej – napisz wprost:
"Brak jednoznacznej podstawy prawnej w dostarczonym kontekście".
```

### C. Widok w Streamlit
1. Pole tekstowe na treść zapytania / maila.
2. Podgląd zanonimizowanego tekstu (dla weryfikacji RODO).
3. Podgląd wyciągniętych przez RAG artykułów.
4. Ostateczna, przejrzysta odpowiedź ze wskaźnikiem pewności.

---

## 5. Wizja Rozwoju (Etap 2 - Wersja Docelowa v4)

1. **Segment 2 RAG (Dane Transakcyjne Klientów)**:
   * Dodanie bazy danych księgowych klientów z filtrowaniem `tenant_id` (multi-tenancy w Qdrant/pgvector).
2. **Local GPU Audit Loop**:
   * Użycie małego lokalnego modelu na `llama-server` (CUDA) do dodatkowego audytu krzyżowego odpowiedzi z Gemini.
3. **Dedykowana Integracja C# z Comarch ERP Optima**:
   * Stworzenie wtyczki w C# (.NET 8 / WPF) do menu Optimy z odczytem tabel MS SQL (`CDN.v_...`).
   * Stworzenie dodatku do Microsoft Outlooka (Outlook Add-in).

---

## 6. Stan gotowości i pierwsze kroki

Projekt jest gotowy do rozpoczęcia prac nad MVP. Posiadamy już w repozytorium sprawdzony moduł anonimizacji `pii_sanitizer.py` oraz skonfigurowane środowisko Docker.
