"""
End-to-end tests for `cross_diff_3way`.

Each scenario:
  1. Create SOURCE database, run `setup_sql`
  2. Clone SOURCE into BASE (the merge base) and BRANCH
  3. Snapshot BRANCH
  4. Run `parent_mutate_sql` on SOURCE (parent drifts independently)
  5. Run `branch_mutate_sql` on BRANCH (the user's branch work)
  6. `cross_diff_3way(SOURCE, BRANCH, BASE)` → conflicts/drifts/sql
  7. Verify counts and conflict/drift presence
  8. If no conflicts AND `expect_converges`: apply SQL to SOURCE,
     then check SOURCE matches an INDEPENDENTLY-CONSTRUCTED expected DB
     (BASE + parent_mutate + branch_mutate applied directly).

Run directly:    python tests/test_page_diff_3way.py
Run via pytest:  pytest tests/test_page_diff_3way.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from foldout import page_diff  # noqa: E402

USER = os.environ.get("USER", "aybarsb")
HOST = "127.0.0.1"
PG_DUMP = os.environ.get("PG_DUMP", "/opt/homebrew/opt/postgresql@17/bin/pg_dump")
PSQL = os.environ.get("PSQL", "/opt/homebrew/opt/postgresql@17/bin/psql")


def _dsn(db):
    return f"host={HOST} dbname={db} user={USER}"


def _server_pgdata():
    """Return the server's data directory.

    Honors `FLD_PG_DATA_PATH` (for container-on-host setups). Otherwise
    asks the running server via `SHOW data_directory`.
    """
    override = os.environ.get("FLD_PG_DATA_PATH")
    if override:
        return override
    with psycopg.connect(_dsn("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            return cur.fetchone()[0]


def _admin(sql):
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(sql)


def _run(db, sqls):
    with psycopg.connect(_dsn(db), autocommit=True) as conn, conn.cursor() as cur:
        for s in sqls:
            cur.execute(s)


def _checkpoint(db):
    _run(db, ["CHECKPOINT"])


def _create(name):
    _admin(f'CREATE DATABASE "{name}";')


def _drop(name):
    _admin(f'DROP DATABASE IF EXISTS "{name}";')


def _clone(src, dst):
    p1 = subprocess.Popen([PG_DUMP, "-h", HOST, "-U", USER, "-d", src],
                          stdout=subprocess.PIPE)
    p2 = subprocess.Popen(
        [PSQL, "-q", "-v", "ON_ERROR_STOP=1", _dsn(dst)],
        stdin=p1.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    p1.stdout.close()
    _, err = p2.communicate()
    p1.wait()
    if p2.returncode != 0:
        raise RuntimeError(f"clone failed: {err.decode()[:400]}")


def _content_hash(db):
    out = {}
    with psycopg.connect(_dsn(db)) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                  AND n.nspname NOT IN ('pg_catalog','information_schema')
                ORDER BY n.nspname, c.relname
            """)
            tables = cur.fetchall()
        for nsp, rel in tables:
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT md5(coalesce(string_agg(row,\'|\' ORDER BY row),\'\')) '
                    f'FROM (SELECT to_jsonb(x.*)::text AS row '
                    f'      FROM "{nsp}"."{rel}" AS x) s'
                )
                (h,) = cur.fetchone()
            out[f"{nsp}.{rel}"] = h
    return out


class Scenario3way:
    def __init__(self, name, *, setup_sql, parent_mutate_sql, branch_mutate_sql,
                 expected_counts=None, expect_conflict_kinds=None,
                 expect_drift_kinds=None, expect_converges=True):
        self.name = name
        self.setup_sql = setup_sql
        self.parent_mutate_sql = parent_mutate_sql
        self.branch_mutate_sql = branch_mutate_sql
        self.expected_counts = expected_counts or {}
        self.expect_conflict_kinds = expect_conflict_kinds or []
        self.expect_drift_kinds = expect_drift_kinds or []
        self.expect_converges = expect_converges

    def run(self, pgdata):
        suf = uuid.uuid4().hex[:8]
        src = f"fld_3w_src_{suf}"
        base = f"fld_3w_base_{suf}"
        tgt = f"fld_3w_tgt_{suf}"
        expected = f"fld_3w_exp_{suf}"
        snap = f"/tmp/fld_3w_{suf}.json"
        parent_snap = f"/tmp/fld_3w_{suf}_parent.json"
        try:
            for d in (src, base, tgt, expected):
                _drop(d)
            _create(src)
            _run(src, self.setup_sql)
            _checkpoint(src)

            # Clone source into BASE and BRANCH (always)
            _create(base);     _clone(src, base);     _checkpoint(base)
            _create(tgt);      _clone(src, tgt);      _checkpoint(tgt)
            # EXPECTED is only needed when we expect convergence — building
            # it by replaying both mutates can fail when the scenario tests
            # "both did the same thing" or "conflict", which is fine.
            if self.expect_converges:
                _create(expected); _clone(src, expected); _checkpoint(expected)

            page_diff.snapshot(pgdata, tgt, snap)
            # Also snapshot the parent (src) at the same instant — mirrors
            # what `vka branch` saves so 3-way can stat-skip on main side.
            page_diff.snapshot(pgdata, src, parent_snap)

            # Apply mutations
            _run(src, self.parent_mutate_sql)
            _checkpoint(src)
            _run(tgt, self.branch_mutate_sql)
            _checkpoint(tgt)
            if self.expect_converges:
                _run(expected, self.parent_mutate_sql)
                _run(expected, self.branch_mutate_sql)
                _checkpoint(expected)

            result = page_diff.cross_diff_3way(
                pgdata, src, tgt, base, snap,
                parent_snap_path=parent_snap,
                verbose=False,
            )

            # Verify counts
            for k, v in self.expected_counts.items():
                got = result.get(k, 0)
                assert got == v, (
                    f"[{self.name}] expected {k}={v}, got {got}; "
                    f"sql={result.get('sql', [])}, conflicts={result.get('conflicts')}, "
                    f"drifts={result.get('drifts')}"
                )

            # Verify conflict kinds
            got_conflict_kinds = sorted({c["kind"] for c in result["conflicts"]})
            assert got_conflict_kinds == sorted(self.expect_conflict_kinds), (
                f"[{self.name}] expected conflict kinds {self.expect_conflict_kinds}, "
                f"got {got_conflict_kinds}: {result['conflicts']}"
            )

            # Verify drift kinds (only check that the expected ones are present)
            got_drift_kinds = sorted({d["kind"] for d in result["drifts"]})
            for k in self.expect_drift_kinds:
                assert k in got_drift_kinds, (
                    f"[{self.name}] expected drift kind '{k}' in {got_drift_kinds}; "
                    f"drifts={result['drifts']}"
                )

            # If no conflicts and convergence expected: apply + hash-compare
            if self.expect_converges and not result["conflicts"]:
                if result["sql"]:
                    with psycopg.connect(_dsn(src)) as conn:
                        with conn.cursor() as cur:
                            for s in result["sql"]:
                                cur.execute(s)
                        conn.commit()
                h_src = _content_hash(src)
                h_exp = _content_hash(expected)
                assert h_src == h_exp, (
                    f"[{self.name}] post-apply hash mismatch.\n"
                    f"  src={h_src}\n  expected={h_exp}\n"
                    f"  sql={result['sql']}"
                )

            print(f"  PASS  {self.name}  "
                  f"({len(result.get('sql', []))} stmts, "
                  f"conflicts={len(result['conflicts'])}, "
                  f"drifts={len(result['drifts'])}, "
                  f"{result['elapsed_ms']:.0f} ms)")
        finally:
            for p in (snap, parent_snap):
                if os.path.exists(p):
                    os.unlink(p)
            for d in (src, base, tgt, expected):
                _drop(d)


def _row_count(db, schema, table):
    with psycopg.connect(_dsn(db)) as conn, conn.cursor() as cur:
        cur.execute(f'SELECT count(*) FROM "{schema}"."{table}"')
        return cur.fetchone()[0]


# -------------------- scenarios --------------------

SCENARIOS = [
    # The motivating case: parent adds a different table, branch adds its own.
    Scenario3way(
        name="parent-adds-t2-branch-adds-t1",
        setup_sql=[
            "CREATE TABLE base_t (id int primary key, v text)",
            "INSERT INTO base_t VALUES (1,'a')",
        ],
        parent_mutate_sql=[
            "CREATE TABLE t2_main (id int primary key, v text)",
            "INSERT INTO t2_main VALUES (10,'main')",
        ],
        branch_mutate_sql=[
            "CREATE TABLE t1_branch (id int primary key, v text)",
            "INSERT INTO t1_branch VALUES (20,'branch')",
        ],
        expected_counts={"DDL_PRE": 1, "INSERT": 1},
        expect_drift_kinds=["table"],
    ),

    # Both add columns to the same table — different columns: compatible.
    # Parent's column shows up as column-level drift.
    Scenario3way(
        name="both-add-different-columns",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b')",
        ],
        parent_mutate_sql=[
            "ALTER TABLE u ADD COLUMN parent_col int DEFAULT 0",
        ],
        branch_mutate_sql=[
            "ALTER TABLE u ADD COLUMN branch_col text",
            "UPDATE u SET branch_col = 'x' WHERE id = 1",
        ],
        expected_counts={"DDL_PRE": 1, "UPDATE": 1},
        expect_drift_kinds=["column"],
    ),

    # Both add the SAME column, same type → no-op (idempotent additions).
    # No drift either: both sides made the same change.
    Scenario3way(
        name="both-add-same-column-same-type",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=[
            "ALTER TABLE u ADD COLUMN x integer",
        ],
        branch_mutate_sql=[
            "ALTER TABLE u ADD COLUMN x integer",
        ],
        expected_counts={"DDL_PRE": 0, "INSERT": 0, "UPDATE": 0, "DELETE": 0},
        expect_converges=False,
    ),

    # Both add the same column with DIFFERENT types → conflict
    Scenario3way(
        name="both-add-same-column-different-types-conflict",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
        ],
        parent_mutate_sql=[
            "ALTER TABLE u ADD COLUMN v integer",
        ],
        branch_mutate_sql=[
            "ALTER TABLE u ADD COLUMN v text",
        ],
        expect_conflict_kinds=["column"],
        expect_converges=False,
    ),

    # Branch updates row 1, parent updates row 2 — independent rows, no conflict
    Scenario3way(
        name="parent-updates-row2-branch-updates-row1",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b'),(3,'c')",
        ],
        parent_mutate_sql=[
            "UPDATE u SET v = 'parent-2' WHERE id = 2",
        ],
        branch_mutate_sql=[
            "UPDATE u SET v = 'branch-1' WHERE id = 1",
        ],
        expected_counts={"UPDATE": 1, "INSERT": 0, "DELETE": 0},
        expect_drift_kinds=["row"],
    ),

    # Same row, same new value on both sides → no DML emitted. expect_converges
    # is True here: after both UPDATEs are applied to the expected DB
    # idempotently, src (after no-op apply) matches expected.
    Scenario3way(
        name="both-update-same-row-same-value",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=["UPDATE u SET v = 'SAME' WHERE id = 1"],
        branch_mutate_sql=["UPDATE u SET v = 'SAME' WHERE id = 1"],
        expected_counts={"UPDATE": 0, "INSERT": 0, "DELETE": 0},
    ),

    # Same row, different values → conflict
    Scenario3way(
        name="both-update-same-row-different-values-conflict",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=["UPDATE u SET v = 'main-wins' WHERE id = 1"],
        branch_mutate_sql=["UPDATE u SET v = 'branch-wins' WHERE id = 1"],
        expect_conflict_kinds=["row"],
        expect_converges=False,
    ),

    # Branch inserts row, parent inserts a different row → both INSERTs preserved
    Scenario3way(
        name="parent-inserts-row3-branch-inserts-row4",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=["INSERT INTO u VALUES (3, 'parent-new')"],
        branch_mutate_sql=["INSERT INTO u VALUES (4, 'branch-new')"],
        expected_counts={"INSERT": 1, "UPDATE": 0, "DELETE": 0},
        expect_drift_kinds=["row"],
    ),

    # Both INSERT the SAME PK with different values → conflict
    Scenario3way(
        name="both-insert-same-pk-conflict",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
        ],
        parent_mutate_sql=["INSERT INTO u VALUES (1, 'main')"],
        branch_mutate_sql=["INSERT INTO u VALUES (1, 'branch')"],
        expect_conflict_kinds=["row"],
        expect_converges=False,
    ),

    # Branch DELETEs row 1, parent untouched → DELETE emitted
    Scenario3way(
        name="branch-deletes-parent-untouched",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b'),(3,'c')",
        ],
        parent_mutate_sql=[],
        branch_mutate_sql=["DELETE FROM u WHERE id = 1"],
        expected_counts={"DELETE": 1, "INSERT": 0, "UPDATE": 0},
    ),

    # Branch DELETEs row 1, parent UPDATEd row 1 → conflict
    Scenario3way(
        name="branch-deletes-parent-updated-conflict",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=["UPDATE u SET v = 'parent-touched' WHERE id = 1"],
        branch_mutate_sql=["DELETE FROM u WHERE id = 1"],
        expect_conflict_kinds=["row"],
        expect_converges=False,
    ),

    # Both DELETE same row → agreed, no SQL emitted; both sides agree.
    Scenario3way(
        name="both-delete-same-row",
        setup_sql=[
            "CREATE TABLE u (id int primary key, v text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        parent_mutate_sql=["DELETE FROM u WHERE id = 1"],
        branch_mutate_sql=["DELETE FROM u WHERE id = 1"],
        expected_counts={"INSERT": 0, "UPDATE": 0, "DELETE": 0},
    ),

    # ---------- no-PK 3-way scenarios ----------

    # Branch inserts a row, parent untouched → INSERT applied to parent.
    Scenario3way(
        name="no-pk-branch-inserts-parent-untouched",
        setup_sql=[
            "CREATE TABLE log (msg text)",
            "INSERT INTO log VALUES ('initial')",
        ],
        parent_mutate_sql=[],
        branch_mutate_sql=["INSERT INTO log VALUES ('branch-new')"],
        expected_counts={"INSERT": 1, "DELETE": 0},
    ),

    # Branch deletes one of two duplicate rows, parent untouched → DELETE 1.
    Scenario3way(
        name="no-pk-branch-deletes-parent-untouched",
        setup_sql=[
            "CREATE TABLE log (msg text)",
            "INSERT INTO log VALUES ('a'),('a'),('b')",
        ],
        parent_mutate_sql=[],
        branch_mutate_sql=[
            "DELETE FROM log WHERE ctid = "
            "(SELECT ctid FROM log WHERE msg = 'a' LIMIT 1)",
        ],
        expected_counts={"INSERT": 0, "DELETE": 1},
    ),

    # Branch inserts row X, parent inserts a different row Y → INSERT X
    # applied to parent; Y is reported as drift.
    Scenario3way(
        name="no-pk-both-insert-different-rows",
        setup_sql=[
            "CREATE TABLE log (msg text)",
        ],
        parent_mutate_sql=["INSERT INTO log VALUES ('main-row')"],
        branch_mutate_sql=["INSERT INTO log VALUES ('branch-row')"],
        expected_counts={"INSERT": 1, "DELETE": 0},
        expect_drift_kinds=["row_no_pk"],
    ),

    # Both sides delete the same row → no SQL emitted; both agree.
    Scenario3way(
        name="no-pk-both-delete-same-row",
        setup_sql=[
            "CREATE TABLE log (msg text)",
            "INSERT INTO log VALUES ('shared')",
        ],
        parent_mutate_sql=[
            "DELETE FROM log WHERE ctid = "
            "(SELECT ctid FROM log WHERE msg = 'shared' LIMIT 1)",
        ],
        branch_mutate_sql=[
            "DELETE FROM log WHERE ctid = "
            "(SELECT ctid FROM log WHERE msg = 'shared' LIMIT 1)",
        ],
        expected_counts={"INSERT": 0, "DELETE": 0},
    ),

    # Branch INSERTs another copy of row R, parent DELETEs the existing R
    # → opposite-direction deltas on the same row → CONFLICT.
    Scenario3way(
        name="no-pk-branch-inserts-parent-deletes-conflict",
        setup_sql=[
            "CREATE TABLE log (msg text)",
            "INSERT INTO log VALUES ('x')",
        ],
        parent_mutate_sql=[
            "DELETE FROM log WHERE ctid = "
            "(SELECT ctid FROM log WHERE msg = 'x' LIMIT 1)",
        ],
        branch_mutate_sql=["INSERT INTO log VALUES ('x')"],
        expect_conflict_kinds=["row_no_pk"],
        expect_converges=False,
    ),

    # Base has two duplicate copies of 'a'; branch deletes one; parent
    # untouched → emit a single DELETE LIMIT 1; parent ends up with one copy.
    Scenario3way(
        name="no-pk-duplicate-row-handling",
        setup_sql=[
            "CREATE TABLE log (msg text)",
            "INSERT INTO log VALUES ('a'),('a')",
        ],
        parent_mutate_sql=[],
        branch_mutate_sql=[
            "DELETE FROM log WHERE ctid = "
            "(SELECT ctid FROM log WHERE msg = 'a' LIMIT 1)",
        ],
        expected_counts={"INSERT": 0, "DELETE": 1},
    ),
]


def main():
    pgdata = _server_pgdata()
    print(f"PGDATA: {pgdata}")
    failures = 0
    for sc in SCENARIOS:
        try:
            sc.run(pgdata)
        except AssertionError as e:
            failures += 1
            print(f"  FAIL  {sc.name}")
            print(f"        {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR {sc.name}: {type(e).__name__}: {e}")
    print()
    if failures:
        print(f"{failures}/{len(SCENARIOS)} scenarios failed")
        sys.exit(1)
    print(f"all {len(SCENARIOS)} 3-way scenarios passed")


try:
    import pytest

    @pytest.fixture(scope="session")
    def pgdata():
        """Server data directory, looked up once per pytest session.

        Lazy: only runs if a test actually requests it, so module collection
        doesn't require a live PostgreSQL server.
        """
        return _server_pgdata()

    @pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
    def test_3way_scenario(scenario, pgdata):
        scenario.run(pgdata)
except ImportError:
    pass


if __name__ == "__main__":
    main()
