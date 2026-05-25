"""End-to-end test: source DB -> foldout snapshot -> mutate -> restore.

Runs once per major in the matrix (PG 16, 17, 18). Verifies that:
- foldout's CLI can drive a live cluster
- the COW copy (`cp --reflink=always` on btrfs) succeeds
- a restore brings the source back to the snapshot's row contents
"""

from __future__ import annotations

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
    """Create SOURCE_DB on `cluster` and populate it with a tiny `items` table."""
    cluster.create_database(SOURCE_DB)
    cluster.psql(
        "CREATE TABLE items (id int primary key, label text); "
        "INSERT INTO items VALUES (1,'one'),(2,'two'),(3,'three');",
        database=SOURCE_DB,
    )


def _labels(cluster: PgCluster, database: str) -> list[str]:
    """Return the `label` column of `items` from `database`, ordered by id."""
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT label FROM items ORDER BY id")
            out: list[str] = []
            for row in cur.fetchall():
                out.append(row[0])
            return out


def _parse_snapshot_name(cli_output: str) -> str:
    """Extract the snapshot DB name from `foldout snapshot`'s stdout."""
    match = re.search(r"Created snapshot database '([^']+)'", cli_output)
    if match is None:
        raise AssertionError(
            f"Couldn't find snapshot name in CLI output:\n{cli_output}"
        )
    return match.group(1)


_FILEFRAG_EXTENT_RE = re.compile(
    r"^\s*\d+:\s*\d+\.\.\s*\d+:\s*(\d+)\.\.\s*(\d+):\s*(\d+):"
)
_FILEFRAG_HEADER_RE = re.compile(
    r"\((\d+)\s+blocks?\s+of\s+(\d+)\s+bytes\)"
)


def _filefrag(file_path: Path) -> tuple[set[tuple[int, int]], int, int]:
    """Run `filefrag -v` and return (extents, block_count, block_size).

    extents: set of (physical_start_block, physical_end_block).
    block_count, block_size: from the header line, e.g. "(25 blocks of 4096 bytes)".
    On btrfs, very small files may be stored inline (no extents); we return
    an empty set with block_size from the header so callers can still reason
    about their on-disk footprint (effectively 0 for inline).
    """
    result = subprocess.run(
        ["filefrag", "-v", str(file_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    extents: set[tuple[int, int]] = set()
    block_count = 0
    block_size = 4096
    for line in result.stdout.splitlines():
        header = _FILEFRAG_HEADER_RE.search(line)
        if header:
            block_count = int(header.group(1))
            block_size = int(header.group(2))
        match = _FILEFRAG_EXTENT_RE.match(line)
        if match:
            extents.add((int(match.group(1)), int(match.group(2))))
    return extents, block_count, block_size


def _physical_extents(file_path: Path) -> set[tuple[int, int]]:
    """Convenience wrapper: just the extent set, no header info."""
    extents, _, _ = _filefrag(file_path)
    return extents


def _unshared_bytes(src_file: Path, snap_file: Path) -> int:
    """Bytes in `snap_file`'s extents that are NOT shared with `src_file`.

    After a reflink, every extent in the destination shares its physical
    range with one in the source, so the result is 0. After a full copy
    (silent fallback), the destination has its own brand-new extents and
    the result equals the file's on-disk size.
    """
    src_extents, _, _ = _filefrag(src_file)
    snap_extents, snap_blocks, snap_block_size = _filefrag(snap_file)
    unique = snap_extents - src_extents
    if not unique:
        return 0
    total_blocks = 0
    for start, end in unique:
        total_blocks += end - start + 1
    # Cap at the file's reported on-disk block count — filefrag can over-
    # report on btrfs preallocation, and we don't want to fabricate bytes
    # beyond what the file could possibly occupy.
    capped = min(total_blocks, snap_blocks)
    return capped * snap_block_size


def _pick_largest_relation(db_dir: Path) -> Path:
    """Return the largest numeric-named relation file in a PG database dir.

    Relation files are named by relfilenode (digits only); pg_filenode.map
    and pg_internal.init are special and not always reflinked, so we skip
    them. The biggest relation gives us the strongest extent-sharing signal.
    """
    largest: Path | None = None
    largest_size = -1
    for entry in db_dir.iterdir():
        if not entry.is_file():
            continue
        if not entry.name.isdigit():
            continue
        size = entry.stat().st_size
        if size > largest_size:
            largest_size = size
            largest = entry
    if largest is None:
        raise AssertionError(f"no relation files found under {db_dir}")
    return largest


def test_snapshot_then_restore(foldout_env: PgCluster) -> None:
    """foldout snapshot → mutate source → foldout restore → original rows back."""
    cluster = foldout_env

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


def test_filesystem_cow_probe_detects_btrfs(foldout_env: PgCluster) -> None:
    """`supports_reflink()` must return True for the btrfs-backed PGDATA."""
    from foldout.db import supports_reflink
    cluster = foldout_env
    base = cluster.pgdata / "base"
    assert supports_reflink(base) is True, (
        f"expected btrfs at {base} to support reflinks; CoW detection "
        f"returned False"
    )


def test_snapshot_uses_reflinks(foldout_env: PgCluster,
                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Strict-COW snapshot must succeed AND share extents with the source.

    Three independent checks:
      1. `FLD_COW_STRICT=1` forces `cp --reflink=always` with no fallback
         on the manual-cp paths (PG ≤ 17 and PG 18 with file_copy_method=copy).
         On pg18-clone the env var is harmless — PG takes the native path
         and `copy_database_files()` is never called.
      2. The largest relation file shares physical extents with the source
         (per-file proof that the copy was a reflink).
      3. The TOTAL bytes occupied by snapshot extents that are not shared
         with the source stays below a few MB — i.e. no silent fallback to
         a full bytewise copy that would have duplicated megabytes/GBs.
    """
    cluster = foldout_env
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
    src_dir = base / str(src_oid)
    snap_dir = base / str(snap_oid)

    # Check 2: per-file reflink proof on the largest relation.
    src_file = _pick_largest_relation(src_dir)
    snap_file = snap_dir / src_file.name
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

    # Check 3: aggregate disk-usage proof across every file in the snapshot.
    # If we'd silently fallen back to `cp -r`, every snapshot file would
    # have its own brand-new extents and `unshared_total` would equal the
    # full source size (tens of MB). Reflinks bring it down to just the
    # catalog files PG's FILE_COPY strategy rewrites — well under 5 MB.
    total_src_logical = 0
    unshared_total = 0
    files_compared = 0
    for snap_entry in snap_dir.iterdir():
        if not snap_entry.is_file():
            continue
        src_entry = src_dir / snap_entry.name
        if not src_entry.exists():
            # Snapshot has files the source doesn't — count them in full
            # since they cannot be reflinks (no source extents to share).
            _, blocks, bsize = _filefrag(snap_entry)
            unshared_total += blocks * bsize
            continue
        total_src_logical += src_entry.stat().st_size
        unshared_total += _unshared_bytes(src_entry, snap_entry)
        files_compared += 1

    assert files_compared > 0, "no files to compare — snapshot dir was empty?"
    threshold = 5 * 1024 * 1024  # 5 MB
    assert unshared_total < threshold, (
        f"silent fallback suspected: {unshared_total} bytes of snapshot "
        f"extents are NOT shared with source (source total = "
        f"{total_src_logical} bytes across {files_compared} files). "
        f"Threshold for legitimate catalog rewrites: {threshold} bytes."
    )
