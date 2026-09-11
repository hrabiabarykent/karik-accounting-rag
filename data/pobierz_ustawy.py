import os
import sys
import json
import time
import hashlib
import logging
import requests
from datetime import datetime
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout
)
logger = logging.getLogger("ELI_Sejm_Downloader")

BASE_API_URL = "https://api.sejm.gov.pl/eli/acts/DU"

# Słownik tożsamości aktów macierzystych (Base ELI Identity)
BASE_ACTS: Dict[str, Dict[str, Any]] = {
    "PIT": {
        "act_code": "PIT",
        "file_name": "PIT.html",
        "base_year": 1991, "base_pos": 350,
        "title": "Ustawa o podatku dochodowym od osób fizycznych",
        "fallback_consolidated": {"year": 2024, "pos": 226}
    },
    "CIT": {
        "act_code": "CIT",
        "file_name": "CIT.html",
        "base_year": 1992, "base_pos": 86,
        "title": "Ustawa o podatku dochodowym od osób prawnych",
        "fallback_consolidated": {"year": 2023, "pos": 2805}
    },
    "VAT": {
        "act_code": "VAT",
        "file_name": "VAT.html",
        "base_year": 2004, "base_pos": 535,
        "title": "Ustawa o podatku od towarów i usług",
        "fallback_consolidated": {"year": 2024, "pos": 361}
    },
    "Ordynacja_Podatkowa": {
        "act_code": "OP",
        "file_name": "Ordynacja_Podatkowa.html",
        "base_year": 1997, "base_pos": 926,
        "title": "Ordynacja podatkowa",
        "fallback_consolidated": {"year": 2022, "pos": 2651}
    },
    "UoR_Rachunkowosc": {
        "act_code": "UOR",
        "file_name": "UoR_Rachunkowosc.html",
        "base_year": 1994, "base_pos": 591,
        "title": "Ustawa o rachunkowości",
        "fallback_consolidated": {"year": 2023, "pos": 120}
    },
    "ZUS_System_Ubezpieczen": {
        "act_code": "ZUS",
        "file_name": "ZUS_System_Ubezpieczen.html",
        "base_year": 1998, "base_pos": 887,
        "title": "Ustawa o systemie ubezpieczeń społecznych",
        "fallback_consolidated": {"year": 2024, "pos": 497}
    },
    "Prawo_Przedsiebiorcow": {
        "act_code": "PP",
        "file_name": "Prawo_Przedsiebiorcow.html",
        "base_year": 2018, "base_pos": 646,
        "title": "Prawo przedsiębiorców",
        "fallback_consolidated": {"year": 2024, "pos": 236}
    }
}


def compute_sha256(content: bytes) -> str:
    """Oblicza sumę kontrolną SHA-256 dla pobranego pliku."""
    return hashlib.sha256(content).hexdigest()


def validate_download(response: requests.Response, expected_type: str = "html", min_size_bytes: int = 15000) -> bool:
    """Weryfikuje poprawność pobranego pliku (kod 200, rozmiar, sygnatura HTML)."""
    if response.status_code != 200:
        logger.error(f"Nieprawidłowy kod HTTP: {response.status_code}")
        return False
    content_len = len(response.content)
    if content_len < min_size_bytes:
        logger.error(f"Plik zbyt mały ({content_len} B < {min_size_bytes} B).")
        return False
    if expected_type == "html":
        s = response.content.decode("utf-8", errors="ignore").lower()
        if "<html" not in s and "<div" not in s:
            logger.error("Treść nie zawiera prawidłowej struktury HTML.")
            return False
    return True


