"""Database connection helpers for foldout."""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import psycopg

from .helpers import pick


logger = logging.getLogger(__name__)


def get_database_dsn() -> str:
    """Get the database DSN from FLD_DATABASE environment variable."""
    dsn = os.getenv("FLD_DATABASE")
    if not dsn:
        raise ValueError("FLD_DATABASE environment variable is required")
    return dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Return a new PostgreSQL connection using the provided DSN or FLD_DATABASE."""
    if dsn is None:
        dsn = get_database_dsn()
    return psycopg.connect(dsn)


def list_databases() -> list[dict[str, str | int]]:
    """List all databases with their OIDs and names."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid, datname FROM pg_database ORDER BY datname")
            return pick(cur.fetchall(), "oid", "name")


def get_data_directory() -> str:
    """Return the PostgreSQL data directory path.

    If the environment variable `FLD_PG_DATA_PATH` is defined, its value
    is returned to allow overriding the detected PostgreSQL data directory.
    This is useful when PostgreSQL is running inside a container while
    foldout runs on the host and needs a host-visible path for file
    operations (e.g., copy-on-write file copying).

    Otherwise falls back to querying the server with `SHOW data_directory`.
    """
    override = os.getenv("FLD_PG_DATA_PATH")
    if override:
        return override

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            return cur.fetchone()[0]


def get_database_oid(database_name: str) -> int:
    """Get the OID of a specific database."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (database_name,))
            result = cur.fetchone()
            if result is None:
                raise ValueError(f"Database '{database_name}' not found")
            return result[0]


def get_branch_parent(branch_name: str) -> tuple[int, int, str]:
    """Look up branch in fld_databases and return (branch_oid, parent_oid, parent_datname)."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT vd.oid, vd.parent, pg.datname
                FROM fld_databases vd
                JOIN pg_database pg ON pg.oid = vd.parent
                WHERE vd.datname = %s AND vd.type = 'branch'
            """, (branch_name,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"Branch '{branch_name}' not found in fld_databases "
                    f"(or its parent no longer exists)"
                )
            return row[0], row[1], row[2]


def save_page_index(branch_oid: int, kind: str, index) -> None:
    """Persist a `PageIndex` row in `fld_page_index` (upsert).

    Args:
        branch_oid: OID of the branch (or the source DB for a "parent" kind).
        kind: 'branch' or 'parent'. See foldout_4.sql for the semantics.
        index: A `foldout.page_diff.PageIndex` instance.
    """
    if kind not in ("branch", "parent"):
        raise ValueError(f"unknown page-index kind: {kind!r}")
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params["dbname"] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    # Local import to avoid the page_diff → db → page_diff cycle.
    from psycopg.types.json import Jsonb
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO fld_page_index
                  (branch_oid, kind, dbname, lsn, xid_snapshot, relations)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (branch_oid, kind) DO UPDATE SET
                  dbname       = EXCLUDED.dbname,
                  lsn          = EXCLUDED.lsn,
                  xid_snapshot = EXCLUDED.xid_snapshot,
                  captured_at  = now(),
                  relations    = EXCLUDED.relations
                """,
                (
                    branch_oid, kind, index.dbname, index.lsn,
                    index.xid_snapshot, Jsonb(index.relations),
                ),
            )
        conn.commit()


def load_page_index(branch_oid: int, kind: str):
    """Load a previously-saved `PageIndex`, or return None if missing.

    Returns:
        A `foldout.page_diff.PageIndex`, or `None` if no row exists for
        `(branch_oid, kind)`.
    """
    if kind not in ("branch", "parent"):
        raise ValueError(f"unknown page-index kind: {kind!r}")
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params["dbname"] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT dbname, lsn, xid_snapshot, relations "
                "FROM fld_page_index WHERE branch_oid = %s AND kind = %s",
                (branch_oid, kind),
            )
            row = cur.fetchone()
    if row is None:
        return None
    # Local import to avoid the page_diff → db → page_diff cycle.
    from .page_diff import PageIndex
    return PageIndex(
        dbname=row[0],
        lsn=row[1],
        xid_snapshot=row[2] or "",
        relations=row[3] or [],
    )


def delete_page_index_for_branch(branch_oid: int) -> int:
    """Remove every `fld_page_index` row for a branch.

    Called by `foldout delete-branch`. Returns the number of rows
    deleted (0..2 — one for `branch`, one for `parent`).
    """
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params["dbname"] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM fld_page_index WHERE branch_oid = %s",
                (branch_oid,),
            )
            removed = cur.rowcount
        conn.commit()
    return removed


