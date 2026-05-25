"""Install and verify foldout change-capture + DDL auditing.

Provides an object-oriented installer that checks if the required
objects exist in a target database and installs them from a bundled
SQL file if missing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import psycopg

from .db import get_database_dsn


class ChangeCaptureInstaller:
    """Installer for foldout change capture and DDL auditing.

    Reads SQL from ``src/foldout/sql/change_capture.sql`` and executes it
    against a target database when not already installed.
    """

    def __init__(self, sql_path: Optional[Path] = None) -> None:
        base = Path(__file__).parent
        default_sql = base / "sql" / "change_capture.sql"
        if sql_path is None:
            self.sql_path = default_sql
        else:
            self.sql_path = sql_path

    def make_db_dsn(self, database_name: str) -> str:
        """Return a DSN targeting ``database_name`` using base DSN env."""
        base_dsn = get_database_dsn()
        params = psycopg.conninfo.conninfo_to_dict(base_dsn)
        params["dbname"] = database_name
        return psycopg.conninfo.make_conninfo(**params)

    def is_installed(self, database_name: str) -> bool:
        """Check if schema, core table and trigger function exist."""
        dsn = self.make_db_dsn(database_name)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM pg_namespace WHERE nspname = 'foldout'
                    )
                    """
                )
                row_ns = cur.fetchone()
                has_schema = bool(row_ns and row_ns[0])
                if not has_schema:
                    return False

                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM pg_class c
                        JOIN pg_namespace n ON n.oid = c.relnamespace
                        WHERE n.nspname = 'foldout'
                          AND c.relname = 'change_log'
                          AND c.relkind = 'r'
                    )
                    """
                )
                row_tbl = cur.fetchone()
                has_table = bool(row_tbl and row_tbl[0])
                if not has_table:
                    return False

                cur.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1
                        FROM pg_proc p
                        JOIN pg_namespace n ON n.oid = p.pronamespace
                        WHERE n.nspname = 'foldout'
                          AND p.proname = 'capture'
                    )
                    """
                )
                row_fn = cur.fetchone()
                has_capture = bool(row_fn and row_fn[0])
                if not has_capture:
                    return False

                return True

    def install(self, database_name: str) -> None:
        """Execute the bundled SQL to install objects into the database."""
        if not self.sql_path.exists():
            raise FileNotFoundError(str(self.sql_path))

        sql_text = self.sql_path.read_text(encoding="utf-8")
        dsn = self.make_db_dsn(database_name)
        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_text)
            conn.commit()

    def ensure_installed(self, database_name: str) -> bool:
        """Install if missing. Returns True if changes were applied."""
        installed = self.is_installed(database_name)
        if installed:
            return False
        self.install(database_name)
        return True

