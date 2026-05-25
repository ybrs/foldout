"""PG 18 `file_copy_method` paths.

PostgreSQL 18 introduced the `file_copy_method` GUC. With value `clone`,
`CREATE DATABASE ... STRATEGY = FILE_COPY` uses kernel reflink syscalls
itself — so foldout's manual `cp --reflink=always` step becomes redundant.

These two tests pin the contract:

1. `file_copy_method = clone`: foldout takes the native-clone path,
   skipping the manual copy. The output never says "Database files
   copied successfully"; it says "Database files cloned by PostgreSQL".
2. `file_copy_method = copy` (the PG 18 default): foldout warns and
   falls back to the manual reflink path. The output contains the
   warning AND "Database files copied successfully".

In both cases the resulting snapshot must share physical extents with
the source (reflinks happened one way or the other).
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from foldout.cli import cli

from .pg_cluster import PgCluster
from .pg_runtime import PgBinaryManager
from .test_snapshot_restore import (
    SOURCE_DB,
    _physical_extents,
    _pick_largest_relation,
    _parse_snapshot_name,
    _seed_source,
    _unshared_bytes,
)


pytestmark = pytest.mark.integration


def _make_pg18_cluster(file_copy_method: str) -> PgCluster:
    """Build a PG 18 cluster with `file_copy_method` set in postgresql.conf."""
    binary = PgBinaryManager().ensure(18)
    cluster = PgCluster(
        binary,
        extra_conf={"file_copy_method": f"'{file_copy_method}'"},
    )
    cluster.initdb()
    cluster.start()
    return cluster


def _largest_relation_oid(cluster: PgCluster, database: str) -> int:
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (database,))
            return int(cur.fetchone()[0])


def test_pg18_native_clone(tmp_path: Path,
                           monkeypatch: pytest.MonkeyPatch) -> None:
    """file_copy_method=clone → foldout uses PG's CREATE DATABASE TEMPLATE."""
    cluster = _make_pg18_cluster("clone")
    try:
        monkeypatch.setenv("FLD_DATABASE", cluster.dsn(database="postgres"))
        monkeypatch.setenv("FLD_PG_DATA_PATH", str(cluster.pgdata))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("FLD_NOCOW", raising=False)

        _seed_source(cluster)
        cluster.psql(
            "INSERT INTO items SELECT g, 'row-' || g "
            "FROM generate_series(100, 50000) AS g;",
            database=SOURCE_DB,
        )
        cluster.psql("CHECKPOINT;", database=SOURCE_DB)

        src_oid = _largest_relation_oid(cluster, SOURCE_DB)

        runner = CliRunner()
        result = runner.invoke(cli, ["snapshot", SOURCE_DB],
                               catch_exceptions=False)
        assert result.exit_code == 0, result.output

        # Native-clone path signal — these strings are the load-bearing
        # contract that the manual cp didn't run.
        assert "file_copy_method='clone'" in result.output, result.output
        assert "cloned by PostgreSQL" in result.output, result.output
        assert "Database files copied successfully" not in result.output, (
            f"native-clone path should have skipped the manual cp; "
            f"output suggests otherwise:\n{result.output}"
        )

        # And reflinks must still be measurable — PG's clone uses the same
        # kernel ioctl as our manual cp, so extents are shared.
        snapshot_name = _parse_snapshot_name(result.output)
        snap_oid = _largest_relation_oid(cluster, snapshot_name)

        base = cluster.pgdata / "base"
        src_file = _pick_largest_relation(base / str(src_oid))
        snap_file = base / str(snap_oid) / src_file.name
        assert snap_file.exists(), f"expected file at {snap_file}"

        src_extents = _physical_extents(src_file)
        snap_extents = _physical_extents(snap_file)
        assert src_extents and (src_extents & snap_extents), (
            f"PG native clone produced no shared extents — "
            f"src={src_extents}, snap={snap_extents}"
        )
    finally:
        cluster.destroy()


def test_pg18_warns_and_falls_back_when_method_is_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """file_copy_method=copy (PG 18 default) → warn, then manual reflink."""
    cluster = _make_pg18_cluster("copy")
    try:
        monkeypatch.setenv("FLD_DATABASE", cluster.dsn(database="postgres"))
        monkeypatch.setenv("FLD_PG_DATA_PATH", str(cluster.pgdata))
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("FLD_COW_STRICT", "1")
        monkeypatch.delenv("FLD_NOCOW", raising=False)

        _seed_source(cluster)
        cluster.psql(
            "INSERT INTO items SELECT g, 'row-' || g "
            "FROM generate_series(100, 50000) AS g;",
            database=SOURCE_DB,
        )
        cluster.psql("CHECKPOINT;", database=SOURCE_DB)

        src_oid = _largest_relation_oid(cluster, SOURCE_DB)

        runner = CliRunner()
        result = runner.invoke(cli, ["snapshot", SOURCE_DB],
                               catch_exceptions=False)
        assert result.exit_code == 0, result.output

        # The warning must appear and the manual path must have been used.
        assert re.search(r"WARNING.*file_copy_method='copy'", result.output), (
            f"expected PG 18 + non-clone warning in output:\n{result.output}"
        )
        assert "Falling back to the manual reflink copy path" in result.output, (
            f"warning text missing fallback note:\n{result.output}"
        )
        assert "Database files copied successfully" in result.output, (
            f"manual copy path should have run on fallback:\n{result.output}"
        )
        assert "cloned by PostgreSQL" not in result.output

        # Reflinks must still hold via the manual path (FLD_COW_STRICT
        # would have aborted the snapshot otherwise).
        snapshot_name = _parse_snapshot_name(result.output)
        snap_oid = _largest_relation_oid(cluster, snapshot_name)
        base = cluster.pgdata / "base"
        src_file = _pick_largest_relation(base / str(src_oid))
        snap_file = base / str(snap_oid) / src_file.name
        unshared = _unshared_bytes(src_file, snap_file)
        assert unshared == 0, (
            f"manual reflink path leaked {unshared} unshared bytes on the "
            f"largest relation — reflinks didn't take effect"
        )
    finally:
        cluster.destroy()
