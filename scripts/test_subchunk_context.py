import os
import sys
import logging

# Dodanie katalogu głównego projektu do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.parser import extract_articles_from_html

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    pit_html = os.path.join(base_dir, "data", "pobrane_ustawy", "PIT.html")

    if not os.path.exists(pit_html):
        print(f"Brak pliku {pit_html}. Najpierw uruchom data/pobierz_ustawy.py!")
        return

    print("=" * 80)
    print("  TESTOWANIE PODZIAŁU DŁUGICH ARTYKUŁÓW I ZACHOWANIA ZDANIA WSTĘPNEGO (PARENT CONTEXT)")
    print("=" * 80)

    articles = extract_articles_from_html(pit_html)
    
    # Szukamy wyekstrahowanego ustępu Art. 23 ust. 1 pkt 47a (limity samochodów)
    target_chunk = None
    for art in articles:
        if "23" in art["article_number"] and "47a" in art["article_number"]:
            target_chunk = art
            break

    if not target_chunk:
        for art in articles:
            if art["act_code"] == "PIT" and "23" in art["article_number"]:
                target_chunk = art
                break


    if not target_chunk:
        print("Nie odnaleziono podzielonego artykułu 23 w PIT.")
        return

    prompt_to_evaluate = f"""Jesteś audytorem prawnym i ekspertem RAG dla polskich ustaw podatkowych.
Przeanalizuj poniższy wyciągnięty z bazy fragment aktu prawnego pod kątem kompletności prawnej i spójności kontekstu.

Pytania sprawdzające dla LLM:
1. Czy wyciągnięty fragment zawiera czytelne zdanie wstępne (wprowadzenie intencji ustawodawcy - np. "Nie uważa się za koszty...")?
2. Czy na podstawie samego tego fragmentu księgowy/LLM wie bez wątpliwości, czy dany przepis to wyłączenie, czy przywilej?
3. Czy kontekst został zachowany w 100% bez gubienia sensu normy prawnej?

WYCIĄGNIĘTY FRAGMENT DLA BAZY RAG:
<WYCIĄG_Z_BAZY>
Tytuł w bazie: {target_chunk['full_title']}
Rozmiar bajtowy: {len(target_chunk['content'])} znaków

Treść fragmentu:
{target_chunk['content']}
</WYCIĄG_Z_BAZY>

Wydać ocenę w skali 1-10 i krótki komentarz, czy ten fragment jest w 100% samowystarczalny i kompletny prawnie.
"""

    print("\n📋 SKOPIUJ POWYŻSZY PROMPT I WKLEJ GO DO DOWOLNEGO LLM (np. ChatGPT / Gemini Online):")
    print("-" * 80)
    print(prompt_to_evaluate)
    print("-" * 80)

if __name__ == "__main__":
    main()
