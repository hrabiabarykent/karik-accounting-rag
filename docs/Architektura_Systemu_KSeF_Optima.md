# Architektura Systemu Przetwarzania Dokumentów Księgowych (KSeF / Comarch Optima)

Dokumentacja architektoniczna produkcyjnego systemu automatycznej ekstrakcji, walidacji i kategoryzacji faktur z wykorzystaniem modeli hybrydowych (BM25 + Dense RAG), deterministycznej walidacji matematycznej oraz bezpiecznych guardraili finansowych.

---

## 1. High-Level Architecture Flow

```
[KSeF / PDF Ingestion]
          │
          ▼
[1. Idempotency & State Manager] ──► (Duplicate / Missing Base? ──► Queue: WAITING_FOR_ORIGINAL)
          │
          ▼
[2. Hybrid Extraction & Routing] ──► (Dense + BM25 + Re-ranker)
          │   ├── Low Complexity  ──► Tier 1: Small LLM / Parser
          │   └── High Complexity ──► Tier 2: Reasoning LLM
          ▼
[3. Zero-Token Math Validator] ──► (Fail? ──► AI_Review_Reason = "MATH_ERROR" ──► MANUAL_REVIEW)
          │
          ▼
[4. Decision Gate & Rule Engine]
          ├── Whitelist OR Confidence >= 0.98 ──► [AUTO_APPROVED] ──► [5. ERP Integration]
          └── Low Confidence / Risk Policy    ──► [MANUAL_REVIEW] ──► [Human Feedback Loop]
                                                              │
                                                   (Explicit Rule Save)
                                                              │
                                                              ▼
                                                   [Rule Engine (Expiring)]
```

---

## 2. Ingestion & Idempotency Layer

* **Idempotentność i Deduplikacja**  
  Unikalny hash generowany z parametrów biznesowych:  
  `Hash = SHA256(NIP_Sprzedawcy + Nr_Faktury + Nr_KSeF)`.  
  Zapytania z istniejącym hashem są odrzucane na poziomie API przed uruchomieniem pipeline'u AI.

* **Maszyna Stanów (State Machine)**
  * `RECEIVED` — Pobrano dokument ze źródła (KSeF / Skan).
  * `WAITING_FOR_ORIGINAL` — Faktura korygująca pobrana przed fakturą pierwotną (brak `NrKSeFFaKorygowanej` w bazie ERP).
  * `VALIDATED` — Dokument po udanej walidacji deterministycznej.
  * `AUTO_APPROVED` — Przekazany do automatycznego księgowania w ERP.
  * `MANUAL_REVIEW` — Przekierowany do kolejki weryfikacji ręcznej z podaniem powodu (`AI_Review_Reason`).

---

## 3. Ekstrakcja Hybrydowa i Routing Modeli

* **Wyszukiwanie Kontekstowe (Hybrid RAG)**
  * **Sparse (BM25)** — Bezpośrednie, dokładne dopasowywanie ciągów znaków (NIP, kody GTU, konta bankowe, numery artykułów).
  * **Dense Embedding** — Semantyczne dopasowywanie złożonych opisów pozycji/usług do planu kont i MPK.
  * **Cross-Encoder Re-ranker** — Ostateczne sortowanie kontekstów pod kątem historii księgowań danego kontrahenta.

* **Routing Kosztowo-Opóźnieniowy (Model Routing)**
  * **Tier 1 (Prosty / Tani model)** — Standardowe faktury z KSeF XML, stałe koszty abonamentowe, znani dostawcy z Whitelist.
  * **Tier 2 (Model Wnioskujący / Reasoning)** — Faktury zagraniczne, nietypowe zakupy, ocena ryzyka NKUP, wielopozycyjne korekty KSeF.

---

## 4. Deterministyczny Walidator Matematyczny (Zero-Token Check)

Osobny moduł wykonany w języku deterministycznym (Python/Rust) uruchamiany **przed** wygenerowaniem docelowej struktury dla ERP. Zapobiega halucynacjom groszowym LLM.

