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


def _pick_free_port() -> int:
    """Bind to port 0, read the port, then release. Tiny race window."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


class PgCluster:
    """A running PostgreSQL instance, isolated to its own PGDATA + port."""

    def __init__(self, binary: PgBinary, data_root: Path = DATA_ROOT) -> None:
        self.binary = binary
        self.data_root = data_root
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.cluster_id = f"pg{binary.major}-{secrets.token_hex(4)}"
        self.pgdata = self.data_root / self.cluster_id
        self.socket_dir = self.pgdata / ".s"
        self.log_file = self.pgdata.with_suffix(".log")
        self.port = _pick_free_port()
        self.superuser = "postgres"
        self._running = False

    def initdb(self) -> None:
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
        # Append our overrides; initdb's defaults stay in place.
        conf = self.pgdata / "postgresql.conf"
        with open(conf, "a", encoding="utf-8") as fh:
            fh.write("\n# foldout integration test overrides\n")
            fh.write(f"port = {self.port}\n")
            fh.write(f"unix_socket_directories = '{self.socket_dir}'\n")
            fh.write("listen_addresses = '127.0.0.1'\n")
            fh.write("fsync = off\n")
            fh.write("full_page_writes = off\n")
            fh.write("synchronous_commit = off\n")
            fh.write("shared_buffers = 64MB\n")
            fh.write("max_connections = 30\n")

    def start(self) -> None:
        if self._running:
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
        if not self._running:
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
        self.stop()
        if self.pgdata.exists():
            shutil.rmtree(self.pgdata, ignore_errors=True)
        if self.log_file.exists():
            try:
                self.log_file.unlink()
            except OSError:
                pass

    def dsn(self, database: str = "postgres", user: str | None = None) -> str:
        u = user or self.superuser
        return f"postgresql://{u}@127.0.0.1:{self.port}/{database}"

    def psql(self, sql: str, database: str = "postgres") -> str:
        result = subprocess.run(
            [
                str(self.binary.psql),
                "-X",
                "-v", "ON_ERROR_STOP=1",
                "-h", "127.0.0.1",
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
        # Quote to allow mixed case / underscores; reject backticks/quotes
        # to keep this safe.
        if '"' in name or '\\' in name:
            raise ValueError(f"unsafe database name: {name!r}")
        self.psql(f'CREATE DATABASE "{name}"')

    def __enter__(self) -> "PgCluster":
        self.initdb()
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.destroy()