def terminate_database_connections(database_name: str) -> int:
    """Terminate all connections to a database except the current one.

    Returns the number of backends `pg_terminate_backend` was called
    against. Termination is asynchronous on the server side — callers
    that need to know when the backends are actually gone should follow
    up with `wait_for_no_connections()`.
    """
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
            """, (database_name,))
            terminated_count = cur.rowcount
            conn.commit()
            return terminated_count


class ActiveConnection:
    """Snapshot of one row from `pg_stat_activity` for human display.

    We use a plain class (not a dataclass) so the rendering helpers
    `__str__` / `format_table` can live on it and so callers can rely on
    these specific attribute names when formatting error messages.
    """

    def __init__(self, pid: int, application_name: str, state: str,
                 client_addr: str | None, query: str) -> None:
        """Build a record. All fields are stringified for stable output."""
        self.pid = pid
        self.application_name = application_name or ""
        self.state = state or ""
        self.client_addr = client_addr or ""
        self.query = query or ""

    def __repr__(self) -> str:
        """Compact repr for assertion messages."""
        return (
            f"ActiveConnection(pid={self.pid}, app={self.application_name!r}, "
            f"state={self.state!r})"
        )


def format_connection_table(connections: list[ActiveConnection]) -> str:
    """Render a list of `ActiveConnection`s as a multi-line aligned table."""
    if not connections:
        return "  (no active connections)"
    lines: list[str] = []
    lines.append(
        f"  {'pid':<8}{'app':<24}{'state':<22}{'client':<18}query"
    )
    lines.append("  " + "-" * 90)
    for conn in connections:
        # Truncate the query to keep the table readable but still useful.
        query_preview = conn.query.replace("\n", " ").strip()
        if len(query_preview) > 80:
            query_preview = query_preview[:77] + "..."
        lines.append(
            f"  {conn.pid:<8}{conn.application_name[:22]:<24}"
            f"{conn.state[:20]:<22}{conn.client_addr[:16]:<18}"
            f"{query_preview}"
        )
    return "\n".join(lines)


class SourceHasActiveConnections(RuntimeError):
    """Raised when a snapshot/branch is blocked by existing connections.

    Carries the full connection list so the CLI layer can format a
    detailed error message without re-querying.
    """

    def __init__(self, database_name: str,
                 connections: list[ActiveConnection]) -> None:
        """Build with the source DB name and the list of active backends."""
        self.database_name = database_name
        self.connections = connections
        super().__init__(
            f"{len(connections)} active connection(s) to '{database_name}'"
        )


class ActiveConnectionsTimeout(RuntimeError):
    """Raised when pg_terminate_backend doesn't drain backends in time.

    Indicates either a stuck backend (e.g. a buggy extension ignoring
    SIGTERM) or a wait timeout set too low. Carries the still-connected
    list at the moment we gave up.
    """

    def __init__(self, database_name: str,
                 connections: list[ActiveConnection],
                 timeout_s: float) -> None:
        """Build with source DB name, the remaining connections, and the timeout used."""
        self.database_name = database_name
        self.connections = connections
        self.timeout_s = timeout_s
        super().__init__(
            f"{len(connections)} connection(s) still attached to "
            f"'{database_name}' after {timeout_s}s"
        )


def _connect_postgres_db() -> psycopg.Connection:
    """Open a connection to the maintenance `postgres` database.

    Used by the lock plumbing — `ALTER DATABASE ... ALLOW_CONNECTIONS`
    can't be run on the database it's altering, so we connect via the
    cluster's bootstrap DB.
    """
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params["dbname"] = "postgres"
    return psycopg.connect(psycopg.conninfo.make_conninfo(**conn_params))


def get_active_connections(database_name: str) -> list[ActiveConnection]:
    """Return the non-self backends currently connected to `database_name`."""
    out: list[ActiveConnection] = []
    with _connect_postgres_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT pid, application_name, state, client_addr::text, query
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                ORDER BY pid
                """,
                (database_name,),
            )
            for row in cur.fetchall():
                out.append(
                    ActiveConnection(
                        pid=int(row[0]),
                        application_name=row[1],
                        state=row[2],
                        client_addr=row[3],
                        query=row[4],
                    )
                )
    return out


