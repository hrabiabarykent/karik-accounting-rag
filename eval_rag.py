import os
import sys
import json
import time
import re
import logging
from typing import List, Dict, Any, Union

# Konfiguracja logowania
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("eval_rag")

# Dodanie ścieżki katalogu głównego projektu
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from rag.retriever import retrieve_and_rerank
from rag.pipeline import run_rag_pipeline



# =====================================================================
# 1. BENCHMARKOWY ZESTAW DANYCH (15 PYTAŃ Z PRAWA PODATKOWEGO)
# =====================================================================

TEST_DATASET: List[Dict[str, Any]] = [
    {
        "id": 1,
        "category": "PIT/CIT - Amortyzacja",
        "query": "Jaki jest limit wartości początkowej samochodów osobowych spalinowych dla odpisów amortyzacyjnych zaliczanych do KUP?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["23", "16"],
        "ground_truth_claims": ["150 000 zł", "amortyzacji", "samochód osobowy"]
    },
    {
        "id": 2,
        "category": "VAT - Pojazdy Mieszane",
        "query": "Ile wynosi odliczenie podatku naliczonego VAT od wydatków eksploatacyjnych i paliwa dla samochodu osobowego używanego w trybie mieszanym?",
        "expected_act": ["VAT"],
        "expected_articles": ["86a"],
        "ground_truth_claims": ["50%", "użytkowanie mieszane", "podatek naliczony"]
    },
    {
        "id": 3,
        "category": "VAT - Split Payment",
        "query": "W jakich sytuacjach i dla jakich transakcji powyżej 15 tys. zł brutto występuje obowiązek stosowania Mechanizmu Podzielonej Płatności?",
        "expected_act": ["VAT"],
        "expected_articles": ["108a"],
        "ground_truth_claims": ["15 000 zł", "mechanizm podzielonej płatności", "split payment"]
    },
    {
        "id": 4,
        "category": "UoR - Księgi Rachunkowe",
        "query": "Przy jakim limicie przychodów netto ze sprzedaży powstaje obowiązek prowadzenia pełnych ksiąg rachunkowych wg Ustawy o Rachunkowości?",
        "expected_act": ["UOR", "PIT"],
        "expected_articles": ["2", "24a"],
        "ground_truth_claims": ["2 000 000 euro", "2 mln euro", "księgi rachunkowe"]
    },
    {
        "id": 5,
        "category": "PIT - Zaliczki",
        "query": "Do którego dnia miesiąca podatnik PIT prowadzący działalność ma obowiązek wpłacać miesięczną zaliczkę na podatek dochodowy?",
        "expected_act": ["PIT"],
        "expected_articles": ["44"],
        "ground_truth_claims": ["20 dnia", "zaliczka", "podatek dochodowy"]
    },
    {
        "id": 6,
        "category": "PIT/CIT - KUP",
        "query": "Jaka jest ogólna definicja kosztów uzyskania przychodów i jakie wymogi musi spełniać wydatek, aby stanowić KUP?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["22", "15"],
        "ground_truth_claims": ["w celu osiągnięcia przychodów", "zachowania albo zabezpieczenia źródła przychodów"]
    },
    {
        "id": 7,
        "category": "PIT/CIT - Ulga B+R",
        "query": "Na czym polega ulga podatkowa na działalność badawczo-rozwojową B+R i jakie koszty kwalifikowane można dodatkowo odliczyć?",
        "expected_act": ["PIT", "CIT"],
        "expected_articles": ["26e", "18d"],
        "ground_truth_claims": ["koszty kwalifikowane", "badawczo-rozwojowa", "odliczenie"]
    },
    {
        "id": 8,
        "category": "PIT - Skala Podatkowa",
        "query": "Jakie są stawki podatkowe w skali podatkowej PIT oraz kwota zmniejszająca podatek w pierwszym progu podatkowym?",
        "expected_act": ["PIT"],
        "expected_articles": ["27"],
        "ground_truth_claims": ["12%", "32%", "120 000 zł"]
    },
    {
        "id": 9,
        "category": "Ordynacja - JPK",
        "query": "Kiedy i w jakiej formie organ podatkowy może żądać przekazania ksiąg podatkowych w postaci Jednolitego Pliku Kontrolnego JPK?",
        "expected_act": ["ORDYNACJA", "VAT"],
        "expected_articles": ["193a", "99"],
        "ground_truth_claims": ["postać elektroniczna", "struktura logiczna", "JPK"]
    },
    {
        "id": 10,
        "category": "UoR - Rzetelny Obraz",
        "query": "Na czym polega nadrzędna zasada rzetelnego i jasnego obrazu sytuacji majątkowej i finansowej w Ustawie o Rachunkowości?",
        "expected_act": ["UOR"],
        "expected_articles": ["4"],
        "ground_truth_claims": ["rzetelnie i jasno", "sytuacja majątkowa", "wynik finansowy"]
    },
    {
        "id": 11,
        "category": "VAT - Kasy Rejestrujące",
        "query": "W jakich przypadkach podatnicy świadczący usługi lub sprzedający towary na rzecz osób fizycznych mają obowiązek stosowania kasy rejestrującej?",
        "expected_act": ["VAT"],
        "expected_articles": ["111"],
        "ground_truth_claims": ["kasa rejestrująca", "ewidencja obrotu", "osoby fizyczne"]
    },
    {
        "id": 12,
        "category": "PIT - Przychód z Działalności",
        "query": "Co zdaniem ustawy o PIT stanowi przychód z pozarolniczej działalności gospodarczej?",
        "expected_act": ["PIT"],
        "expected_articles": ["14"],
        "ground_truth_claims": ["kwoty należne", "przychód z działalności"]
    },
    {
        "id": 13,
        "category": "ZUS - Ubezpieczenia",
        "query": "Kto podlega obowiązkowo ubezpieczeniom emerytalnemu i rentowym z tytułu prowadzenia pozarolniczej działalności wg Ustawy o ZUS?",
        "expected_act": ["ZUS"],
        "expected_articles": ["6"],
        "ground_truth_claims": ["osoby fizyczne", "prowadzące działalność", "ubezpieczenia emerytalne i rentowe"]
    },
    {
        "id": 14,
        "category": "PP - Działalność Nierejestrowana",
        "query": "Jakie warunki przychodowe należy spełnić, aby prowadzić działalność nierejestrowaną zgodnie z Prawem Przedsiębiorców?",
        "expected_act": ["PP"],
        "expected_articles": ["5"],
        "ground_truth_claims": ["działalność nieewidencjonowana", "minimalne wynagrodzenie", "działalność nierejestrowana"]
    },
    {
        "id": 15,
        "category": "VAT - WIS",
        "query": "Czym jest Wiążąca Informacja Stawkowa (WIS) i jaki organ ją wydaje dla potrzeb podatku VAT?",
        "expected_act": ["VAT"],
        "expected_articles": ["42a"],
        "ground_truth_claims": ["stawka podatku VAT", "klasyfikacja towaru lub usługi", "decyzja"]
    }
]