```python
def validate_invoice_math(invoice_data: Invoice) -> ValidationResult:
    # 1. Walidacja pozycji
    for line in invoice_data.lines:
        calc_gross = round(line.net + line.vat, 2)
        if calc_gross != round(line.gross, 2):
            return ValidationResult(valid=False, reason="LINE_MATH_MISMATCH")
            
    # 2. Sumowanie nagłówka
    total_net = sum(line.net for line in invoice_data.lines)
    total_vat = sum(line.vat for line in invoice_data.lines)
    
    if round(total_net + total_vat, 2) != round(invoice_data.header.gross, 2):
        return ValidationResult(valid=False, reason="HEADER_MATH_MISMATCH")
        
    # 3. Weryfikacja wartości ujemnych
    if not invoice_data.is_correction:
        if any(line.net < 0 for line in invoice_data.lines):
            return ValidationResult(valid=False, reason="UNEXPECTED_NEGATIVE_VALUE")
            
    return ValidationResult(valid=True, reason="OK")
```

---

## 5. Decision Gate & Hard Rules Engine

* **Zasady Auto-Approval**
  * Score >= 0.98 **LUB** Dostawca znajduje się na Whitelist (stałe media, SaaS, leasing, telekomunikacja).
  * **ORAZ** Zero-Token Check == OK.
  * **ORAZ** Brak zastrzeżeń / flagi ryzyka NKUP.

* **Pętla Zwrotna bez Zatruwania Reguł (Anti-Rule Poisoning)**
  * Ręczna zmiana księgowego **nie** tworzy automatycznie stałej reguły systemowej.
  * Tworzenie reguły wymaga kliknięcia przycisku „Zapisz jako stałą regułę dla NIP X”.
  * **Ważność reguł**: Każda wprowadzona reguła otrzymuje znacznik czasowy oraz domyślny czas wygasnięcia (`expires_at` = 6 lub 12 miesięcy).

---

## 6. Integracja z ERP (Comarch Optima)

Pełna separacja danych generowanych przez AI w dedykowanych atrybutach nagłówka dokumentu:

| Atrybut ERP       | Typ    | Przykładowa wartość          | Opis                                      |
|-------------------|--------|------------------------------|-------------------------------------------|
| `AI_Status`       | String | AUTO / MANUAL / REJECTED     | Status przetworzenia dokumentu            |
| `AI_Confidence`   | Float  | 0.985                        | Wynik pewności modelu                     |
| `AI_Review_Reason`| String | MATH_ERROR, NO_BASE_INV, NKUP_RISK | Czytelny kod powodu odrzucenia     |
| `AI_Rule_ID`      | UUID   | e3b0c442-888c-48a3-...       | Identyfikator reguły użytej do automatyzacji |
| `AI_Prompt_Ver`   | String | v2.4.1                       | Wersja promptu / mapowania w systemie     |

---

## 7. MLOps, Observability & Safety Controls

* **Automatyczny Kill-Switch (Wykrywanie Driftu)**
  * Monitorowanie wskaźnika akceptacji (Precision) w oknie kroczącym ostatnich 20 dokumentów per dostawca.
  * Jeśli spadek poprawności wyniesie < 95%, system automatycznie wyłącza tryb `AUTO_APPROVED` dla tego dostawcy i generuje alert dla administratora.

* **Golden Dataset (Testy Regresyjne)**
  * Utrzymywanie zbioru co najmniej 50–100 skrajnych dokumentów testowych (korekty, błędy zaokrągleń, zagraniczne waluty, nietypowe stawki VAT).
  * Każda zmiana promptu lub edycja kodu w CI/CD wymaga przejścia 100% testów regresyjnych.

* **Wersjonowanie Promptów i Reguł**
  * Wszystkie szablony promptów i twarde reguły są wersjonowane (Git / Tabela DB) z możliwością powrotu do poprzedniej wersji za pomocą jednego kliknięcia (One-click rollback).

---

*Dokument zaktualizowany: 2026-08-18*  
*Poziom dojrzałości: Production-grade*