def alter_database_allow_connections(database_name: str, allow: bool) -> None:
    """Toggle `pg_database.datallowconn` for `database_name`.

    When `allow=False`, new connection attempts to this database are
    rejected by PG with "database X is not currently accepting
    connections". Existing connections continue normally.
    """
    sql = (
        f'ALTER DATABASE "{database_name}" '
        f'WITH ALLOW_CONNECTIONS {"true" if allow else "false"}'
    )
    with _connect_postgres_db() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql)


def checkpoint() -> None:
    """Issue a cluster-wide `CHECKPOINT`.

    Flushes every dirty 8 KB page in shared_buffers to its on-disk
    relation file, plus a few WAL records to mark the consistent point.
    Cluster-wide — doesn't matter which DB we're connected to.

    Used before a snapshot/branch so the file mtime/size we capture, and
    the data the subsequent reflink copy reads, reflect committed state.
    """
    with _connect_postgres_db() as conn:
        with conn.cursor() as cur:
            cur.execute("CHECKPOINT")


def wait_for_no_connections(database_name: str,
                            timeout_s: float = 10.0,
                            poll_interval_s: float = 0.1) -> None:
    """Poll until `database_name` has no non-self backends, or timeout.

    Used after `pg_terminate_backend` since termination is asynchronous
    on the server side. Raises `ActiveConnectionsTimeout` with the
    remaining connection list if the wait expires.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        remaining = get_active_connections(database_name)
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise ActiveConnectionsTimeout(database_name, remaining, timeout_s)
        time.sleep(poll_interval_s)


@contextmanager
def lock_source_database(database_name: str,
                         force: bool = False,
                         wait_timeout_s: float = 10.0) -> Iterator[None]:
    """Acquire an exclusive lock on `database_name` for the duration.

    Sequence (lock first, then check — eliminates the race window where
    a client could connect between the check and the snapshot copy):

    1. `ALTER DATABASE ... ALLOW_CONNECTIONS false` (reject new connections).
    2. Query `pg_stat_activity` for non-self backends.
    3. - Empty → CHECKPOINT, yield.
       - Non-empty + `force=False` → raise `SourceHasActiveConnections`.
       - Non-empty + `force=True` → `pg_terminate_backend` each,
         `wait_for_no_connections` (raises `ActiveConnectionsTimeout`
         on timeout), CHECKPOINT, yield.

    A `try / finally` restores `ALLOW_CONNECTIONS true` on every exit
    path including exceptions. The one unrecoverable case is the Python
    process being `kill -9`'d between lock and unlock — see TASKS.md for
    the manual recovery one-liner.

    Args:
        database_name: The source database to lock.
        force: If True, terminate existing connections instead of failing.
        wait_timeout_s: How long to wait for terminated backends to drop.
    """
    alter_database_allow_connections(database_name, allow=False)
    try:
        connections = get_active_connections(database_name)
        if connections:
            if not force:
                raise SourceHasActiveConnections(database_name, connections)
            logger.info(
                "Terminating %d connection(s) on '%s' (--force)",
                len(connections), database_name,
            )
            terminate_database_connections(database_name)
            wait_for_no_connections(database_name, timeout_s=wait_timeout_s)
        # NOTE: CHECKPOINT is the caller's responsibility — see
        # `foldout.db.checkpoint()`. Hoisting it out keeps lock concerns
        # (allow_connections + terminate) separate from buffer flushing,
        # and lets the CLI layer print timing info around it.
        yield
    finally:
        alter_database_allow_connections(database_name, allow=True)

def get_pg_major_version(conn) -> int:
    """Return the major version (e.g. `17`, `18`) of the connected server.

    Used to gate version-specific SQL — STRATEGY=FILE_COPY requires PG 15+,
    file_copy_method requires PG 18+, etc. Reads `server_version` (the
    human-readable form like `'17.5 (Debian 17.5-1.pgdg120+1)'`) and
    takes the leading integer.
    """
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        version_str = cur.fetchone()[0]
    return int(version_str.split('.')[0])


def get_file_copy_method(conn) -> str | None:
    """Return the server's `file_copy_method` GUC, or None on PG < 18.

    `file_copy_method` was introduced in PostgreSQL 18. When set to
    `'clone'`, `CREATE DATABASE ... STRATEGY = FILE_COPY` uses kernel
    reflink syscalls (FICLONE / copy_file_range on CoW filesystems) so
    no userland copy step is needed.
    """
    if get_pg_major_version(conn) < 18:
        return None
    with conn.cursor() as cur:
        cur.execute("SHOW file_copy_method")
        return cur.fetchone()[0].lower()


def should_use_native_clone(conn) -> bool:
    """True iff PG ≥ 18 is configured to clone files itself.

    When True, `CREATE DATABASE ... STRATEGY = FILE_COPY` already produces
    reflinked relation files; the manual `cp --reflink=always` step in
    `copy_database_files()` is redundant and should be skipped.
    """
    return get_file_copy_method(conn) == "clone"

def _create_database_sql(name: str, pg_version: int,
                         template: str | None) -> str:
    """Render the CREATE DATABASE SQL for our supported strategies.

    Why we always force `STRATEGY=FILE_COPY` (even though WAL_LOG is the
    PG 15+ default and would be faster on idle clusters):

    `WAL_LOG` populates the new database's pages by going through
    shared_buffers — every page gets a dirty buffer entry keyed by
    `(new_oid, relfilenode)`. When our caller subsequently runs
    `copy_database_files()` (rm -rf + cp --reflink the on-disk dir),
    the disk has source's content but shared_buffers still serves the
    stale empty-template1 pages for the new OID. First query on the
    snapshot returns "relation X does not exist" because pg_class is
    read from cache.

    `FILE_COPY` doesn't have this problem: PG uses direct `copy_dir()`
    syscalls and never populates shared_buffers for the new OID. The
    cost is two internal `CHECKPOINT_IMMEDIATE`s (slow on busy clusters)
    — that's the price for correctness on the manual-cp path.

    On PG 18 with `file_copy_method=clone` + `TEMPLATE source`, the byte
    copy inside FILE_COPY becomes a kernel FICLONE; reflinks happen
    inside PG and we skip `copy_database_files()` entirely.

    PG < 15: WAL_LOG didn't exist; plain CREATE DATABASE is FILE_COPY
    by default and we don't need the keyword.
    """
    if template is not None:
        return (
            f'CREATE DATABASE "{name}" '
            f'TEMPLATE "{template}" STRATEGY=\'FILE_COPY\''
        )
    if pg_version < 15:
        return f'CREATE DATABASE "{name}"'
    return f'CREATE DATABASE "{name}" STRATEGY=\'FILE_COPY\''


def create_snapshot_database(source_database: str,
                             template: str | None = None) -> tuple[str, int]:
    """Create a new database for snapshot with timestamp name.

    Pass `template=source_database` (only safe on PG 18+ when no other
    connections are open to the source) to let PG clone the source itself.
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_name = f"snapshot_{source_database}_{timestamp}"

    with connect() as conn:
        # Set autocommit for CREATE DATABASE (required for PostgreSQL)
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            cur.execute(_create_database_sql(snapshot_name, pg_version, template))
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (snapshot_name,))
            oid = cur.fetchone()[0]

    return snapshot_name, oid


