import os
import logging
from typing import Optional, List
from database.config import PostgresConfig

logger = logging.getLogger(__name__)


def apply_migrations(conn=None) -> List[str]:
    """
    Aplikuje niezaaplikowane migracje SQL z katalogu database/migrations/
    w bazie PostgreSQL, śledząc historię w tabeli schema_migrations.
    """
    should_close = False
    if conn is None:
        import psycopg2
        config = PostgresConfig.from_env(require_password=True)
        conn = psycopg2.connect(**config.to_conn_params())
        should_close = True

    applied = []
    try:
        with conn.cursor() as cur:
            # Tabela rejestrująca zaaplikowane migracje
            cur.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(64) PRIMARY KEY,
                    applied_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)

            migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")
            if not os.path.exists(migrations_dir):
                return applied

            sql_files = sorted([f for f in os.listdir(migrations_dir) if f.endswith(".sql")])
            for sql_file in sql_files:
                cur.execute("SELECT 1 FROM schema_migrations WHERE version = %s;", (sql_file,))
                if cur.fetchone() is None:
                    logger.info(f"Aplikowanie migracji schematu: {sql_file}...")
                    file_path = os.path.join(migrations_dir, sql_file)
                    with open(file_path, "r", encoding="utf-8") as f:
                        sql_content = f.read()
                    cur.execute(sql_content)
                    cur.execute("INSERT INTO schema_migrations (version) VALUES (%s);", (sql_file,))
                    applied.append(sql_file)
                    logger.info(f"Pomyślnie zaaplikowano migrację: {sql_file}")

        conn.commit()
        return applied
    finally:
        if should_close:
            conn.close()
