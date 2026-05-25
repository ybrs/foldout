"""End-to-end test: source DB -> foldout snapshot -> mutate -> restore.

Runs once per major in the matrix (PG 16, PG 17 today). Verifies that:
- foldout's CLI can drive a live cluster
- the COW copy (`cp --reflink=always` on btrfs) succeeds
- a restore brings the source back to the snapshot's row contents
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from foldout.cli import cli

from .pg_cluster import PgCluster


pytestmark = pytest.mark.integration


SOURCE_DB = "appdb"


def _seed_source(cluster: PgCluster) -> None:
    cluster.create_database(SOURCE_DB)
    cluster.psql(
        "CREATE TABLE items (id int primary key, label text); "
        "INSERT INTO items VALUES (1,'one'),(2,'two'),(3,'three');",
        database=SOURCE_DB,
    )


def _row_count(cluster: PgCluster, database: str) -> int:
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM items")
            return int(cur.fetchone()[0])


def _labels(cluster: PgCluster, database: str) -> list[str]:
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT label FROM items ORDER BY id")
            out: list[str] = []
            for row in cur.fetchall():
                out.append(row[0])
            return out


def _parse_snapshot_name(cli_output: str) -> str:
    match = re.search(r"Created snapshot database '([^']+)'", cli_output)
    if match is None:
        raise AssertionError(
            f"Couldn't find snapshot name in CLI output:\n{cli_output}"
        )
    return match.group(1)


_FILEFRAG_EXTENT_RE = re.compile(
    r"^\s*\d+:\s*\d+\.\.\s*\d+:\s*(\d+)\.\.\s*(\d+):"
)


def _physical_extents(file_path: Path) -> set[tuple[int, int]]:
    """Return the set of (physical_start, physical_end) extents for a file.

    Uses `filefrag -v`. Two files sharing extents (reflink) will have
    identical physical extent ranges. Empty/all-zero files report no
    extents on btrfs (the FS may store them inline), so callers should
    pick a non-trivial file.
    """
    result = subprocess.run(
        ["filefrag", "-v", str(file_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    extents: set[tuple[int, int]] = set()
    for line in result.stdout.splitlines():
        match = _FILEFRAG_EXTENT_RE.match(line)
        if match:
            extents.add((int(match.group(1)), int(match.group(2))))
    return extents


def _pick_largest_relation(db_dir: Path) -> Path:
    largest: Path | None = None
    largest_size = -1
    for entry in db_dir.iterdir():
        if not entry.is_file():
            continue
        # Numeric filenames are relations; pg_filenode.map / pg_internal.init
        # are special and not always reflinked.
        if not entry.name.isdigit():
            continue
        size = entry.stat().st_size
        if size > largest_size:
            largest_size = size
            largest = entry
    if largest is None:
        raise AssertionError(f"no relation files found under {db_dir}")
    return largest


def test_snapshot_then_restore(foldout_env: PgCluster,
                               tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    cluster = foldout_env
    # Isolate ~/.foldout per test run so snapshot json files don't leak.
    monkeypatch.setenv("HOME", str(tmp_path))

    _seed_source(cluster)
    assert _labels(cluster, SOURCE_DB) == ["one", "two", "three"]

    runner = CliRunner()
    result = runner.invoke(cli, ["snapshot", SOURCE_DB], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    snapshot_name = _parse_snapshot_name(result.output)

    # The snapshot is a live, queryable database with the same rows.
    assert _labels(cluster, snapshot_name) == ["one", "two", "three"]

    # Mutate the source so a restore is observable.
    cluster.psql(
        "UPDATE items SET label = 'mutated' WHERE id = 2; "
        "DELETE FROM items WHERE id = 3;",
        database=SOURCE_DB,
    )
    assert _labels(cluster, SOURCE_DB) == ["one", "mutated"]

    # Restore wipes the source's data dir aside and reinstates the snapshot.
    result = runner.invoke(
        cli, ["snapshots", "restore", SOURCE_DB, snapshot_name],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    assert _labels(cluster, SOURCE_DB) == ["one", "two", "three"]


def test_snapshot_uses_reflinks(foldout_env: PgCluster,
                                tmp_path: Path,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict-COW snapshot must succeed AND share extents with the source.

    We assert two independent things:
      1. `FLD_COW_STRICT=1` forces `cp --reflink=always` with no fallback.
        If reflinks fail, the snapshot errors out. A clean exit means the
        FS-level CoW path was taken.
      2. The largest relation file in the snapshot shares physical extents
        with the corresponding file in the source (verified via filefrag).
        This is the definitive test that the copy was a reflink rather
        than a full bytewise copy that happened to succeed.
    """
    cluster = foldout_env
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("FLD_COW_STRICT", "1")

    _seed_source(cluster)
    # Pad with enough rows that the relation file has real on-disk extents
    # (and not the inline-data path btrfs uses for tiny files).
    cluster.psql(
        "INSERT INTO items SELECT g, 'row-' || g "
        "FROM generate_series(100, 50000) AS g;",
        database=SOURCE_DB,
    )
    cluster.psql("CHECKPOINT;", database=SOURCE_DB)

    src_oid = None
    with psycopg.connect(cluster.dsn(database="postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (SOURCE_DB,))
            src_oid = int(cur.fetchone()[0])

    runner = CliRunner()
    result = runner.invoke(cli, ["snapshot", SOURCE_DB], catch_exceptions=False)
    assert result.exit_code == 0, result.output
    snapshot_name = _parse_snapshot_name(result.output)

    with psycopg.connect(cluster.dsn(database="postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (snapshot_name,))
            snap_oid = int(cur.fetchone()[0])

    base = cluster.pgdata / "base"
    src_file = _pick_largest_relation(base / str(src_oid))
    snap_file = base / str(snap_oid) / src_file.name

    assert snap_file.exists(), f"expected reflinked file at {snap_file}"

    src_extents = _physical_extents(src_file)
    snap_extents = _physical_extents(snap_file)

    assert src_extents, f"source file {src_file} reported no extents"
    shared = src_extents & snap_extents
    assert shared, (
        f"snapshot file {snap_file} shares no physical extents with source "
        f"{src_file} (src={src_extents}, snap={snap_extents}) — reflink "
        f"did not take effect"
    )
