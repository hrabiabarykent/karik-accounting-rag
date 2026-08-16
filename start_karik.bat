@echo off
echo =====================================================================
echo    AUTONOMICZNY STARTER SYSTEMU KARIK RAG - WINDOWS 1-CLICK
echo =====================================================================
echo.

:: 1. Sprawdzanie srodowiska Python
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [BLAD KRYTYCZNY] Python nie jest zainstalowany na tym komputerze!
    echo Zainstaluj Python 3.10+ i zaznacz Add Python to PATH.
    pause
    exit /b 1
)

:: 2. Inicjalizacja srodowiska wirtualnego .venv
if not exist ".venv\Scripts\activate.bat" (
    echo [1/6] Tworzenie nowego srodowiska wirtualnego .venv...
    python -m venv .venv
)

echo [2/6] Aktywacja srodowiska wirtualnego...
call .venv\Scripts\activate.bat

:: 3. Weryfikacja i instalacja zaleznosci Python z widocznym postepem
python -c "import streamlit, presidio_analyzer, sentence_transformers" > nul 2>&1
if %errorlevel% neq 0 (
    echo [3/6] Instalowanie wymaganych pakietow Python...
    pip install -r requirements.txt
) else (
    echo [3/6] Pakiety Python sa juz zainstalowane.
)

:: 4. Pobieranie polskiego modelu Spacy dla RODO
python -c "import spacy; spacy.load('pl_core_news_lg')" > nul 2>&1
if %errorlevel% neq 0 (
    echo [4/6] Pobieranie polskiego modelu jezykowego Spacy pl_core_news_lg dla RODO...
    python -m spacy download pl_core_news_lg
) else (
    echo [4/6] Model SpaCy pl_core_news_lg jest juz pobrany.
)

:: 5. Uruchamianie kontenerow Docker
echo [5/6] Sprawdzanie i uruchamianie kontenerow Docker...
docker-compose up -d
if %errorlevel% neq 0 (
    echo [OSTRZEZENIE] Upewnij sie, ze Docker Desktop jest wlaczony na tym komputerze.
)

:: 6. Synchronizacja statycznych aktow prawnych z kotwicami HTML
echo [6/6] Synchronizacja i przestylizowanie plikow ustaw...
python scripts/sync_static_laws.py > nul 2>&1

:: 7. Uruchomienie serwera dokumentow prawnych na porcie 8085
echo Uruchamianie lokalnego serwera dokumentow - Port 8085...
start /b python -m http.server 8085 --directory static/pobrane_ustawy > nul 2>&1

:: 8. Uruchomienie aplikacji Karik RAG w przegladarce
echo Uruchamianie aplikacji Karik RAG w Streamlit - Port 8501...
start "" streamlit run app.py

echo.
echo =====================================================================
echo    SYSTEM KARIK RAG WYSTARTOWAL POMYSLNIE!
echo    Aplikacja otworzy sie automatycznie pod adresem: http://localhost:8501
echo =====================================================================