def create_base_database(source_database: str, branch_name: str,
                         template: str | None = None) -> tuple[str, int]:
    """Create a COW snapshot of source_database to serve as the merge base
    for `fld diff` of `branch_name`. Returned name is `__base__<branch_name>`.
    """
    base_name = f"__base__{branch_name}"
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            cur.execute(_create_database_sql(base_name, pg_version, template))
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (base_name,))
            oid = cur.fetchone()[0]
    return base_name, oid


def register_base_database(base_name: str, base_oid: int, parent_oid: int,
                           branch_oid: int) -> None:
    """Insert the base into fld_databases and link it to its branch."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fld_databases (oid, datname, parent, created_at, type, status)
                VALUES (%s, %s, %s, %s, 'base', 'live')
            """, (base_oid, base_name, parent_oid, datetime.now()))
            cur.execute(
                "UPDATE fld_databases SET base_oid = %s WHERE oid = %s",
                (base_oid, branch_oid),
            )
        conn.commit()


def get_branch_base(branch_name: str) -> tuple[int, str] | None:
    """Return (base_oid, base_datname) for a branch, or None if no base set."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT vd.base_oid, pg.datname
                FROM fld_databases vd
                LEFT JOIN pg_database pg ON pg.oid = vd.base_oid
                WHERE vd.datname = %s AND vd.type = 'branch'
            """, (branch_name,))
            row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return row[0], row[1]


def drop_base_for_branch(branch_name: str) -> str | None:
    """Drop the base snapshot associated with a branch (after a successful merge).
    Returns the base name dropped, or None if there was no base.
    """
    base = get_branch_base(branch_name)
    if base is None:
        return None
    base_oid, base_name = base
    if base_name is None:
        return None
    # DROP DATABASE
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{base_name}"')
    # Remove fld_databases entries
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fld_databases WHERE oid = %s", (base_oid,))
            cur.execute(
                "UPDATE fld_databases SET base_oid = NULL WHERE datname = %s",
                (branch_name,),
            )
        conn.commit()
    return base_name


