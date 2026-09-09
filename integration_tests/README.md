# Odizolowane testy PostgreSQL / Redis / Celery

Ten zestaw uruchamia prawdziwe procesy HTTP (Uvicorn) i Celery (`solo`, zgodne
z Windows). Nie korzysta z `tests/conftest.py`, bazy aplikacji, chmurowego LLM
ani Optimy. Wymaga wolnych portów 25432, 26379 i 28001.

```powershell
docker run -d --name karik-audit-pg --label karik.audit=isolated -e POSTGRES_DB=karik_audit_test -e POSTGRES_USER=audit -e POSTGRES_PASSWORD=audit-local-test -p 127.0.0.1:25432:5432 pgvector/pgvector:pg16
docker run -d --name karik-audit-redis --label karik.audit=isolated -p 127.0.0.1:26379:6379 redis:7.2-alpine
docker exec karik-audit-pg pg_isready -U audit -d karik_audit_test
$env:KARIK_RUN_INTEGRATION='1'
.\.venv\Scripts\python.exe -m pytest integration_tests/ -q --basetemp=.cache/integration_pytest
docker stop karik-audit-pg karik-audit-redis
```

Poczekaj na gotowość PostgreSQL przed uruchomieniem pytest. Istniejące kontenery
testowe można ponownie uruchomić przez `docker start` zamiast `docker run`.
Po zakończeniu prac można usunąć wyłącznie te dwa kontenery testowe.

Konfiguracja zestawu wymusza osobną bazę `karik_audit_test` na lokalnym porcie
25432, lokalnego brokera na 26379 i testowy sekret sesji. Nie resetuje tabel;
rekordy mają unikalne identyfikatory. Procesy API i workerów są zatrzymywane
w `finally`; logi pozostają w `.cache/integration_pytest`.

Sprawdzane scenariusze:

- inicjalizacja i ponowna migracja schematu PostgreSQL;
- równoległe odrzucenie dokumentu i rezerwacja eksportu;
- sześć równoległych prób wznowienia eksportu `FAILED`;
- czas od ostatniej publikacji, backoff i wyczerpanie limitu dostarczenia;
- HTTP upload → outbox/PostgreSQL → Redis → osobny worker → odczyt HTTP;
- zabicie workera podczas zadania, wznowienie i dokładnie jedna wersja wyniku;
- trwałe zakończenie próby po utracie pliku źródłowego.

Blokada odrzucenia: dokument z eksportem `IN_FLIGHT`, `UNKNOWN` lub `TRANSMITTED`
wymaga najpierw rozstrzygnięcia eksportu. Jeśli odrzucenie wygra transakcję,
rezerwacja eksportu jest odrzucana. Obie operacje blokują ten sam wiersz dokumentu.

Nie jest to test importu do Optimy, poprawności OCR ani konfiguracji prefork
Celery w Linuksie. Te obszary wymagają osobnych testów.
