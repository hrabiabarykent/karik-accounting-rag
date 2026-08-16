import sys
import os
import json
import logging

# Ensure root directory is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from pii_sanitizer import PresidioInvoiceSanitizer, extract_pdf_text, tolerant_detokenize

# Silence unnecessary logs for clean CLI testing
logging.basicConfig(level=logging.WARNING)

DEFAULT_SAMPLE_TEXT = """
FAKTURA VAT Nr 102/2026
Data wystawienia: 2026-07-20

Sprzedawca:
Polski Koncern Naftowy ORLEN Spółka Akcyjna
ul. Chemików 7, 09-411 Płock
NIP: 7740001454
Nr konta bankowego: PL61109010140000071219812874

Nabywca:
Jan Kowalski Consulting
ul. Marszałkowska 10/12, 00-001 Warszawa
NIP: 5250008057
E-mail kontaktowy: jan.kowalski@example.com
Telefon: +48 600 100 200

Pozycje na fakturze:
1. Zakup paliwa Diesel VERVA - 250.00 PLN (VAT 23%)
2. Płyn do spryskiwaczy - 30.00 PLN (VAT 23%)

Suma brutto: 280.00 PLN
"""

def main():
    print("=" * 70)
    print("      LOKALNY TESTER ANONIMIZACJI I DETOKENIZACJI (BEZ GEMINI)")
    print("=" * 70)

    # 1. Odczyt tekstu wejściowego
    if len(sys.argv) > 1:
        target_path_or_text = sys.argv[1]
        if os.path.exists(target_path_or_text):
            ext = os.path.splitext(target_path_or_text)[1].lower()
            if ext == ".pdf":
                print(f"\n[1/4] Odczytywanie pliku PDF: {target_path_or_text}...")
                raw_text = extract_pdf_text(target_path_or_text)
                if not raw_text:
                    print("ERROR: Brak tekstu w PDF (prawdopodobnie skan/obrazek).")
                    sys.exit(1)
            else:
                with open(target_path_or_text, "r", encoding="utf-8") as f:
                    raw_text = f.read()
        else:
            raw_text = target_path_or_text
    else:
        print("\nNie podano ścieżki do pliku ani tekstu. Używam domyślnej faktury testowej.")
        raw_text = DEFAULT_SAMPLE_TEXT

    print("\n--- [DOKUMENT WEJŚCIOWY] ---")
    print(raw_text.strip())

    # 2. Krok A: Anonimizacja Presidio + Gemma SLM
    llama_url = os.getenv("LOCAL_AI_URL", "http://localhost:8080/v1")
    if not llama_url.endswith("/chat/completions"):
        llama_url = f"{llama_url.rstrip('/')}/chat/completions"

    print(f"\n[2/4] Uruchamianie Kroku A (Presidio + Gemma SLM na {llama_url})...")
    
    try:
        sanitizer = PresidioInvoiceSanitizer(llama_cpp_url=llama_url)
        anonymized_text, mapping_dict = sanitizer.sanitize(raw_text)
    except Exception as e:
        print(f"\n[BŁĄD KROKU A]: {e}")
        print("Upewnij się, że lokalny serwer llama.cpp (gemma4-e4b) jest uruchomiony!")
        sys.exit(1)

    print("\n--- [ZANONIMIZOWANY TEKST - GOTOWY DLA CHMURY] ---")
    print(anonymized_text.strip())

    print("\n--- [SŁOWNIK MAPOWANIA (ZAPISYWANY W REDIS)] ---")
    print(json.dumps(mapping_dict, indent=2, ensure_ascii=False))

    # 3. Symulacja odpowiedzi Gemini (Krok B)
    print("\n[3/4] Symulowanie obiektu JSON z chmury (Krok B)...")
    simulated_cloud_json = {
        "data_wystawienia": "2026-07-20",
        "sprzedawca": "<COMPANY_NAME_1>",
        "sprzedawca_nip": "<PL_NIP_1>",
        "sprzedawca_iban": "<PL_IBAN_1>",
        "nabywca": "<COMPANY_NAME_2>",
        "nabywca_nip": "<PL_NIP_2>",
        "kontakt_email": "<EMAIL_1>",
        "kontakt_telefon": "<TELEFON_1>",
        "pozycje": [
            {"opis": "Zakup paliwa Diesel VERVA", "brutto": 250.00},
            {"opis": "Płyn do spryskiwaczy", "brutto": 30.00}
        ],
        "suma_brutto": 280.00
    }

    # 4. Krok C: Tolerancyjna Detokenizacja w RAM
    print("\n[4/4] Uruchamianie Kroku C (Tolerancyjna Detokenizacja w RAM)...")
    detokenized_json, has_leftovers = tolerant_detokenize(simulated_cloud_json, mapping_dict)

    print("\n--- [ZDETOKENIZOWANY OBIEKT WYNIKOWY] ---")
    print(json.dumps(detokenized_json, indent=2, ensure_ascii=False))

    print("\n" + "=" * 70)
    print("                       WERYFIKACJA WYNIKU")
    print("=" * 70)
    if has_leftovers:
        print("[!] STATUS: REQUIRES_MANUAL_VERIFICATION (Wykryto niepodmienione tokeny!)")
    else:
        print("[OK] STATUS: VERIFIED (Wszystkie dane RODO przywrócone poprawnie)")
    print("=" * 70)

if __name__ == "__main__":
    main()