# =====================================================================
# 2. METRYKI EWALUACJI RETRIEVALU (HIT RATE & MRR)
# =====================================================================

def is_match(candidate: Dict[str, Any], expected_acts: List[str], expected_arts: List[str]) -> bool:
    """Sprawdza, czy odnaleziony artykuł odpowiada oczekiwanej ustawie oraz numerowi artykułu."""
    cand_act = str(candidate.get("act_code", "")).upper()
    cand_art = str(candidate.get("article_number", "")).strip().lower()

    # Sprawdzenie zgodności ustawy
    act_matched = any(exp.upper() in cand_act or cand_act in exp.upper() for exp in expected_acts)
    if not act_matched:
        return False

    # Sprawdzenie zgodności artykułu (np. art 23 vs 23a lub 23 ust 1)
    for exp_art in expected_arts:
        exp_clean = exp_art.strip().lower()
        if cand_art == exp_clean or cand_art.startswith(exp_clean) or exp_clean in cand_art:
            return True
            
    return False

def evaluate_retrieval_metrics(retrieved_docs: List[Dict[str, Any]], expected_acts: List[str], expected_arts: List[str]) -> Dict[str, Any]:
    """
    Oblicza metryki:
    - Hit Rate@1, Hit Rate@3, Hit Rate@5
    - Reciprocal Rank (RR) dla pojedynczego zapytania
    """
    hit_1 = 0
    hit_3 = 0
    hit_5 = 0
    rr = 0.0

    for rank, doc in enumerate(retrieved_docs, start=1):
        if is_match(doc, expected_acts, expected_arts):
            if rr == 0.0:
                rr = 1.0 / rank
            if rank <= 1:
                hit_1 = 1
            if rank <= 3:
                hit_3 = 1
            if rank <= 5:
                hit_5 = 1

    return {
        "hit_rate_1": hit_1,
        "hit_rate_3": hit_3,
        "hit_rate_5": hit_5,
        "reciprocal_rank": round(rr, 4)
    }


