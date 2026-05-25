"""Spin up isolated PostgreSQL clusters from theseus-rs binaries.

Each `PgCluster` owns its own PGDATA on a btrfs-backed test directory,
listens on a per-cluster TCP port + unix socket, and tears itself down
cleanly. We launch as the current (non-root) user, which is the only
mode the theseus-rs bundles support.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import psycopg

from .pg_runtime import PgBinary


REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / ".test-data"

# Listen address for the embedded test clusters. We deliberately bind to
# loopback only — these are throwaway DBs with `--auth=trust`, never meant
# to be reachable from outside the test process.
TEST_HOST = "127.0.0.1"


def _pick_free_port() -> int:
    """Pick an unused TCP port on TEST_HOST.

    Binds to port 0 (kernel picks), reads what was assigned, then closes
    the socket. There's a tiny race window where another process could
    grab the same port before we bring postgres up; in practice this is
    fine for short-lived test clusters.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((TEST_HOST, 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class PgCluster:
    """A running PostgreSQL instance, isolated to its own PGDATA + port.

    Two modes:
    - Spawn (default `__init__`): we allocate a fresh PGDATA + free port,
      call `initdb()` and `start()`, and `destroy()` tears everything down.
    - Attach (via `attach`): wraps a cluster that is already running
      (e.g. one spun up by `scripts/run-harness.py`). We don't run initdb
      and `destroy()` is a no-op so the persistent harness survives.
    """

    def __init__(self, binary: PgBinary, data_root: Path = DATA_ROOT,
                 extra_conf: dict[str, str] | None = None) -> None:
        """Build a spawn-mode cluster spec (does not start anything yet).

        Args:
            binary: A resolved `PgBinary` (downloaded + extracted bundle).
            data_root: Parent directory where this cluster's PGDATA will live.
            extra_conf: Optional dict of `postgresql.conf` overrides appended
                after the test defaults. Values are written verbatim, so
                string values must include their own surrounding quotes
                where SQL would require them (e.g. `"'clone'"`).
        """
        self.binary = binary
        self.data_root = data_root
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cluster_id = f"pg{binary.major}-{secrets.token_hex(4)}"
        self.pgdata = self.data_root / self.cluster_id
        self.socket_dir = self.pgdata / ".s"
        self.log_file = self.pgdata.with_suffix(".log")
        self.port = _pick_free_port()
        self.superuser = "postgres"
        self.extra_conf = dict(extra_conf) if extra_conf else {}
        self._running = False
        # Externally-managed clusters (from `attach`) skip all lifecycle
        # operations — the harness owns initdb / start / stop / destroy.
        self._external = False

    @classmethod
    def attach(cls, binary: PgBinary, pgdata: Path, port: int,
               superuser: str = "postgres") -> "PgCluster":
        """Wrap an already-running cluster managed by an external harness.

        Used when `scripts/run-harness.py` has spun up persistent clusters
        between test runs. `start`/`stop`/`destroy` become no-ops so the
        harness's clusters survive the pytest session; only the database-
        level cleanup between tests still runs.

        Args:
            binary: The PG binary matching the running cluster's version.
            pgdata: Path to the running cluster's data directory.
            port: TCP port the running cluster is listening on.
            superuser: Superuser name (defaults to "postgres").
        """
        instance = cls.__new__(cls)
        instance.binary = binary
        instance.data_root = pgdata.parent
        instance.cluster_id = pgdata.name
        instance.pgdata = pgdata
        instance.socket_dir = pgdata / ".s"
        instance.log_file = pgdata.with_suffix(".log")
        instance.port = port
        instance.superuser = superuser
        instance.extra_conf = {}
        instance._running = True
        instance._external = True
        return instance

    def initdb(self) -> None:
        """Initialize a fresh PGDATA at `self.pgdata` and write our conf.

        Removes any pre-existing directory at the same path. Uses `trust`
        authentication and the C locale to keep test setup deterministic.
        """
        if self.pgdata.exists():
            shutil.rmtree(self.pgdata)
        self.pgdata.mkdir(parents=True)
        subprocess.run(
            [
                str(self.binary.initdb),
                "-D", str(self.pgdata),
                "-U", self.superuser,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            check=True,
            env=self.binary.env(),
            capture_output=True,
            text=True,
        )
        self.socket_dir.mkdir(exist_ok=True)
        self._write_postgresql_conf()

    def _write_postgresql_conf(self) -> None:
        """Append test-tuned overrides + any caller-supplied `extra_conf`."""
        conf = self.pgdata / "postgresql.conf"
        with open(conf, "a", encoding="utf-8") as fh:
            fh.write("\n# foldout integration test overrides\n")
            fh.write(f"port = {self.port}\n")
            fh.write(f"unix_socket_directories = '{self.socket_dir}'\n")
            fh.write(f"listen_addresses = '{TEST_HOST}'\n")
            fh.write("fsync = off\n")
            fh.write("full_page_writes = off\n")
            fh.write("synchronous_commit = off\n")
            fh.write("shared_buffers = 64MB\n")
            fh.write("max_connections = 30\n")
            for key, value in self.extra_conf.items():
                fh.write(f"{key} = {value}\n")

    def start(self) -> None:
        """Boot the postgres server via `pg_ctl start` and wait until ready.

        No-op if the cluster is already running, or if this instance is
        in attach mode (the harness owns the lifecycle).
        """
        if self._external or self._running:
            return
        subprocess.run(
            [
                str(self.binary.pg_ctl),
                "-D", str(self.pgdata),
                "-l", str(self.log_file),
                "-w",
                "-t", "30",
                "start",
            ],
            check=True,
            env=self.binary.env(),
            capture_output=True,
            text=True,
        )
        self._running = True
        self._wait_ready()

    def _wait_ready(self, timeout_s: float = 15.0) -> None:
        """Poll `SELECT 1` until the server accepts connections or we time out."""
        deadline = time.monotonic() + timeout_s
        last_err: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.dsn(database="postgres")) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SELECT 1")
                        cur.fetchone()
                return
            except Exception as exc:
                last_err = exc
                time.sleep(0.2)
        raise RuntimeError(
            f"Postgres at {self.pgdata} (port {self.port}) never became ready: {last_err}"
        )

    def stop(self) -> None:
        """Stop the postgres server (immediate mode). No-op if not running.

        No-op when in attach mode — the external harness owns shutdown.
        Failures from spawn-mode stops are swallowed since `destroy()`
        rmtrees PGDATA regardless.
        """
        if self._external or not self._running:
            return
        try:
            subprocess.run(
                [
                    str(self.binary.pg_ctl),
                    "-D", str(self.pgdata),
                    "-m", "immediate",
                    "-w",
                    "-t", "20",
                    "stop",
                ],
                check=False,
                env=self.binary.env(),
                capture_output=True,
                text=True,
            )
        finally:
            self._running = False

    def destroy(self) -> None:
        """Stop the cluster and delete its PGDATA + log file from disk.

        No-op in attach mode — the persistent harness must survive past
        the pytest session that borrowed it.
        """
        if self._external:
            return
        self.stop()
        if self.pgdata.exists():
            shutil.rmtree(self.pgdata, ignore_errors=True)
        if self.log_file.exists():
            try:
                self.log_file.unlink()
            except OSError:
                pass

    def dsn(self, database: str = "postgres", user: str | None = None) -> str:
        """Return a `postgresql://` DSN aimed at this cluster.

        Args:
            database: dbname to embed in the DSN. Defaults to the bootstrap
                `postgres` DB which exists on every cluster.
            user: Optional user override. Defaults to the cluster's superuser.
        """
        effective_user = user or self.superuser
        return f"postgresql://{effective_user}@{TEST_HOST}:{self.port}/{database}"

    def psql(self, sql: str, database: str = "postgres") -> str:
        """Run a single SQL statement via the bundled `psql` and return stdout.

        Uses `-X` (no `~/.psqlrc`) and `ON_ERROR_STOP=1` so failures are loud.
        """
        result = subprocess.run(
            [
                str(self.binary.psql),
                "-X",
                "-v", "ON_ERROR_STOP=1",
                "-h", TEST_HOST,
                "-p", str(self.port),
                "-U", self.superuser,
                "-d", database,
                "-c", sql,
            ],
            check=True,
            env=self.binary.env(),
            capture_output=True,
            text=True,
        )
        return result.stdout

    def create_database(self, name: str) -> None:
        """Create a database. Rejects names containing quote/escape chars.

        We embed the name directly into the SQL (no parameter binding for
        DDL), so we explicitly forbid characters that could break out of
        the identifier quoting.
        """
        if '"' in name or '\\' in name:
            raise ValueError(f"unsafe database name: {name!r}")
        self.psql(f'CREATE DATABASE "{name}"')

    def __enter__(self) -> "PgCluster":
        """Context-manager entry: initdb + start, returning self."""
        self.initdb()
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        """Context-manager exit: tear the cluster down unconditionally."""
        self.destroy()
