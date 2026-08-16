import os
import sys
import logging
import requests

# 1. Poprawna konfiguracja loggera (konfigurowalny debugger)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("ELI_Sejm_Downloader")

# 2. Słownik oficjalnych pozycji najnowszych Tekstów Jednolitych z Dziennika Ustaw (DU)
ELI_ACTS = {
    "PIT": {
        "year": 2024, "pos": 226, 
        "title": "Ustawa o podatku dochodowym od osób fizycznych (Tekst Jednolity 2024)"
    },
    "CIT": {
        "year": 2023, "pos": 2805,  
        "title": "Ustawa o podatku dochodowym od osób prawnych (Tekst Jednolity 2023)"
    },
    "VAT": {
        "year": 2024, "pos": 361, 
        "title": "Ustawa o podatku od towarów i usług (Tekst Jednolity 2024 - zawiera Art. 86a)"
    },
    "Ordynacja_Podatkowa": {
        "year": 1997, "pos": 926, 
        "title": "Ordynacja podatkowa (Tekst Jednolity ISAP)"
    },

    "UoR_Rachunkowosc": {
        "year": 2023, "pos": 120, 
        "title": "Ustawa o rachunkowości (Tekst Jednolity 2023)"
    },
    "ZUS_System_Ubezpieczen": {
        "year": 2024, "pos": 497, 
        "title": "Ustawa o systemie ubezpieczeń społecznych (Tekst Jednolity 2024)"
    },
    "Prawo_Przedsiebiorcow": {
        "year": 2024, "pos": 236,
        "title": "Prawo przedsiębiorców (Tekst Jednolity 2024 - zawiera Ulgę na start w ZUS Art. 18)"
    }
}


BASE_API_URL = "https://api.sejm.gov.pl/eli/acts/DU"

def validate_download(response: requests.Response, expected_type: str, min_size_bytes: int = 15000) -> bool:
    """
    Weryfikuje, czy odpowiedź serwera zawiera prawidłowy plik aktu prawnego.
    """
    logger.debug("Rozpoczynanie walidacji odpowiedzi...")
    
    if response.status_code != 200:
        logger.error(f"Nieprawidłowy kod statusu HTTP: {response.status_code}")
        return False

    content_length = len(response.content)
    logger.debug(f"Pobrany rozmiar: {content_length / 1024:.2f} KB")
    if content_length < min_size_bytes:
        logger.error(f"Plik jest zbyt mały ({content_length} B < {min_size_bytes} B) – prawdopodobnie pobrano błąd HTTP.")
        return False

    content_type = response.headers.get("Content-Type", "")
    logger.debug(f"Content-Type serwera: {content_type}")

    if expected_type == "html":
        content_str = response.content.decode("utf-8", errors="ignore").lower()
        if "<html" not in content_str and "<div" not in content_str:
            logger.error("Treść HTML nie zawiera prawidłowej struktury znaczników.")
            return False
            
    elif expected_type == "pdf":
        if not response.content.startswith(b"%PDF"):
            logger.error("Nagłówek pliku nie zawiera sygnatury magicznej %PDF.")
            return False

    logger.debug("Walidacja zakończona sukcesem [OK].")
    return True

def get_latest_consolidated_url(year: int, pos: int, file_format: str = "html") -> str:
    """
    Odpytuje API Sejmu o metadane aktu i odnajduje najnowszy Tekst Jednolity (ujednolicony).
    """
    meta_url = f"{BASE_API_URL}/{year}/{pos}"
    headers = {"Accept": "application/json"}
    
    try:
        res = requests.get(meta_url, headers=headers, timeout=10)
        if res.status_code == 200:
            meta = res.json()
            texts = meta.get("texts", [])
            
            # Szukamy tekstu jednolitego (type == 'U' lub najnowszego tekstu ujednoliconego)
            unified_texts = [t for t in texts if t.get("type") in ("U", "T")]
            if unified_texts:
                latest_text = unified_texts[-1]  # Ostatni/najnowszy tekst jednolity
                u_year = latest_text.get("year", year)
                u_pos = latest_text.get("pos", pos)
                logger.info(f"Odnaleziono aktualny Tekst Jednolity: Dz.U. {u_year} poz. {u_pos}")
                return f"{BASE_API_URL}/{u_year}/{u_pos}/text.{file_format}"
    except Exception as e:
        logger.warning(f"Nie udało się odczytać najnowszego tekstu ujednoliconego: {e}")

    # Fallback na pierwotny adres aktu
    return f"{BASE_API_URL}/{year}/{pos}/text.{file_format}"

def download_all_eli(output_dir=None, file_format="html"):
    """
    Pobiera aktualne, ujednolicone akty prawne (Teksty Jednolite) z bazy Sejmu ISAP ELI API.
    """
    if not output_dir:
        output_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "pobrane_ustawy"))
    
    os.makedirs(output_dir, exist_ok=True)
    logger.info(f"Katalog docelowy: {os.path.abspath(output_dir)}")
    logger.info(f"Wybrany format: {file_format.upper()}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SejmAPIDownloader/2.0",
        "Accept": "text/html" if file_format == "html" else "application/pdf"
    }

    summary = {"ok": 0, "failed": 0}

    for name, info in ELI_ACTS.items():
        year, pos = info["year"], info["pos"]
        logger.info("=" * 65)
        logger.info(f"Przetwarzanie: {name}")
        logger.info(f"Tytuł: {info['title']}")

        # Dynamiczne odnajdywanie najnowszego Tekstu Jednolitego
        download_url = get_latest_consolidated_url(year, pos, file_format=file_format)
        file_path = os.path.join(output_dir, f"{name}.{file_format}")

        logger.info(f"Pobieranie treści aktu z oficjalnego API Sejmu ISAP: {download_url}")
        try:
            res = requests.get(download_url, headers=headers, timeout=45)

            if validate_download(res, expected_type=file_format):
                with open(file_path, "wb") as f:
                    f.write(res.content)
                logger.info(f"✓ Sukces! Zapisano oficjalny tekst z ISAP: {file_path} ({len(res.content) / 1024:.2f} KB)")
                summary["ok"] += 1
            else:
                logger.error(f"✗ Błąd walidacji pliku dla {name}.")
                summary["failed"] += 1

        except requests.RequestException as e:
            logger.error(f"✗ Błąd sieciowy podczas pobierania {name}: {e}")
            summary["failed"] += 1



    logger.info("=" * 65)
    logger.info(f"Podsumowanie: Pobrano pomyślnie {summary['ok']}/{len(ELI_ACTS)} uaktualnionych ustaw.")

if __name__ == "__main__":
    download_all_eli(file_format="html")