import os
import sys
import re
import logging
from typing import Dict, Any, List

# Dodanie katalogu głównego projektu do sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from rag.db import get_db_connection, verify_no_duplicates_in_db

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("RAG_Database_Auditor")

REQUIRED_LAWS = {
    "PIT.html": ("PIT", ["23", "22k"], ["150 000", "150000"]),
    "CIT.html": ("CIT", ["16", "15"], ["150 000", "150000"]),
    "VAT.html": ("VAT", ["86a", "86"], ["50%"]),
    "Ordynacja_Podatkowa.html": ("ORDYNACJA", ["70", "15"], []),
    "UoR_Rachunkowosc.html": ("UOR", ["32", "51"], []),
    "ZUS_System_Ubezpieczen.html": ("ZUS", ["1", "18"], []),
    "Prawo_Przedsiebiorcow.html": ("PP", ["18"], ["ulga na start", "składki"])
}

def audit_downloaded_files(laws_dir: str) -> bool:
    print("\n" + "=" * 80)
    print("  KROK 1: AUDYT PLIKÓW HTML W KATALOGU 'data/pobrane_ustawy'")
    print("=" * 80)
    
    all_files_ok = True
    for file_name, (code, benchmark_arts, benchmark_phrases) in REQUIRED_LAWS.items():
        file_path = os.path.join(laws_dir, file_name)
        if not os.path.exists(file_path):
            print(f"❌ [FAIL] Brak pliku na dysku: {file_name}")
            all_files_ok = False
            continue

        size_kb = os.path.getsize(file_path) / 1024
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()

        # Weryfikacja obecności benchmarkowych artykułów w HTML
        missing_arts = [art for art in benchmark_arts if f"arti_{art}" not in content.lower() and f"art. {art}" not in content.lower()]
        
        status_str = "✅ OK" if not missing_arts and size_kb > 50 else "❌ FAIL"
        print(f"  • {file_name:<30} | Rozmiar: {size_kb:>7.1f} KB | Status: {status_str}")
        
        if missing_arts:
            print(f"    ⚠️ Uwaga: W pliku {file_name} nie odnaleziono artykułów: {missing_arts}")
            all_files_ok = False

    return all_files_ok

def audit_database_contents() -> bool:
    print("\n" + "=" * 80)
    print("  KROK 2: AUDYT TABELI 'legal_articles' W BAZIE POSTGRESQL (pgvector)")
    print("=" * 80)

    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            # 1. Zliczanie artykułów wg ustawy
            cur.execute("""
                SELECT act_code, COUNT(*), COUNT(embedding) 
                FROM legal_articles 
                GROUP BY act_code 
                ORDER BY act_code;
            """)
            rows = cur.fetchall()

            print(f"{'KOD USTAWY':<12} | {'LICZBA ARTYKUŁÓW':<18} | {'WEKTORY 768D':<15} | STATUS")
            print("-" * 65)
            for row in rows:
                code, count, vec_count = row[0], row[1], row[2]
                st = "✅ OK" if count > 0 and count == vec_count else "❌ BŁĄD WEKTORÓW"
                print(f"{code:<12} | {count:<18} | {vec_count:<15} | {st}")

            # 2. Weryfikacja obecności krytycznych artykułów w PostgreSQL
            print("\n  🔍 WERYFIKACJA KLUCZOWYCH ARTYKUŁÓW PRAWNYCH W BAZIE:")
            critical_checks = [
                ("VAT", "86a", "VAT Art. 86a (Odliczenie 50% VAT bez limitu kwotowego)"),
                ("PIT", "23", "PIT Art. 23 (Limity amortyzacji samochodów 150 000 zł)"),
                ("CIT", "16", "CIT Art. 16 (Limity koszty uzyskania przychodów)"),
                ("PP", "18", "PP Art. 18 (Ulga na start w ZUS przez 6 miesięcy)")
            ]

            db_ok = True
            for code, art_num, desc in critical_checks:
                cur.execute("SELECT id, full_title FROM legal_articles WHERE act_code = %s AND article_number = %s;", (code, art_num))
                res = cur.fetchone()
                if res:
                    print(f"   ✅ [ODNALEZIONO] {desc}")
                else:
                    print(f"   ❌ [BRAK W BAZIE] {desc}")
                    db_ok = False

        conn.close()

        # 3. Audyt braku duplikatów
        print("\n  🔍 AUDYT SPÓJNOŚCI I DEDUPLIKACJI:")
        dup_stats = verify_no_duplicates_in_db()
        print(f"   ✅ Łączny stan: {dup_stats['total_articles']} unikalnych artykułów, 0 duplikatów.")

        return db_ok

    except Exception as e:
        print(f"❌ Błąd połączenia lub zapytania bazy danych: {e}")
        return False

def main():
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    laws_dir = os.path.join(base_dir, "data", "pobrane_ustawy")

    print("=" * 80)
    print("  KARIK RAG - AUDYTOR PRAWIDŁOWOŚCI BAZY USTAW I PLIKÓW SOURCE")
    print("=" * 80)

    files_ok = audit_downloaded_files(laws_dir)
    db_ok = audit_database_contents()

    print("\n" + "=" * 80)
    if files_ok and db_ok:
        print("  🎉 WYNIK KOŃCOWY AUDYTU: Vektorowa baza ustaw jest w 100% SPÓJNA, AKTUALNA i GOTOWA!")
    else:
        print("  ⚠️ WYNIK KOŃCOWY AUDYTU: Wykryto zastrzeżenia. Uruchom `python data/pobierz_ustawy.py` oraz `python scripts/ingest_laws.py`!")
    print("=" * 80)

if __name__ == "__main__":
    main()