def create_branch_database(source_database: str, branch_name: str,
                           template: str | None = None) -> tuple[str, int]:
    """Create a new database for branch with user-provided branch name."""
    # TODO: think. we might add a prefix, though git doesnt add any prefix.
    branch_database_name = f"{branch_name}"

    with connect() as conn:
        # Set autocommit for CREATE DATABASE (required for PostgreSQL)
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            cur.execute(_create_database_sql(branch_database_name, pg_version, template))
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (branch_database_name,))
            oid = cur.fetchone()[0]

    return branch_database_name, oid


_REFLINK_SUPPORT_CACHE: dict[str, bool] = {}


def supports_reflink(probe_dir: Path) -> bool:
    """Return True if `probe_dir`'s filesystem supports reflink copies.

    Probes by writing a 1-byte file and trying `cp --reflink=always` on it
    inside `probe_dir`. Result is cached per directory because the answer
    is fixed for the lifetime of a process. Cheap (sub-ms) on the cold path.

    Linux-only — on macOS we use `cp -cR` (clonefile) which is supported
    on every APFS volume, so we don't probe.
    """
    if platform.system() == "Darwin":
        return True
    key = str(probe_dir)
    cached = _REFLINK_SUPPORT_CACHE.get(key)
    if cached is not None:
        return cached
    src = probe_dir / f".fld_cow_probe_src_{os.getpid()}"
    dst = probe_dir / f".fld_cow_probe_dst_{os.getpid()}"
    try:
        src.write_bytes(b"x")
        result = subprocess.run(
            ["cp", "--reflink=always", str(src), str(dst)],
            capture_output=True,
        )
        supported = result.returncode == 0
    except Exception:
        supported = False
    finally:
        for p in (src, dst):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    _REFLINK_SUPPORT_CACHE[key] = supported
    return supported


def copy_database_files(data_directory: str, source_oid: int, target_oid: int) -> None:
    """Copy database files from source to target using OIDs.

    Copy strategy:
      - FLD_NOCOW=1 forces plain `cp -r` (escape hatch).
      - macOS: always `cp -cR` (APFS clonefile).
      - Linux: probe the destination filesystem. If it supports reflinks
        (btrfs, xfs+reflink), use `cp -R --reflink=always` and refuse to
        fall back — silent fallback to a full copy on a CoW-capable FS is
        a real bug (it'd quietly burn gigabytes). If the FS does NOT
        support reflinks (overlay/tmpfs), drop to plain `cp -r`.
      - FLD_COW_STRICT=1 disables even the non-CoW fallback, so a missing
        reflink capability surfaces as an error from a test's perspective.
    """
    data_path = Path(data_directory)
    base_path = data_path / "base"

    source_path = base_path / str(source_oid)
    target_path = base_path / str(target_oid)

    if not source_path.exists():
        raise FileNotFoundError(f"Source database directory not found: {source_path}")

    if not target_path.exists():
        raise FileNotFoundError(f"Target database directory not found: {target_path}")

    use_nocow = os.getenv("FLD_NOCOW") is not None
    strict_cow = os.getenv("FLD_COW_STRICT") is not None

    subprocess.run(
        ["rm", "-rf", str(target_path) + "/"],
        check=True,
        capture_output=True,
        text=True
    )

    src_arg = str(source_path) + "/"
    dst_arg = str(target_path) + "/"

    if use_nocow:
        subprocess.run(
            ["cp", "-r", src_arg, dst_arg],
            check=True, capture_output=True, text=True,
        )
    elif platform.system() == "Darwin":
        subprocess.run(
            ["cp", "-cR", src_arg, dst_arg],
            check=True, capture_output=True, text=True,
        )
    else:
        # Linux: probe and branch. Capability is fixed per-FS, so a
        # single probe is enough to commit to one path.
        cow_capable = supports_reflink(base_path)
        if cow_capable or strict_cow:
            # CoW-capable FS — reflink must succeed. We do NOT fall back:
            # a failure here means something's wrong with the FS state
            # (e.g., a NOCOW attribute, a quota issue) and silently doing
            # a full copy would copy potentially gigabytes.
            subprocess.run(
                ["cp", "-R", "--reflink=always", src_arg, dst_arg],
                check=True, capture_output=True, text=True,
            )
        else:
            # FS truly doesn't support reflinks (overlay, tmpfs, ext4
            # without reflink). Plain copy is the only correct option.
            subprocess.run(
                ["cp", "-r", src_arg, dst_arg],
                check=True, capture_output=True, text=True,
            )

    # Remove pg_internal.init file from the copied directory. It's
    # regenerated on first connection from the relmap files; carrying a
    # stale copy across a clone can confuse the new database. The file is
    # normally present in a live PGDATA but may legitimately be missing
    # right after `CREATE DATABASE STRATEGY=FILE_COPY` (PG hasn't started
    # the new DB yet to populate it).
    pg_internal_init = target_path / "pg_internal.init"
    if pg_internal_init.exists():
        pg_internal_init.unlink()
    else:
        logger.debug(
            "pg_internal.init not present in cloned target %s (normal for "
            "a freshly-created FILE_COPY target)", target_path,
        )