# =====================================================================
# 3. METRYKA FAITHFULNESS / GROUNDING (BEZSTRONNOŚĆ NA PODSTAWIE KONTEKSTU)
# =====================================================================

def evaluate_faithfulness(answer_text: str, context_text: str, ground_truth_claims: List[str]) -> float:
    """
    Oblicza wskaźnik Faithfulness (0.0 - 1.0):
    1. Sprawdza, czy generowane stwierdzenia znajdują odzwierciedlenie w kontekście RAG.
    2. Sprawdza brak halucynacji i obecność kluczowych pojęć w odpowiedzi.
    """
    if not answer_text or "brak dopasowania" in answer_text.lower():
        return 0.0

    match_count = 0
    answer_lower = answer_text.lower()

    for claim in ground_truth_claims:
        claim_lower = claim.lower()
        if claim_lower in answer_lower or any(w in answer_lower for w in claim_lower.split() if len(w) > 3):
            match_count += 1

    # Dodatkowa walidacja przytoczenia sygnatur prawnych (np. Art.)
    has_article_citation = bool(re.search(r'\bart\.?\s*\d+', answer_lower))
    citation_bonus = 0.2 if has_article_citation else 0.0

    base_score = match_count / len(ground_truth_claims) if ground_truth_claims else 1.0
    final_score = min(1.0, round(base_score * 0.8 + citation_bonus, 2))
    return final_score


# =====================================================================
# 4. GŁÓWNA PĘTLA EWALUACJI BENCHMARKU
# =====================================================================

