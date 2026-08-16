import os
import sys
import logging

# Dodanie katalogu głównego do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.query_rewriter import rewrite_query

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# Zestaw testowy pytań księgowo-podatkowych z różnych dziedzin prawa
TEST_SUITE = [
    {
        "id": 1,
        "category": "PIT / CIT / VAT - Samochód w firmie",
        "query": "Chcę kupić auto spalinowe za 220 000 zł netto na firmę. Jak rozliczyć limit amortyzacji oraz 50% VAT od paliwa i serwisu?",
        "expected_keywords": ["art 23 PIT", "art 16 CIT", "limit 150000", "art 86a VAT", "50% VAT"]
    },
    {
        "id": 2,
        "category": "ZUS / Prawo Przedsiębiorców - Nowa działalność",
        "query": "Zakładam jednoosobową działalność gospodarczą. Jak działa Ulga na start bez składek społecznych przez pierwsze 6 miesięcy?",
        "expected_keywords": ["art 18 Prawo przedsiębiorców", "ulga na start", "składki ZUS"]
    },
    {
        "id": 3,
        "category": "CIT - Preferencyjna stawka 9%",
        "query": "Nasza spółka z o.o. miała przychody 1,2 mln EUR w zeszłym roku. Czy możemy płacić mały CIT 9% w tym roku?",
        "expected_keywords": ["art 19 CIT", "mały podatnik", "stawka 9 procent", "limit przychodów"]
    },
    {
        "id": 4,
        "category": "VAT - Noclegi i gastronomia",
        "query": "Czy biuro rachunkowe może odliczyć VAT z faktury za hotel i restaurację z udziałem kontrahenta w trakcie delegacji?",
        "expected_keywords": ["art 88 VAT", "usługi noclegowe", "gastronomiczne", "wyłączenie odliczenia"]
    },
    {
        "id": 5,
        "category": "Ordynacja Podatkowa - Przedawnienie",
        "query": "Po ilu latach przedawnia się zobowiązanie podatkowe w PIT za rok 2020 i jakie zdarzenia zawieszają bieg przedawnienia?",
        "expected_keywords": ["art 70 Ordynacja podatkowa", "przedawnienie zobowiązania", "okres 5 lat"]
    },
    {
        "id": 6,
        "category": "Ustawa o Rachunkowości - Amortyzacja bilansowa",
        "query": "Jak ustala się plan amortyzacji i stawki odpisów dla używanej maszyny produkcyjnej metodą liniową i degresywną?",
        "expected_keywords": ["art 32 UoR", "plan amortyzacji", "metoda liniowa", "degresywna"]
    }
]

def run_test_suite():
    print("=" * 85)
    print("  ZESTAW TESTOWY DLA MODUŁU QUERY REWRITER (Gemma SLM)")
    print("=" * 85)
    print(f"Liczba przypadek testowych w pakiecie: {len(TEST_SUITE)}\n")

    passed_count = 0

    for test in TEST_SUITE:
        print("-" * 85)
        print(f"TEST #{test['id']} [{test['category']}]")
        print(f"Pytanie klienta: \"{test['query']}\"")
        
        try:
            rewritten_output = rewrite_query(test['query'])
            print(f"Wygenerowane pojęcia pod RAG:\n  ➔ \"{rewritten_output}\"")
            print("Oczekiwane kluczowe nawiązania:", ", ".join(test['expected_keywords']))
            passed_count += 1
        except Exception as e:
            print(f"❌ BŁĄD TESTU #{test['id']}: {e}")

    print("\n" + "=" * 85)
    print(f" PODSUMOWANIE PAKIETU TESTOWEGO: Zakończono {passed_count}/{len(TEST_SUITE)} testów.")
    print("=" * 85)

if __name__ == "__main__":
    run_test_suite()
