import os
import sys
import logging

# Dodanie katalogu głównego do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.retriever import retrieve_and_rerank

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    print("=" * 70)
    print("  KARIK RAG - TESTER WYSZUKIWANIA HYBRYDOWEGO PGVECTOR + CROSS-ENCODER")
    print("=" * 70)

    sample_queries = [
        "Amortyzacja samochodu osobowego o wartości powyżej 150 tysięcy złotych",
        "Odliczenie 50% VAT od wydatków na paliwo do auta firmowego",
        "Ulga na start w ZUS dla nowo zarejestrowanej jednoosobowej działalności",
        "Stawki CIT dla małych podatników 9 procent"
    ]

    print("\nWybierz lub wpisz własne pytanie:")
    for idx, q in enumerate(sample_queries, 1):
        print(f"  [{idx}] {q}")
    print("  [0] Wpisz własne pytanie w konsoli")

    choice = input("\nTwoj wybór (0-4) [domyślnie 1]: ").strip() or "1"

    if choice == "0":
        user_query = input("Wpisz pytanie podatkowo-księgowe: ").strip()
    elif choice in ["1", "2", "3", "4"]:
        user_query = sample_queries[int(choice) - 1]
    else:
        user_query = choice

    print(f"\n[QUERY] Szukam w bazie pgvector dla: '{user_query}'...\n")

    results = retrieve_and_rerank(user_query, top_k=5, score_threshold=0.50)

    if not results:
        print("❌ Brak pasujących artykułów prawnych (Cross-Encoder score < 0.50).")
        return

    print(f"✅ Znaleziono {len(results)} najbardziej istotnych artykułów prawnych:\n")

    for i, res in enumerate(results, 1):
        print(f"--- [WYNIK {i}] ---")
        print(f"Akt prawny:   {res.get('act_title')} ({res.get('act_code')})")
        print(f"Artykuł:      Art. {res.get('article_number')}")
        if res.get('chapter'):
            print(f"Rozdział:     {res.get('chapter')}")
        print(f"Trafność:     Rerank Score = {res.get('rerank_score')} (RRF Score = {round(res.get('rrf_score', 0), 4)})")
        print("Skrót treści:")
        snippet = res.get('content', '')[:350].replace('\n', ' ')
        print(f"  \"{snippet}...\"\n")

if __name__ == "__main__":
    main()
