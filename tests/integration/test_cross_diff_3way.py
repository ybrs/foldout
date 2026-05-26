"""End-to-end tests for `page_diff.cross_diff_3way`.

Each scenario:
  1. Create SOURCE db, run `setup_sql`
  2. Clone SOURCE into BASE (merge base) and BRANCH (via CREATE DATABASE TEMPLATE)
  3. build_page_index(BRANCH) + build_page_index(SOURCE) — in-memory
  4. parent_mutate_sql -> SOURCE   (parent drifts independently)
  5. branch_mutate_sql -> BRANCH   (the user's branch work)
  6. cross_diff_3way(SOURCE, BRANCH, BASE) -> conflicts / drifts / sql
  7. Verify counts and conflict / drift presence
  8. If no conflicts AND `expect_converges`: apply SQL to SOURCE,
     then check SOURCE matches an INDEPENDENTLY-CONSTRUCTED expected DB
     (BASE + parent_mutate + branch_mutate applied directly).

Runs on every variant via the shared `pg_cluster` fixture. Scenarios
parametrize so each appears as its own pytest item.
"""

from __future__ import annotations

import uuid

import psycopg
import pytest

from foldout import page_diff

from .pg_cluster import PgCluster


pytestmark = pytest.mark.integration


class Scenario3way:
    """One end-to-end 3-way diff scenario."""

    def __init__(self, name: str, *, setup_sql: list[str],
                 parent_mutate_sql: list[str],
                 branch_mutate_sql: list[str],
                 expected_counts: dict[str, int] | None = None,
                 expect_conflict_kinds: list[str] | None = None,
                 expect_drift_kinds: list[str] | None = None,
                 expect_converges: bool = True) -> None:
        """Build a scenario spec; nothing runs yet.

        Args:
            name: Short stable id used as the pytest parametrize id.
            setup_sql: Statements applied to SOURCE before cloning.
            parent_mutate_sql: Statements applied to SOURCE (the "parent")
                AFTER cloning, representing independent drift on main.
            branch_mutate_sql: Statements applied to BRANCH after cloning.
            expected_counts: Optional totals from cross_diff_3way to
                assert (INSERT/UPDATE/DELETE/DDL_PRE/DDL_POST).
            expect_conflict_kinds: Sorted list of conflict["kind"] values
                we expect to see; checked as set equality.
            expect_drift_kinds: Drift kinds that must be present (subset
                check, not equality — we tolerate extras).
            expect_converges: When True and no conflicts, the diff's SQL
                is applied to SOURCE and the result is hash-compared
                against an independently-constructed "expected" DB.
        """
        self.name = name
        self.setup_sql = setup_sql
        self.parent_mutate_sql = parent_mutate_sql
        self.branch_mutate_sql = branch_mutate_sql
        self.expected_counts = expected_counts or {}
        self.expect_conflict_kinds = expect_conflict_kinds or []
        self.expect_drift_kinds = expect_drift_kinds or []
        self.expect_converges = expect_converges


