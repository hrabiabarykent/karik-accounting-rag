import os
import sys
import socket
import json
import requests

def check_socket(host="127.0.0.1", port=8088, timeout=3) -> bool:
    """Sprawdza, czy jakikolwiek proces nasłuchuje na wskazanym porcie (IPv4 / IPv6)."""
    for h in [host, "127.0.0.1", "localhost"]:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect((h, port))
            s.close()
            return True
        except Exception:
            pass
    return False


def main():
    print("=" * 80)
    print("  🔍 ŚCIEŻKA DIAGNOSTYCZNA SERWERA GEMMA SLM (LLAMA.CPP / PORT 8088)")
    print("=" * 80)

    # 1. SPRAWDZENIE PORTU 8088
    print("\n[TEST 1/3] Sprawdzanie otwarcia gniazda HTTP na port 8088...")
    is_port_open = check_socket("127.0.0.1", 8088)

    
    if is_port_open:
        print("  ✓ Port 8080 jest OTWARTY! Usługa nasłuchuje na porcie 8080.")
    else:
        print("  ❌ Port 8080 jest ZAMKNIĘTY (WinError 10061 - Odmowa połączenia).")
        print("     Oznacza to, że proces serwera Gemma SLM nie działa w tle.")

    # 2. SPRAWDZENIE KATALOGU MODELS
    print("\n[TEST 2/3] Sprawdzanie obecności pliku modelu GGUF na dysku...")
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    models_dir = os.path.join(base_dir, "models")
    
    if os.path.exists(models_dir):
        gguf_files = [f for f in os.listdir(models_dir) if f.endswith(".gguf")]
        if gguf_files:
            print(f"  ✓ Odnaleziono {len(gguf_files)} plików GGUF w katalogu ./models:")
            for gf in gguf_files:
                file_path = os.path.join(models_dir, gf)
                size_gb = os.path.getsize(file_path) / (1024 ** 3)
                print(f"    • {gf} ({size_gb:.2f} GB)")
        else:
            print(f"  ⚠️ Katalog {models_dir} istnieje, ale JEST PUSTY (brak plików .gguf).")
    else:
        print(f"  ⚠️ Brak katalogu {models_dir} na dysku!")

    # 3. TEST ZAPYTANIA HTTP REST
    print("\n[TEST 3/3] Wysyłanie zapytania próbnego HTTP do Gemma SLM...")
    if is_port_open:
        try:
            url = "http://127.0.0.1:8088/v1/chat/completions"


            payload = {
                "messages": [{"role": "user", "content": "Test połączenia Gemma SLM"}],
                "max_tokens": 10,
                "temperature": 0.0
            }
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code == 200:
                print("  ✓ SUKCES! Serwer Gemma SLM odpowiada na zapytania HTTP (Status 200 OK).")
            else:
                print(f"  ⚠️ Serwer odpowiedział statusem HTTP {resp.status_code}: {resp.text[:200]}")
        except Exception as err:
            print(f"  ❌ Błąd wysyłania żądania HTTP: {err}")
    else:
        print("  ⏭️ Pominięto test HTTP (port 8080 jest zamknięty).")

    # DIAGNOZA I ZALECENIA
    print("\n" + "=" * 80)
    print("  📋 REKOMENDOWANE KROKI NAPRAWCZE")
    print("=" * 80)

    if not is_port_open:
        print("""
KROK 1: Upewnij się, że usługa Docker Desktop jest uruchomiona na Windowsie.
KROK 2: Uruchom kontener z Gemmą wpisując w konsoli:

        docker-compose up -d local-ai

KROK 3: Sprawdź logi startowe kontenera, czy model prawidłowo załadował się na GPU CUDA:

        docker logs karik-local-ai

KROK 4: Ponownie uruchom aplikację Streamlit:

        streamlit run app.py
""")
    else:
        print("\n✓ Serwer Gemma SLM działa prawidłowo! Możesz odpalić Streamlit (`streamlit run app.py`).")

if __name__ == "__main__":
    main()