def restore_database_from_snapshot(database_name: str, snapshot_name: str) -> dict:
    """Restore a database's physical files from a snapshot database.

    Steps:
    - Ensure the snapshot exists and belongs to the given database.
    - Terminate all connections to the source database and acquire a write lock.
    - Move the source database's data directory to a backup prefixed with `fld_delete_`.
    - Drop the source database and recreate a new one with the same name using STRATEGY='FILE_COPY'.
    - Copy the snapshot's data directory into the new database OID path using copy-on-write.
    - Remove `pg_internal.init` from the restored directory after copy.
    - Verify connectivity and that at least one table exists in the restored database.
    - Log the restore operation and update the database status to 'restored'.

    Returns a dict with keys: `source_oid`, `snapshot_oid`, `restored_oid`, `backup_path`, `tables_count`.
    """
    # Resolve OIDs and paths
    source_oid = get_database_oid(database_name)
    snapshot_record = get_snapshot_record(snapshot_name)
    if snapshot_record is None:
        raise ValueError(f"Snapshot '{snapshot_name}' not found in metadata")

    snapshot_oid = snapshot_record['oid']
    parent_oid = snapshot_record['parent']
    if parent_oid != source_oid:
        raise ValueError(
            f"Snapshot '{snapshot_name}' does not belong to database '{database_name}'"
        )

    # Start logging the restore operation
    log_id = log_restore_operation(source_oid, None, database_name, "restore", "started")

    try:
        data_directory = get_data_directory()
        base_path = Path(data_directory) / "base"
        source_path = base_path / str(source_oid)
        snapshot_path = base_path / str(snapshot_oid)

        if not source_path.exists():
            raise FileNotFoundError(f"Source data directory not found: {source_path}")
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot data directory not found: {snapshot_path}")

        # Prepare a unique backup directory name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = base_path / f"fld_delete_{source_oid}_{timestamp}"

        # Decide once whether to let PG clone the snapshot itself.
        with connect() as conn:
            use_native = should_use_native_clone(conn)

        # Safety: terminate connections, wait for them to actually drop,
        # then CHECKPOINT before moving files. Restore is destructive by
        # nature so we kill connections unconditionally (no --force gate);
        # the user knows they're rebuilding the database.
        terminate_database_connections(database_name)
        try:
            wait_for_no_connections(database_name, timeout_s=10.0)
        except ActiveConnectionsTimeout as exc:
            raise RuntimeError(
                f"could not terminate connections to '{database_name}' "
                f"before restore: {len(exc.connections)} still attached "
                f"after {exc.timeout_s}s"
            ) from exc
        with _connect_postgres_db() as ckpt_conn:
            with ckpt_conn.cursor() as ckpt_cur:
                ckpt_cur.execute("CHECKPOINT")
        subprocess.run(
            ["mv", str(source_path), str(backup_path)],
            check=True, capture_output=True, text=True,
        )

        # Drop and recreate the database. On PG 18+clone, we recreate it
        # directly from the snapshot via TEMPLATE — PG handles the reflinks.
        # On older PG / non-clone, we create empty then overlay with cp.
        drop_database(database_name)
        if use_native:
            create_database_with_strategy(database_name,
                                          template=snapshot_name)
        else:
            create_database_with_strategy(database_name)
        restored_oid = get_database_oid(database_name)

        # Update the log with the new OID
        update_restore_log(log_id, "in_progress")

        if not use_native:
            # Copy from snapshot OID directory to the new database OID directory
            copy_database_files(data_directory, snapshot_oid, restored_oid)

        # Post-restore validation: can connect and tables exist
        tables_count = 0
        db_dsn = get_database_dsn()
        params = psycopg.conninfo.conninfo_to_dict(db_dsn)
        params['dbname'] = database_name
        target_dsn = psycopg.conninfo.make_conninfo(**params)
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
                row = cur.fetchone()
                if row is not None:
                    tables_count = int(row[0])
        
        # Update database status to 'restored' after successful validation
        update_database_status(restored_oid, 'restored')
        
        # Update log with success
        update_restore_log(log_id, "success")
        
        return {
            "source_oid": source_oid,
            "snapshot_oid": snapshot_oid,
            "restored_oid": restored_oid,
            "backup_path": str(backup_path),
            "tables_count": tables_count,
        }
        
    except Exception as e:
        # Update log with error
        update_restore_log(log_id, "error", str(e))
        raise


