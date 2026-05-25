"""Pytest fixtures for foldout integration tests against live PostgreSQL.

These tests are opt-in: run them with `uv run pytest -m integration`.
The default `pytest` invocation skips them so the existing unit suite
stays fast and doesn't require downloading PG binaries.
"""

from __future__ import annotations

import os
from typing import Iterator

import pytest

from .pg_cluster import PgCluster
from .pg_runtime import PgBinary, PgBinaryManager


PG_TEST_MATRIX: tuple[int, ...] = (16, 17)


@pytest.fixture(scope="session")
def pg_binary_manager() -> PgBinaryManager:
    return PgBinaryManager()


@pytest.fixture(scope="session", params=PG_TEST_MATRIX, ids=lambda v: f"pg{v}")
def pg_binary(request: pytest.FixtureRequest,
              pg_binary_manager: PgBinaryManager) -> PgBinary:
    """Download (if needed) and return a PG binary for the requested major."""
    return pg_binary_manager.ensure(request.param)


@pytest.fixture
def pg_cluster(pg_binary: PgBinary) -> Iterator[PgCluster]:
    """A fresh per-test PostgreSQL cluster on a btrfs-backed PGDATA."""
    cluster = PgCluster(pg_binary)
    cluster.initdb()
    cluster.start()
    try:
        yield cluster
    finally:
        cluster.destroy()


@pytest.fixture
def foldout_env(pg_cluster: PgCluster,
                monkeypatch: pytest.MonkeyPatch) -> PgCluster:
    """Set FLD_DATABASE / FLD_PG_DATA_PATH to point at the live cluster.

    Foldout normally runs against a remote PG, but for snapshot/branch
    operations it needs filesystem-level access to PGDATA. Here CLI and
    server share a host (the test process), so both env vars point at
    the same paths.
    """
    monkeypatch.setenv("FLD_DATABASE", pg_cluster.dsn(database="postgres"))
    monkeypatch.setenv("FLD_PG_DATA_PATH", str(pg_cluster.pgdata))
    # Make sure no NOCOW override leaks in from the parent shell.
    monkeypatch.delenv("FLD_NOCOW", raising=False)
    return pg_cluster