class Scenario3wayRunner:
    """Executes a Scenario3way against a live PgCluster."""

    def __init__(self, cluster: PgCluster) -> None:
        """Capture the cluster for all subsequent helper calls."""
        self.cluster = cluster

    def admin(self, sql: str) -> None:
        """Run a maintenance SQL statement against the `postgres` DB."""
        self.cluster.psql(sql, database="postgres")

    def exec_many(self, db: str, statements: list[str]) -> None:
        """Run multiple SQL statements against `db` in one connection."""
        with psycopg.connect(self.cluster.dsn(database=db),
                             autocommit=True) as conn:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)

    def clone(self, src: str, dst: str) -> None:
        """Make `dst` a copy of `src` via CREATE DATABASE TEMPLATE."""
        self.admin(f'CREATE DATABASE "{dst}" TEMPLATE "{src}"')

    def checkpoint(self, db: str) -> None:
        """Force pending writes on `db` to disk."""
        self.cluster.psql("CHECKPOINT", database=db)

    def content_hash(self, db: str) -> dict[str, str]:
        """Return `{schema.table: md5_of_ordered_rows}` for all user tables."""
        out: dict[str, str] = {}
        with psycopg.connect(self.cluster.dsn(database=db)) as conn:
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
                        f'SELECT md5(coalesce(string_agg(row, \'|\' '
                        f'ORDER BY row), \'\')) '
                        f'FROM (SELECT to_jsonb(x.*)::text AS row '
                        f'      FROM "{nsp}"."{rel}" AS x) s'
                    )
                    (h,) = cur.fetchone()
                out[f"{nsp}.{rel}"] = h
        return out

    def apply_sql(self, db: str, statements: list[str]) -> None:
        """Run the diff's SQL statements against `db` in one transaction."""
        with psycopg.connect(self.cluster.dsn(database=db),
                             autocommit=False) as conn:
            with conn.cursor() as cur:
                for stmt in statements:
                    cur.execute(stmt)
            conn.commit()

    def run(self, scenario: Scenario3way) -> None:
        """Execute the scenario and assert correctness."""
        suffix = uuid.uuid4().hex[:8]
        src = f"fld_3w_src_{suffix}"
        base = f"fld_3w_base_{suffix}"
        tgt = f"fld_3w_tgt_{suffix}"
        expected = f"fld_3w_exp_{suffix}"
        pgdata = str(self.cluster.pgdata)

        self.admin(f'CREATE DATABASE "{src}"')
        self.exec_many(src, scenario.setup_sql)
        self.checkpoint(src)

        # Clone source into BASE and BRANCH (always).
        self.clone(src, base)
        self.checkpoint(base)
        self.clone(src, tgt)
        self.checkpoint(tgt)
        # EXPECTED only needed when we expect convergence — replaying
        # both mutates can fail for "both did the same thing" / conflict
        # scenarios, and that's fine.
        if scenario.expect_converges:
            self.clone(src, expected)
            self.checkpoint(expected)

        branch_index = page_diff.build_page_index(pgdata, tgt)
        # Capture the parent's state at the same instant — mirrors what
        # `foldout branch` saves so 3-way can stat-skip on parent side.
        parent_index = page_diff.build_page_index(pgdata, src)

        # Apply mutations.
        self.exec_many(src, scenario.parent_mutate_sql)
        self.checkpoint(src)
        self.exec_many(tgt, scenario.branch_mutate_sql)
        self.checkpoint(tgt)
        if scenario.expect_converges:
            self.exec_many(expected, scenario.parent_mutate_sql)
            self.exec_many(expected, scenario.branch_mutate_sql)
            self.checkpoint(expected)

        result = page_diff.cross_diff_3way(
            pgdata, src, tgt, base, branch_index,
            parent_index=parent_index,
            verbose=False,
        )

        for key, want in scenario.expected_counts.items():
            got = result.get(key, 0)
            assert got == want, (
                f"[{scenario.name}] expected {key}={want}, got {got}; "
                f"sql={result.get('sql', [])}, "
                f"conflicts={result.get('conflicts')}, "
                f"drifts={result.get('drifts')}"
            )

        got_conflict_kinds = sorted({c["kind"] for c in result["conflicts"]})
        assert got_conflict_kinds == sorted(scenario.expect_conflict_kinds), (
            f"[{scenario.name}] expected conflict kinds "
            f"{scenario.expect_conflict_kinds}, got {got_conflict_kinds}: "
            f"{result['conflicts']}"
        )

        got_drift_kinds = sorted({d["kind"] for d in result["drifts"]})
        for kind in scenario.expect_drift_kinds:
            assert kind in got_drift_kinds, (
                f"[{scenario.name}] expected drift kind '{kind}' in "
                f"{got_drift_kinds}; drifts={result['drifts']}"
            )

        # If no conflicts and convergence expected: apply + hash-compare.
        if scenario.expect_converges and not result["conflicts"]:
            if result["sql"]:
                self.apply_sql(src, result["sql"])
            h_src = self.content_hash(src)
            h_exp = self.content_hash(expected)
            assert h_src == h_exp, (
                f"[{scenario.name}] post-apply hash mismatch.\n"
                f"  src={h_src}\n  expected={h_exp}\n"
                f"  sql={result['sql']}"
            )


# -------------------- scenarios --------------------
# Verbatim port of the SCENARIOS list from tests/test_page_diff_3way.py.

SCENARIOS: list[Scenario3way] = [
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


def _scenario_id(scenario: Scenario3way) -> str:
    """Render a pytest parametrize id from a Scenario3way object."""
    return scenario.name


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_scenario_id)
def test_cross_diff_3way_scenario(foldout_env: PgCluster,
                                  scenario: Scenario3way) -> None:
    """Run one 3-way diff scenario end-to-end against the shared cluster."""
    runner = Scenario3wayRunner(foldout_env)
    runner.run(scenario)