def database_exists(database_name: str) -> bool:
    """Check if a database exists."""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
                return cur.fetchone() is not None
    except Exception:
        return False


def create_database(database_name: str) -> None:
    """Create a database."""
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{database_name}"')

def create_database_with_strategy(database_name: str,
                                  template: str | None = None) -> None:
    """Create a database, optionally cloning from `template`.

    Used by the restore path. Same dispatch rule as `_create_database_sql`:
    `template` set → STRATEGY=FILE_COPY (PG-native clone, fast on PG 18 +
    file_copy_method=clone); otherwise plain CREATE DATABASE (defaults to
    WAL_LOG on PG 15+, single allowed option pre-15).
    """
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            cur.execute(_create_database_sql(database_name, pg_version, template))


def table_exists(table_name: str, database_name: str = "foldout") -> bool:
    """Check if a table exists in the specified database."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    try:
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = %s AND table_schema = 'public'
                """, (table_name,))
                return cur.fetchone() is not None
    except Exception:
        return False


def get_current_version(database_name: str = "foldout") -> str:
    """Get the current migration version from fld_dbversion table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    try:
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM fld_dbversion LIMIT 1")
                result = cur.fetchone()
                return result[0] if result else '0'
    except Exception:
        return '0'


def get_latest_migration_version() -> int:
    """Get the latest migration version from migration files."""
    migration_dir = Path(__file__).parent / "migration"
    if not migration_dir.exists():
        return 0
    
    versions = []
    for file in migration_dir.glob("foldout_*.sql"):
        match = re.search(r'foldout_(\d+)\.sql', file.name)
        if match:
            versions.append(int(match.group(1)))
    
    return max(versions) if versions else 0


def execute_migration(migration_file: Path, database_name: str = "foldout") -> None:
    """Execute a migration file against the specified database."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            with open(migration_file, 'r') as f:
                sql = f.read()
            cur.execute(sql)
        conn.commit()


def register_source_database(database_name: str, oid: int) -> None:
    """Register a source database in fld_databases table if not already exists."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Check if database already exists in fld_databases
            cur.execute("SELECT 1 FROM fld_databases WHERE oid = %s", (oid,))
            if cur.fetchone() is None:
                # Insert the source database record
                cur.execute("""
                    INSERT INTO fld_databases (oid, datname, parent, created_at, type, status) 
                    VALUES (%s, %s, NULL, %s, 'source', 'live')
                """, (oid, database_name, datetime.now()))
        conn.commit()


def register_snapshot_database(snapshot_name: str, snapshot_oid: int, parent_oid: int) -> None:
    """Register a snapshot database in fld_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Insert the snapshot database record
            cur.execute("""
                INSERT INTO fld_databases (oid, datname, parent, created_at, type, status) 
                VALUES (%s, %s, %s, %s, 'snapshot', 'live')
            """, (snapshot_oid, snapshot_name, parent_oid, datetime.now()))
        conn.commit()