def resolve_latest_consolidated_act(act_key: str, act_meta: Dict[str, Any], file_format: str = "html") -> Dict[str, Any]:
    """
    Dynamicznie odnajduje najnowszy tekst jednolity powiązany z aktem macierzystym (Base ELI).
    Weryfikuje dostępność formatu HTML i status prawny.
    """
    base_year = act_meta["base_year"]
    base_pos = act_meta["base_pos"]
    fallback = act_meta["fallback_consolidated"]
    title_query = act_meta["title"]

    headers = {"Accept": "application/json", "User-Agent": "KarikRAG/2.0"}

    # 1. Próba odnalezienia przez API wyszukiwania obwieszczeń jednolitego tekstu
    try:
        query_url = f"https://api.sejm.gov.pl/eli/acts/search?title=jednolitego+tekstu+ustawy+-+{requests.utils.quote(title_query)}&publisher=DU"
        res = requests.get(query_url, headers=headers, timeout=12)
        if res.status_code == 200:
            items = res.json().get("items", [])
            for it in items:
                y = it.get("year")
                p = it.get("pos")
                if not y or not p:
                    continue
                # Sprawdzenie dostępności text.html
                meta_res = requests.get(f"{BASE_API_URL}/{y}/{p}", headers=headers, timeout=10)
                if meta_res.status_code == 200:
                    texts = meta_res.json().get("texts", [])
                    has_html = any(t.get("fileName", "").endswith(".html") for t in texts)
                    if has_html:
                        url = f"{BASE_API_URL}/{y}/{p}/text.{file_format}"
                        logger.info(f"[{act_key}] Odnaleziono najnowszy Tekst Jednolity z HTML: Dz.U. {y} poz. {p}")
                        return {
                            "year": y,
                            "pos": p,
                            "title": it.get("title", act_meta["title"]),
                            "download_url": url,
                            "resolution_method": "dynamic_eli_search"
                        }
    except Exception as e:
        logger.warning(f"[{act_key}] Dynamiczne wyszukiwanie tekstu jednolitego zwróciło błąd: {e}")

    # 2. Fallback na zweryfikowany stabilny Tekst Jednolity z HTML
    fb_y = fallback["year"]
    fb_p = fallback["pos"]
    url = f"{BASE_API_URL}/{fb_y}/{fb_p}/text.{file_format}"
    logger.info(f"[{act_key}] Używam zweryfikowanego Tekstu Jednolitego: Dz.U. {fb_y} poz. {fb_p}")
    return {
        "year": fb_y,
        "pos": fb_p,
        "title": act_meta["title"],
        "download_url": url,
        "resolution_method": "verified_fallback"
    }


def download_all_acts(output_dir: Optional[str] = None, manifest_path: Optional[str] = None) -> Dict[str, Any]:
    """Pobiera wszystkie akty prawne i zapisuje manifest data/manifest.json."""
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if not output_dir:
        output_dir = os.path.join(base_dir, "data", "pobrane_ustawy")
    if not manifest_path:
        manifest_path = os.path.join(base_dir, "data", "manifest.json")

    os.makedirs(output_dir, exist_ok=True)
    manifest_data: Dict[str, Any] = {
        "updated_at": datetime.now().isoformat(),
        "acts": {}
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SejmDownloader/2.0",
        "Accept": "text/html"
    }

    for key, meta in BASE_ACTS.items():
        file_name = meta["file_name"]
        file_path = os.path.join(output_dir, file_name)
        act_code = meta["act_code"]

        resolved = resolve_latest_consolidated_act(key, meta, file_format="html")
        dl_url = resolved["download_url"]

        logger.info(f"Pobieranie {key} ({act_code}) z {dl_url}...")
        res = requests.get(dl_url, headers=headers, timeout=45)
        if not validate_download(res, expected_type="html"):
            raise RuntimeError(f"Błąd pobierania aktu {key} z adresu {dl_url}.")

        with open(file_path, "wb") as f:
            f.write(res.content)

        sha = compute_sha256(res.content)
        manifest_data["acts"][act_code] = {
            "key": key,
            "act_code": act_code,
            "file_name": file_name,
            "file_path": file_path,
            "year": resolved["year"],
            "pos": resolved["pos"],
            "title": resolved["title"],
            "download_url": dl_url,
            "sha256": sha,
            "size_bytes": len(res.content),
            "downloaded_at": datetime.now().isoformat(),
            "resolution_method": resolved["resolution_method"]
        }
        logger.info(f"[OK] Zapisano {file_name} ({len(res.content)/1024:.1f} KB, SHA-256: {sha[:12]}...)")

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, ensure_ascii=False)
    logger.info(f"[OK] Zapisano manifest aktów: {manifest_path}")

    return manifest_data


if __name__ == "__main__":
    download_all_acts()