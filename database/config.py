import os
import re
import socket
from dataclasses import dataclass
from typing import Optional, Dict, Any
from urllib.parse import urlsplit, parse_qs, unquote

from database.exceptions import DatabaseConfigurationError


_ENV_LOADED = False
ALLOWED_QUERY_PARAMS = {"sslmode", "connect_timeout", "application_name"}


def mask_password_in_url(url: str) -> str:
    """Maskuje hasło w adresie URL połączenia z bazą danych (np. postgresql://user:***@host:5432/db)."""
    if not url:
        return ""
    # Bezpieczne zastąpienie hasła w części poświadczeń URL
    return re.sub(r"://([^:@]+):([^@]+)@", r"://\1:***@", url)


def load_env_file(dotenv_path: Optional[str] = None, force_reload: bool = False) -> None:
    """
    Bezpieczne ładowanie zmiennych środowiskowych z pliku .env.
    Działa w każdym środowisku bez konieczności instalowania zewnętrznej biblioteki python-dotenv.
    """
    global _ENV_LOADED
    if _ENV_LOADED and not force_reload:
        return

    if dotenv_path is None:
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        dotenv_path = os.path.join(base_dir, ".env")
    if os.path.exists(dotenv_path):
        try:
            with open(dotenv_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k_s = k.strip()
                        if k_s and k_s not in os.environ:
                            os.environ[k_s] = v.strip().strip("'\"")
        except Exception:
            pass
    _ENV_LOADED = True


@dataclass(frozen=True)
class PostgresConfig:
    """
    Konfiguracja połączenia z PostgreSQL / pgvector.
    Obsługuje DATABASE_URL (z pierwszeństwem) oraz zestaw zmiennych POSTGRES_*.
    Zapewnia brak domyślnych haseł w kodzie i rygorystyczne maskowanie poświadczeń w logach i wyjątkach.
    """
    host: str
    port: int
    dbname: str
    user: str
    password: Optional[str]
    sslmode: str = "prefer"
    connect_timeout: int = 5
    application_name: Optional[str] = "karik_app"

    def __repr__(self) -> str:
        return (
            f"PostgresConfig(host={self.host!r}, port={self.port}, dbname={self.dbname!r}, "
            f"user={self.user!r}, password={'***' if self.password else None}, "
            f"sslmode={self.sslmode!r}, connect_timeout={self.connect_timeout})"
        )

    def to_masked_dsn(self) -> str:
        """Zwraca bezpieczny ciąg DSN z zamaskowanym hasłem."""
        host_repr = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        auth = f"{self.user}:***@" if self.password else f"{self.user}@"
        return f"postgresql://{auth}{host_repr}:{self.port}/{self.dbname}"

    @classmethod
    def _resolve_docker_host(cls, host: str) -> str:
        """Kieruje nazwę kontenera Docker na 127.0.0.1, jeśli host nie jest rozwiązywalny w DNS."""
        if host in ("postgres-pgvector", "postgres_pgvector"):
            try:
                socket.gethostbyname(host)
            except (socket.gaierror, OSError):
                return "127.0.0.1"
        return host

    @classmethod
    def from_url(cls, database_url: str, require_password: bool = True) -> "PostgresConfig":
        """
        Tworzy instancję PostgresConfig z adresu DATABASE_URL.
        Rygorystycznie waliduje schemat, maskuje hasła w błędach i dekoduje percent-encoding.
        """
        masked_url = mask_password_in_url(database_url)

        try:
            parsed = urlsplit(database_url)
        except Exception as e:
            raise DatabaseConfigurationError(
                f"Nieprawidłowy format DATABASE_URL '{masked_url}': {e}"
            ) from None

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("postgresql", "postgres", "postgresql+psycopg2"):
            raise DatabaseConfigurationError(
                f"Nieobsługiwany schemat '{scheme}' w DATABASE_URL '{masked_url}'. "
                "Wymagany schemat to 'postgresql://' lub 'postgres://'."
            )

        if not parsed.hostname:
            raise DatabaseConfigurationError(
                f"Brak zdefiniowanego hosta w DATABASE_URL: '{masked_url}'."
            )

        host = cls._resolve_docker_host(unquote(parsed.hostname))

        # Walidacja portu — brak cichego fallbacku, przechwycenie ValueError z urllib.parse
        try:
            raw_port = parsed.port
        except ValueError as e:
            raise DatabaseConfigurationError(
                f"Nieprawidłowy port PostgreSQL w DATABASE_URL '{masked_url}': {e}"
            ) from None

        if raw_port is not None:
            port = raw_port
            if not (1 <= port <= 65535):
                raise DatabaseConfigurationError(
                    f"Nieprawidłowy port PostgreSQL {port} w DATABASE_URL '{masked_url}'. "
                    "Port musi być liczbą z zakresu 1-65535."
                )
        else:
            port = 5432

        # Baza danych (ścieżka z pominięciem początkowego slash)
        raw_path = parsed.path.lstrip("/")
        if not raw_path:
            dbname = "karik_db"
        else:
            dbname = unquote(raw_path)

        user = unquote(parsed.username) if parsed.username else "karik_admin"
        password = unquote(parsed.password) if parsed.password is not None else None

        if require_password and not password:
            raise DatabaseConfigurationError(
                f"Wymagane hasło bazy danych nie zostało podane w DATABASE_URL '{masked_url}'. "
                "Zahardkodowane hasła zostały usunięte w celu spełnienia wymogów bezpieczeństwa."
            )

        # Parsowanie parametrów zapytania (query parameters)
        sslmode = "prefer"
        connect_timeout = 5
        application_name = "karik_app"

        if parsed.query:
            query_params = parse_qs(parsed.query)
            for param_key, param_vals in query_params.items():
                if param_key.lower() not in ALLOWED_QUERY_PARAMS:
                    raise DatabaseConfigurationError(
                        f"Niedozwolony parametr zapytania '{param_key}' w DATABASE_URL. "
                        f"Dozwolone parametry: {sorted(list(ALLOWED_QUERY_PARAMS))}"
                    )
                val = param_vals[-1]
                if param_key.lower() == "sslmode":
                    sslmode = val
                elif param_key.lower() == "connect_timeout":
                    try:
                        connect_timeout = int(val)
                        if connect_timeout <= 0:
                            raise ValueError()
                    except ValueError:
                        raise DatabaseConfigurationError(
                            f"Parametr connect_timeout musi być dodatnią liczbą całkowitą, otrzymano: '{val}'"
                        )
                elif param_key.lower() == "application_name":
                    application_name = val

        return cls(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=connect_timeout,
            application_name=application_name,
        )

    @classmethod
    def from_env(cls, require_password: bool = True, load_env: bool = True) -> "PostgresConfig":
        """
        Pobiera konfigurację z otoczenia.
        W przypadku obecności zmiennej DATABASE_URL ma ona bezwzględne pierwszeństwo przed zmiennymi POSTGRES_*.
        """
        if load_env:
            load_env_file()

        # 1. Bezwzględny priorytet: RAG_DATABASE_URL lub DATABASE_URL (brak cichego fallbacku na POSTGRES_*)
        database_url = os.getenv("RAG_DATABASE_URL") or os.getenv("DATABASE_URL")
        if database_url:
            return cls.from_url(database_url, require_password=require_password)

        # 2. Fallback: indywidualne zmienne POSTGRES_*
        raw_host = os.getenv("POSTGRES_HOST", "127.0.0.1")
        port_raw = os.getenv("POSTGRES_PORT", "5432")
        try:
            port = int(port_raw)
            if not (1 <= port <= 65535):
                raise ValueError()
        except ValueError:
            raise DatabaseConfigurationError(
                f"Zmienna POSTGRES_PORT musi być poprawną liczbą z zakresu 1-65535, otrzymano: '{port_raw}'."
            )

        host = cls._resolve_docker_host(raw_host)
        dbname = os.getenv("POSTGRES_DB", "karik_db")
        user = os.getenv("POSTGRES_USER", "karik_admin")
        password = os.getenv("POSTGRES_PASSWORD")
        sslmode = os.getenv("POSTGRES_SSLMODE", "prefer")

        timeout_raw = os.getenv("POSTGRES_CONNECT_TIMEOUT", "5")
        try:
            connect_timeout = int(timeout_raw)
            if connect_timeout <= 0:
                raise ValueError()
        except ValueError:
            raise DatabaseConfigurationError(
                f"POSTGRES_CONNECT_TIMEOUT musi być dodatnią liczbą całkowitą, otrzymano: '{timeout_raw}'."
            )

        if require_password and not password:
            raise DatabaseConfigurationError(
                "POSTGRES_PASSWORD must be explicitly configured when connecting to PostgreSQL. "
                "Hardcoded credentials have been removed for security compliance."
            )

        return cls(
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
            connect_timeout=connect_timeout,
        )

    def to_conn_params(self) -> Dict[str, Any]:
        """Zwraca słownik parametrów akceptowany przez psycopg2.connect()."""
        params = {
            "host": self.host,
            "port": self.port,
            "dbname": self.dbname,
            "user": self.user,
            "client_encoding": "utf8",
            "connect_timeout": self.connect_timeout,
        }
        if self.password:
            params["password"] = self.password
        if self.sslmode:
            params["sslmode"] = self.sslmode
        if self.application_name:
            params["application_name"] = self.application_name
        return params