def register_branch_database(branch_name: str, branch_oid: int, parent_oid: int) -> None:
    """Register a branch database in fld_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Insert the branch database record
            cur.execute("""
                INSERT INTO fld_databases (oid, datname, parent, created_at, type, status) 
                VALUES (%s, %s, %s, %s, 'branch', 'live')
            """, (branch_oid, branch_name, parent_oid, datetime.now()))
        conn.commit()


def get_databases_with_snapshots() -> dict[str, dict]:
    """Get databases from foldout metadata DB with their snapshots in parent-child relationship."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Get all databases from fld_databases
            cur.execute("""
                SELECT vd.oid, vd.datname, vd.parent, vd.created_at, vd.type, vd.status,
                       pg.datname as current_datname
                FROM fld_databases vd
                LEFT JOIN pg_database pg ON vd.oid = pg.oid
                ORDER BY vd.type, vd.created_at
            """)
            
            databases = {}
            snapshots = {}
            
            for row in cur.fetchall():
                oid, datname, parent, created_at, db_type, status, current_datname = row
                
                # Determine if database is defunct (not restored and doesn't exist in pg_database)
                if status != 'restored' and current_datname is None:
                    status = 'defunct'
                    # Update the status in the database
                    update_database_status(oid, 'defunct')
                
                # Use current database name if available, fallback to stored name
                display_name = current_datname or datname
                
                db_info = {
                    'oid': oid,
                    'stored_name': datname,
                    'current_name': display_name,
                    'parent': parent,
                    'created_at': created_at,
                    'type': db_type,
                    'status': status,
                    'snapshots': []
                }
                
                if db_type == 'source':
                    databases[oid] = db_info
                else:  # snapshot
                    snapshots[oid] = db_info
            
            # Organize snapshots under their parent databases
            for snapshot_oid, snapshot_info in snapshots.items():
                parent_oid = snapshot_info['parent']
                if parent_oid in databases:
                    databases[parent_oid]['snapshots'].append(snapshot_info)
            
            return databases


def drop_database(database_name: str) -> None:
    """Drop a database."""
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def get_snapshot_record(snapshot_name: str) -> dict | None:
    """Get snapshot record from fld_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT oid, datname, parent, created_at, type 
                FROM fld_databases 
                WHERE datname = %s AND type = 'snapshot'
            """, (snapshot_name,))
            result = cur.fetchone()
            if result:
                return {
                    'oid': result[0],
                    'datname': result[1], 
                    'parent': result[2],
                    'created_at': result[3],
                    'type': result[4]
                }
            return None


def update_database_status(oid: int, status: str) -> None:
    """Update the status of a database in fld_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fld_databases SET status = %s WHERE oid = %s", (status, oid))
        conn.commit()


def delete_database_record(database_name: str) -> None:
    """Delete a database record from fld_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM fld_databases WHERE datname = %s", (database_name,))
        conn.commit()


def log_restore_operation(old_oid: int, new_oid: int, datname: str, operation: str = "restore", status: str = "started", error_description: str = None) -> int:
    """Log a restore operation to fld_log table and return the log ID."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            if status == "started":
                cur.execute("""
                    INSERT INTO fld_log (old_oid, new_oid, datname, operation, created_at, started_at, status) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (old_oid, new_oid, datname, operation, datetime.now(), datetime.now(), status))
            else:
                cur.execute("""
                    INSERT INTO fld_log (old_oid, new_oid, datname, operation, created_at, status, error_description) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (old_oid, new_oid, datname, operation, datetime.now(), status, error_description))
            log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def log_branch_operation(source_oid: int, branch_oid: int, branch_name: str, operation: str = "branch", status: str = "success") -> int:
    """Log a branch creation operation to fld_log table and return the log ID."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO fld_log (old_oid, new_oid, datname, operation, created_at, started_at, finished_at, status) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (source_oid, branch_oid, branch_name, operation, datetime.now(), datetime.now(), datetime.now(), status))
            log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def update_restore_log(log_id: int, status: str, error_description: str = None) -> None:
    """Update a restore operation log entry."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "foldout"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE fld_log 
                SET finished_at = %s, status = %s, error_description = %s 
                WHERE id = %s
            """, (datetime.now(), status, error_description, log_id))
        conn.commit()


def initialize_database() -> None:
    """Initialize the foldout database and run migrations."""
    # Check if foldout database exists, create if not
    if not database_exists("foldout"):
        create_database("foldout")

    migration_dir = Path(__file__).parent / "migration"

    # Bootstrap on a brand-new install: foldout_1.sql creates fld_dbversion
    # itself, so we must run it before get_current_version() can succeed.
    if not table_exists("fld_dbversion"):
        initial_migration = migration_dir / "foldout_1.sql"
        if initial_migration.exists():
            execute_migration(initial_migration)

    # Apply every migration strictly newer than the recorded version. This
    # path also catches fresh installs (they're at version 1 after the
    # bootstrap above) so we don't need an early return that would leave
    # later migrations unapplied.
    current_version = int(get_current_version())
    latest_version = get_latest_migration_version()

    if current_version < latest_version:
        for version in range(current_version + 1, latest_version + 1):
            migration_file = migration_dir / f"foldout_{version}.sql"
            if migration_file.exists():
                execute_migration(migration_file)