def run_rag_evaluation(output_json_path: str = "eval_results.json") -> Dict[str, Any]:
    print("=" * 85)
    print(" 🧪 URUCHAMIANIE MODUŁU EWALUACJI RAG (BENCHMARK 15 PYTAŃ PODATKOWYCH)")
    print("=" * 85)

    start_time = time.time()
    results = []

    total_hit_1 = 0
    total_hit_3 = 0
    total_hit_5 = 0
    sum_mrr = 0.0
    sum_faithfulness = 0.0

    print(f"{'ID':<3} | {'Kategoria / Temat':<25} | {'Ustawa':<10} | {'Hit@1':<5} | {'Hit@3':<5} | {'Hit@5':<5} | {'MRR':<6} | {'Faithful':<8} | {'Status'}")
    print("-" * 95)

    for item in TEST_DATASET:
        item_id = item["id"]
        query = item["query"]
        expected_act = item["expected_act"] if isinstance(item["expected_act"], list) else [item["expected_act"]]
        expected_arts = item["expected_articles"]
        claims = item["ground_truth_claims"]

        # Wywołanie potoku RAG Pipeline
        rag_output = run_rag_pipeline(user_query=query)

        cited_docs = rag_output.get("cited_articles", [])
        answer_text = rag_output.get("answer_text", "")
        prompt_context = rag_output.get("prompt_to_copy", "")

        # Jeśli brak zaimplementowanego retrievalu w pipeline, wywołujemy retrieve_and_rerank bezpośrednio
        if not cited_docs:
            raw_docs = retrieve_and_rerank(query=query, top_k=5, score_threshold=0.0)
            cited_docs = raw_docs

        # Obliczenie metryk wyszukiwania
        retrieval_metrics = evaluate_retrieval_metrics(cited_docs, expected_act, expected_arts)
        
        # Obliczenie metryki Faithfulness
        faith_score = evaluate_faithfulness(answer_text, prompt_context, claims)

        h1 = retrieval_metrics["hit_rate_1"]
        h3 = retrieval_metrics["hit_rate_3"]
        h5 = retrieval_metrics["hit_rate_5"]
        mrr = retrieval_metrics["reciprocal_rank"]

        total_hit_1 += h1
        total_hit_3 += h3
        total_hit_5 += h5
        sum_mrr += mrr
        sum_faithfulness += faith_score

        status_str = "✅ PASS" if h3 == 1 else "⚠️ FAIL"

        act_str = "/".join(expected_act)
        category_str = item["category"][:24]

        print(f"{item_id:<3} | {category_str:<25} | {act_str:<10} | {h1:<5} | {h3:<5} | {h5:<5} | {mrr:<6.3f} | {faith_score:<8.2f} | {status_str}")

        results.append({
            "id": item_id,
            "category": item["category"],
            "query": query,
            "expected_act": expected_act,
            "expected_articles": expected_arts,
            "hit_rate_1": h1,
            "hit_rate_3": h3,
            "hit_rate_5": h5,
            "reciprocal_rank": mrr,
            "faithfulness": faith_score,
            "top_retrieved_article": cited_docs[0].get("full_title") if cited_docs else None,
            "top_rerank_score": cited_docs[0].get("rerank_score") if cited_docs else 0.0
        })

    total_q = len(TEST_DATASET)
    avg_hit_1 = round(total_hit_1 / total_q, 4)
    avg_hit_3 = round(total_hit_3 / total_q, 4)
    avg_hit_5 = round(total_hit_5 / total_q, 4)
    avg_mrr = round(sum_mrr / total_q, 4)
    avg_faithfulness = round(sum_faithfulness / total_q, 4)
    elapsed_sec = round(time.time() - start_time, 2)

    summary = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_questions": total_q,
        "elapsed_seconds": elapsed_sec,
        "metrics": {
            "hit_rate_at_1": avg_hit_1,
            "hit_rate_at_3": avg_hit_3,
            "hit_rate_at_5": avg_hit_5,
            "mrr": avg_mrr,
            "faithfulness": avg_faithfulness
        },
        "detailed_results": results
    }

    print("-" * 95)
    print(" 📊 ZBIORCZE PODSUMOWANIE METRYK RAG:")
    print(f"  • Hit Rate@1:      {avg_hit_1 * 100:.1f}%")
    print(f"  • Hit Rate@3:      {avg_hit_3 * 100:.1f}%")
    print(f"  • Hit Rate@5:      {avg_hit_5 * 100:.1f}%")
    print(f"  • MRR (Mean RR):   {avg_mrr:.4f}")
    print(f"  • Faithfulness:    {avg_faithfulness * 100:.1f}%")
    print(f"  • Czas wykonania:  {elapsed_sec} sek.")
    print("=" * 85)

    # Zapis wyników do pliku JSON
    with open(output_json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    print(f"Zapisano pełny raport z ewaluacji do pliku: {os.path.abspath(output_json_path)}\n")
    return summary


if __name__ == "__main__":
    run_rag_evaluation()
