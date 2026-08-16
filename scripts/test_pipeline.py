import os
import sys
import json
import logging

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


# Dodanie katalogu głównego do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.pipeline import build_prompt_and_context, run_rag_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    print("=" * 80)
    print("  KARIK RAG - TESTER INTERAKTYWNY (Z ANONIMIZACJĄ RODO I PRZYGOTOWANIEM PROMPTU)")
    print("=" * 80)

    sample_email = (
        "Dzień dobry,\n"
        "Nazywam się Jan Kowalski z firmy Auto-Fix Sp. z o.o., NIP 5213456789.\n"
        "Chciałbym zapytać o koszty amortyzacji samochodu osobowego o wartości 220 000 PLN netto,\n"
        "wprowadzonego do ewidencji środków trwałych w lipcu 2026 r.\n"
        "Jaki jest limit wartości początkowej i jak odliczyć 50% VAT od paliwa?"
    )

    print("\n[PRZESŁANA WIADOMOŚĆ OD KLIENTA]:")
    print("-" * 60)
    print(sample_email)
    print("-" * 60)

    print("\n🚀 Krok 1 & 2: Anonimizacja RODO + Pobranie Kontekstu z pgvector (CUDA)...")
    data = build_prompt_and_context(sample_email)

    print("\n" + "=" * 80)
    print("  GOTOWY PROMPT DO SKOPIOWANIA DO GEMINI ONLINE CHAT")
    print("=" * 80)
    print(data["prompt_to_copy"])
    print("=" * 80)

    print("\n📋 SKOPIUJ POWYŻSZY PROMPT I WKLEJ GO W INTERFEJSIE GEMINI ONLINE CHAT.")
    print("Gdy otrzymasz odpowiedź z Chatu Gemini, wklej ją poniżej (lub po prostu naciśnij Enter, aby zakończyć):")

    print("\nWklej odpowiedź z Gemini Online Chat (naciśnij Ctrl+Z i Enter w nowej linii na Windowsie po wklejeniu):")
    try:
        user_pasted_lines = []
        while True:
            try:
                line = input()
                user_pasted_lines.append(line)
            except EOFError:
                break
        user_pasted_response = "\n".join(user_pasted_lines).strip()
    except Exception:
        user_pasted_response = ""

    if user_pasted_response:
        print("\n🚀 Krok 4: Detokenizacja RODO odpowiedzi z Gemini...")
        final_result = run_rag_pipeline(user_query=sample_email, manual_llm_response=user_pasted_response)

        print("\n" + "=" * 80)
        print("  OSTATECZNA DETOKENIZOWANA ODPOWIEDŹ DLA KSIĘGOWEGO")
        print("=" * 80)
        print(f"Poziom ryzyka podatkowego: {final_result['risk_level']}")
        print(f"Uzasadnienie ryzyka:        {final_result['risk_justification']}")
        print("\nTreść odpowiedzi po przywróceniu danych klienta:")
        print("-" * 60)
        print(final_result['answer_text'])
        print("-" * 60)

        print("\nLista odnośników plikowych:")
        for art in final_result['cited_articles']:
            print(f"  • {art['full_title']} -> Link: {art['file_link']}")
    else:
        print("\nℹ️ Nie wklejono odpowiedzi. Prompt został poprawnie wygenerowany i jest gotowy do użycia.")

if __name__ == "__main__":
    main()
