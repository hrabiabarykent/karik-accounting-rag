#!/usr/bin/env bash

echo "====================================================================="
echo "   AUTONOMICZNY STARTER SYSTEMU KARIK RAG - LINUX / MACOS 1-CLICK"
echo "====================================================================="
echo ""

# 1. Sprawdzanie środowiska Python
if ! command -v python3 &> /dev/null; then
    echo "[BŁĄD KRYTYCZNY] Python 3 nie jest zainstalowany na tym komputerze!"
    echo "Zainstaluj Python 3.10+ i dodaj go do PATH."
    exit 1
fi

# 2. Inicjalizacja środowiska wirtualnego .venv
if [ ! -f ".venv/bin/activate" ]; then
    echo "[1/6] Tworzenie nowego środowiska wirtualnego .venv..."
    python3 -m venv .venv
fi

echo "[2/6] Aktywacja środowiska wirtualnego..."
source .venv/bin/activate

# 3. Weryfikacja i instalacja zależności Python
if ! python3 -c "import streamlit, presidio_analyzer, sentence_transformers" &> /dev/null; then
    echo "[3/6] Instalowanie wymaganych pakietów Python..."
    pip install -r requirements.txt
else
    echo "[3/6] Pakiety Python są już zainstalowane."
fi

# 4. Pobieranie polskiego modelu SpaCy dla RODO
if ! python3 -c "import spacy; spacy.load('pl_core_news_lg')" &> /dev/null; then
    echo "[4/6] Pobieranie polskiego modelu językowego SpaCy pl_core_news_lg dla RODO..."
    python3 -m spacy download pl_core_news_lg
else
    echo "[4/6] Model SpaCy pl_core_news_lg jest już pobrany."
fi

# 5. Uruchamianie kontenerów Docker
echo "[5/6] Sprawdzanie i uruchamianie kontenerów Docker..."
if command -v docker-compose &> /dev/null; then
    docker-compose up -d
elif command -v docker &> /dev/null; then
    docker compose up -d
else
    echo "[OSTRZEŻENIE] Docker / docker-compose nie jest zainstalowany lub uruchomiony."
fi

# 6. Synchronizacja statycznych aktów prawnych
echo "[6/6] Synchronizacja i przestylizowanie plików ustaw..."
python3 scripts/sync_static_laws.py &> /dev/null

# 7. Uruchomienie serwera dokumentów prawnych na porcie 8085
echo "Uruchamianie lokalnego serwera dokumentów - Port 8085..."
python3 -m http.server 8085 --directory static/pobrane_ustawy &> /dev/null &

# 8. Uruchomienie aplikacji Karik RAG
echo "Uruchamianie aplikacji Karik RAG w Streamlit - Port 8501..."
streamlit run app.py &

echo ""
echo "====================================================================="
echo "   SYSTEM KARIK RAG WYSTARTOWAŁ POMYŚLNIE!"
echo "   Aplikacja otwarta pod adresem: http://localhost:8501"
echo "====================================================================="
