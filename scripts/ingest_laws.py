import os
import sys
import glob
import logging

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"


# Dodanie katalogu głównego projektu do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

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

    all_articles = []
    for file_path in html_files:
        file_name = os.path.basename(file_path)
        logging.info(f"Parsowanie pliku: {file_name}...")
        articles = extract_articles_from_html(file_path)
        logging.info(f"Wyciągnięto {len(articles)} artykułów z {file_name}.")
        
        # Wsadowe generowanie embeddingów dla wyciągniętych artykułów
        texts_to_embed = [f"{art['full_title']}\n{art['clean_text']}" for art in articles]
        logging.info(f"Generowanie embeddingów dla {len(texts_to_embed)} artykułów w paczkach...")
        
        embeddings = generate_embeddings_batch(texts_to_embed, batch_size=64)
        for art, emb in zip(articles, embeddings):
            art["embedding"] = emb
        logging.info(f"[OK] Wygenerowano {len(embeddings)} wektorów dla {file_name}.")

        all_articles.extend(articles)

    manifest_path = os.path.join(base_dir, "data", "manifest.json")
    manifest_checksum = None
    if os.path.exists(manifest_path):
        import hashlib
        with open(manifest_path, "rb") as mf:
            manifest_checksum = hashlib.sha256(mf.read()).hexdigest()

    logging.info(f"Zakończono parsowanie. Łącznie zebrano {len(all_articles)} artykułów ze wszystkich ustaw.")
    logging.info("Rozpoczynanie atomowego ingestu ze stagingiem w bazie PostgreSQL pgvector...")

    from rag.db import ingest_articles_staged
    result = ingest_articles_staged(
        all_articles,
        source_file="data/manifest.json",
        source_checksum=manifest_checksum,
        validate=True
    )
    logging.info(f"[OK] Atomowy ingest zakończony sukcesem: wersje {result.get('act_versions')}, zaindeksowano {result['articles_count']} artykułów.")

    # Automatyczny audyt braku duplikatów po zakończonym procesie
    from rag.db import verify_no_duplicates_in_db
    dup_stats = verify_no_duplicates_in_db()
    logging.info(f"[OK] Audyt spójności bazy zakończony sukcesem: {dup_stats['total_articles']} artykułów (0 duplikatów).")

if __name__ == "__main__":
    process_and_ingest()

