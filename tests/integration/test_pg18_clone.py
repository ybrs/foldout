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

We pin each test to its matching cluster variant (`pg18-clone` and
`pg18-default`) and skip on the other parametrized variants — the
shared session-scoped clusters mean we don't pay extra setup cost.
"""

from __future__ import annotations

import re

import psycopg
import pytest
from click.testing import CliRunner

from foldout.cli import cli

from .clusters import ClusterVariant
from .pg_cluster import PgCluster
from .test_snapshot_restore import (
    SOURCE_DB,
    _physical_extents,
    _pick_largest_relation,
    _parse_snapshot_name,
    _seed_source,
    _unshared_bytes,
)


pytestmark = pytest.mark.integration


def _require_variant(variant: ClusterVariant, expected_name: str) -> None:
    """Skip the current test unless we're running on the expected variant."""
    if variant.name != expected_name:
        pytest.skip(f"only runs on cluster variant '{expected_name}'")


def _database_oid(cluster: PgCluster, database: str) -> int:
    """Return the OID of `database` on `cluster`."""
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s",
                        (database,))
            return int(cur.fetchone()[0])


def test_pg18_native_clone(foldout_env: PgCluster,
                           cluster_variant: ClusterVariant) -> None:
    """file_copy_method=clone → foldout uses PG's CREATE DATABASE TEMPLATE.

    Asserts the output strings that mark the native path, and verifies
    PG's own clone produced reflink-shared extents.
    """
    _require_variant(cluster_variant, "pg18-clone")
    cluster = foldout_env

    _seed_source(cluster)
    cluster.psql(
        "INSERT INTO items SELECT g, 'row-' || g "
        "FROM generate_series(100, 50000) AS g;",
        database=SOURCE_DB,
    )
    cluster.psql("CHECKPOINT;", database=SOURCE_DB)

    src_oid = _database_oid(cluster, SOURCE_DB)

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

    # Reflinks must still be measurable — PG's clone uses the same
    # kernel ioctl as our manual cp, so extents are shared.
    snapshot_name = _parse_snapshot_name(result.output)
    snap_oid = _database_oid(cluster, snapshot_name)

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


def test_pg18_warns_and_falls_back_when_method_is_copy(
    foldout_env: PgCluster,
    cluster_variant: ClusterVariant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """file_copy_method=copy (PG 18 default) → warn, then manual reflink."""
    _require_variant(cluster_variant, "pg18-default")
    cluster = foldout_env
    monkeypatch.setenv("FLD_COW_STRICT", "1")

    _seed_source(cluster)
    cluster.psql(
        "INSERT INTO items SELECT g, 'row-' || g "
        "FROM generate_series(100, 50000) AS g;",
        database=SOURCE_DB,
    )
    cluster.psql("CHECKPOINT;", database=SOURCE_DB)

    src_oid = _database_oid(cluster, SOURCE_DB)

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
    snap_oid = _database_oid(cluster, snapshot_name)
    base = cluster.pgdata / "base"
    src_file = _pick_largest_relation(base / str(src_oid))
    snap_file = base / str(snap_oid) / src_file.name
    unshared = _unshared_bytes(src_file, snap_file)
    assert unshared == 0, (
        f"manual reflink path leaked {unshared} unshared bytes on the "
        f"largest relation — reflinks didn't take effect"
    )
