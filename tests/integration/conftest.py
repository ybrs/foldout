"""Pytest fixtures for foldout integration tests against live PostgreSQL.

These tests are opt-in: run them with `uv run pytest -m integration`.
The default `pytest` invocation skips them so the existing unit suite
stays fast and doesn't require downloading PG binaries.

Cluster lifecycle (two paths):
- **Spawn mode (default)**: one long-lived cluster per `ClusterVariant`
  per pytest session. Initdb + pg_ctl start happens once at session
  start; the cluster is destroyed at session end.
- **Attach mode (when `scripts/run-harness.py start` has been run)**:
  the harness writes `.test-harness.json` at the repo root listing the
  variant → (pgdata, port) mappings. We attach to those instead of
  spawning, and skip teardown so the harness survives.

Between tests we drop every non-system DB via the autouse
`_clean_between_tests` fixture, so individual tests still see a
pristine cluster — they just don't pay the initdb cost.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

import psycopg
import pytest

from .clusters import VARIANTS, ClusterVariant
from .pg_cluster import PgCluster
from .pg_runtime import PgBinary, PgBinaryManager


REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS_STATE_FILE = REPO_ROOT / ".test-harness.json"

_SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1"})


def _read_harness_state() -> dict[str, dict[str, Any]]:
    """Return the harness's state dict, or empty if no harness is running."""
    if not HARNESS_STATE_FILE.exists():
        return {}
    with open(HARNESS_STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _variant_id(variant: ClusterVariant) -> str:
    """Render a pytest parametrize id from a ClusterVariant."""
    return variant.name


@pytest.fixture(scope="session")
def pg_binary_manager() -> PgBinaryManager:
    """Session-wide binary-cache manager for theseus-rs PG bundles."""
    return PgBinaryManager()


@pytest.fixture(scope="session", params=VARIANTS, ids=_variant_id)
def cluster_variant(request: pytest.FixtureRequest) -> ClusterVariant:
    """The variant a parametrized test is currently running against."""
    return request.param


@pytest.fixture(scope="session")
def pg_binary(cluster_variant: ClusterVariant,
              pg_binary_manager: PgBinaryManager) -> PgBinary:
    """Download (if needed) and return the PG binary for the active variant."""
    return pg_binary_manager.ensure(cluster_variant.pg_major)


@pytest.fixture(scope="session")
def pg_cluster(cluster_variant: ClusterVariant,
               pg_binary: PgBinary) -> Iterator[PgCluster]:
    """One long-lived PG cluster per variant, shared across the whole session.

    If `scripts/run-harness.py start` was run (and its state file exists
    + lists this variant), we attach to that existing cluster instead of
    spawning a fresh one — and `destroy()` becomes a no-op so the harness
    survives the session.

    Tests should NOT initdb / start / stop this cluster themselves — they
    just use it. Per-test isolation comes from `_clean_between_tests`.
    """
    state = _read_harness_state()
    record = state.get(cluster_variant.name)
    if record is not None:
        cluster = PgCluster.attach(
            pg_binary,
            pgdata=Path(record["pgdata"]),
            port=int(record["port"]),
        )
        try:
            yield cluster
        finally:
            cluster.destroy()  # no-op in attach mode
        return

    cluster = PgCluster(pg_binary, extra_conf=cluster_variant.extra_conf)
    cluster.initdb()
    cluster.start()
    try:
        yield cluster
    finally:
        cluster.destroy()


def _drop_user_databases(cluster: PgCluster) -> None:
    """Drop every database except postgres/template0/template1.

    Called after each test so the next one sees a clean cluster. We
    terminate any lingering backends first so the DROP can't fail with
    "database is being accessed by other users".
    """
    with psycopg.connect(cluster.dsn(database="postgres"),
                         autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT datname FROM pg_database")
            names: list[str] = []
            for row in cur.fetchall():
                names.append(row[0])
        for name in names:
            if name in _SYSTEM_DATABASES:
                continue
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')


@pytest.fixture(autouse=True)
def _clean_between_tests(request: pytest.FixtureRequest) -> Iterator[None]:
    """Drop all non-system DBs after each integration test.

    Autouse so individual tests don't need to opt in — but the body only
    runs for tests that actually touched a `pg_cluster` (i.e. when the
    fixture is in scope). Non-cluster tests are unaffected.
    """
    yield
    if "pg_cluster" not in request.fixturenames:
        return
    cluster = request.getfixturevalue("pg_cluster")
    _drop_user_databases(cluster)


@pytest.fixture
def foldout_env(pg_cluster: PgCluster, tmp_path,
                monkeypatch: pytest.MonkeyPatch) -> PgCluster:
    """Point foldout at the shared cluster + isolate `~/.foldout` per test.

    Foldout normally runs against a remote PG, but for snapshot/branch
    operations it needs filesystem-level access to PGDATA. Here CLI and
    server share a host (the test process), so both env vars point at
    the same paths. `HOME` is redirected to `tmp_path` so each test's
    `~/.foldout/snapshots/` is fresh.
    """
    monkeypatch.setenv("FLD_DATABASE", pg_cluster.dsn(database="postgres"))
    monkeypatch.setenv("FLD_PG_DATA_PATH", str(pg_cluster.pgdata))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FLD_NOCOW", raising=False)
    return pg_cluster
