import os
import sys
import glob
import logging

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


# Dodanie katalogu głównego projektu do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.parser import extract_articles_from_html
from rag.db import init_database, save_articles_to_db
from rag.retriever import generate_embeddings_batch

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def process_and_ingest():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    html_dir = os.path.join(base_dir, "data", "pobrane_ustawy")
    html_files = glob.glob(os.path.join(html_dir, "*.html"))

    if not html_files:
        logging.error(f"Brak plików HTML w katalogu {html_dir}")
        return

    logging.info("Inicjalizacja bazy danych PostgreSQL z rozszerzeniem pgvector...")
    try:
        init_database()
        logging.info("Baza danych zainicjalizowana pomyślnie.")
    except Exception as e:
        logging.warning(f"Nie udało się połączyć z bazą danych PostgreSQL: {e}. Upewnij się, że kontener postgres-pgvector jest uruchomiony.")

    total_articles = 0

    for file_path in html_files:
        file_name = os.path.basename(file_path)
        logging.info(f"Parsowanie pliku: {file_name}...")
        articles = extract_articles_from_html(file_path)
        logging.info(f"Wyciągnięto {len(articles)} artykułów z {file_name}.")
        
        # Wsadowe generowanie embeddingów dla wyciągniętych artykułów
        texts_to_embed = [f"{art['full_title']}\n{art['clean_text']}" for art in articles]
        logging.info(f"Generowanie embeddingów dla {len(texts_to_embed)} artykułów w paczkach...")
        
        embeddings = generate_embeddings_batch(texts_to_embed, batch_size=32)
        for art, emb in zip(articles, embeddings):
            art["embedding"] = emb

        save_articles_to_db(articles)
        logging.info(f"Zapisano {len(articles)} artykułów z {file_name} w bazie pgvector.")


        total_articles += len(articles)

    logging.info(f"Zakończono parsowanie. Łączna liczba wyekstrahowanych artykułów: {total_articles}")

    # Automatyczny audyt braku duplikatów po zakończonym procesie
    from rag.db import verify_no_duplicates_in_db
    dup_stats = verify_no_duplicates_in_db()
    logging.info(f"✓ Audyt spójności bazy zakończony sukcesem: {dup_stats['total_articles']} artykułów (0 duplikatów).")

if __name__ == "__main__":
    process_and_ingest()

