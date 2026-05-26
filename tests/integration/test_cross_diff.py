"""End-to-end tests for `page_diff.cross_diff` (2-way diff).

Each scenario:
  1. Create SOURCE db, run `setup_sql`
  2. Clone SOURCE -> TARGET (CREATE DATABASE TEMPLATE)
  3. build_page_index(TARGET)  — in-memory, no JSON files
  4. Mutate TARGET (inserts / updates / deletes / DDL)
  5. cross_diff(pgdata, SOURCE, TARGET, index)  -> SQL diff
  6. Apply the SQL to SOURCE
  7. Assert SOURCE and TARGET are content-equal (per-table md5 hash)

Runs against every cluster variant (pg16, pg17, pg18-default, pg18-clone)
via the shared `pg_cluster` fixture. Scenarios are parametrized so each
appears as its own pytest item.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest

from foldout import page_diff

from .pg_cluster import PgCluster


pytestmark = pytest.mark.integration


class Scenario:
    """One end-to-end 2-way diff scenario."""

    def __init__(self, name: str, setup_sql: list[str],
                 mutate_sql: list[str],
                 expected_counts: dict[str, int] | None = None,
                 skip_post_checkpoint: bool = False) -> None:
        """Build a scenario spec; nothing runs yet.

        Args:
            name: Short stable id used as the pytest parametrize id.
            setup_sql: Statements to run on SOURCE before cloning.
            mutate_sql: Statements to run on TARGET after cloning,
                producing the diff we'll detect.
            expected_counts: Optional `{INSERT|UPDATE|DELETE|DDL_PRE|DDL_POST: n}`.
                Each present key is asserted against the totals reported
                by cross_diff.
            skip_post_checkpoint: If True, do NOT run CHECKPOINT after
                mutate_sql. Used to catch regressions where the diff
                forgets to flush dirty buffers itself.
        """
        self.name = name
        self.setup_sql = setup_sql
        self.mutate_sql = mutate_sql
        self.expected = expected_counts
        self.skip_post_checkpoint = skip_post_checkpoint


class ScenarioRunner:
    """Executes a Scenario against a live PgCluster."""

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
        """Make `dst` a copy of `src` using PG's CREATE DATABASE TEMPLATE.

        Replaces the previous pg_dump | psql pipe. CREATE DATABASE
        TEMPLATE requires no concurrent connections to src — fine here
        because each helper opens and closes its own connection.
        """
        self.admin(f'CREATE DATABASE "{dst}" TEMPLATE "{src}"')

    def checkpoint(self, db: str) -> None:
        """Force pending writes on `db` to disk."""
        self.cluster.psql("CHECKPOINT", database=db)

    def content_hash(self, db: str) -> dict[str, str]:
        """Return `{schema.table: md5_of_ordered_rows}` for all user tables.

        Used as the post-merge equality check: after applying the diff
        SQL to source, source and target must hash identically.
        """
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

    def run(self, scenario: Scenario) -> None:
        """Execute the scenario and assert correctness."""
        suffix = uuid.uuid4().hex[:8]
        src = f"fld_t_src_{suffix}"
        tgt = f"fld_t_tgt_{suffix}"
        pgdata = str(self.cluster.pgdata)

        self.admin(f'CREATE DATABASE "{src}"')
        self.exec_many(src, scenario.setup_sql)
        self.checkpoint(src)

        self.clone(src, tgt)
        self.checkpoint(tgt)

        tgt_index = page_diff.build_page_index(pgdata, tgt)

        self.exec_many(tgt, scenario.mutate_sql)
        if not scenario.skip_post_checkpoint:
            self.checkpoint(tgt)

        result = page_diff.cross_diff(pgdata, src, tgt, tgt_index,
                                      verbose=False)
        sql = result["sql"]

        if scenario.expected is not None:
            for key, want in scenario.expected.items():
                got = result.get(key, 0)
                assert got == want, (
                    f"[{scenario.name}] expected {key}={want}, got {got}. "
                    f"sql={sql}"
                )

        if sql:
            self.apply_sql(src, sql)

        h_src = self.content_hash(src)
        h_tgt = self.content_hash(tgt)
        assert h_src == h_tgt, (
            f"[{scenario.name}] post-merge hash mismatch.\n"
            f"  src={h_src}\n  tgt={h_tgt}\n  sql={sql}"
        )


# ----------------- scenarios -----------------
# Verbatim port of the SCENARIOS list from tests/test_page_diff.py.
# Adding / removing entries here drives the parametrized pytest matrix.

SCENARIOS: list[Scenario] = [
    Scenario(
        name="no-changes",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u SELECT g, 'u-'||g FROM generate_series(1,500) g",
        ],
        mutate_sql=[],
        expected_counts={"INSERT": 0, "UPDATE": 0, "DELETE": 0},
    ),
    Scenario(
        name="simple-insert-update-delete",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text, score int)",
            "INSERT INTO u SELECT g, 'u-'||g, g*10 FROM generate_series(1,1000) g",
        ],
        mutate_sql=[
            "INSERT INTO u VALUES (2001,'new-1',99)",
            "INSERT INTO u VALUES (2002,'new-2',98)",
            "UPDATE u SET score=42, name='RENAMED' WHERE id=500",
            "DELETE FROM u WHERE id=999",
        ],
        expected_counts={"INSERT": 2, "UPDATE": 1, "DELETE": 1},
    ),
    Scenario(
        name="multiple-tables-only-some-changed",
        setup_sql=[
            "CREATE TABLE a (id int primary key, v text)",
            "CREATE TABLE b (id int primary key, v text)",
            "CREATE TABLE c (id int primary key, v text)",
            "INSERT INTO a SELECT g, 'a'||g FROM generate_series(1,200) g",
            "INSERT INTO b SELECT g, 'b'||g FROM generate_series(1,200) g",
            "INSERT INTO c SELECT g, 'c'||g FROM generate_series(1,200) g",
        ],
        mutate_sql=[
            "UPDATE b SET v='X' WHERE id IN (10, 20, 30)",
            "INSERT INTO c VALUES (999,'cnew')",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 3, "DELETE": 0},
    ),
    Scenario(
        name="composite-pk",
        setup_sql=[
            "CREATE TABLE k (a int, b int, v text, primary key (a,b))",
            "INSERT INTO k SELECT g, g%7, 'v'||g FROM generate_series(1,300) g",
        ],
        mutate_sql=[
            "INSERT INTO k VALUES (1000, 1, 'newcomp')",
            "UPDATE k SET v='upd' WHERE a=5 AND b=5",
            "DELETE FROM k WHERE a=10 AND b=3",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 1, "DELETE": 1},
    ),
    Scenario(
        name="bulk-insert-extends-file",
        setup_sql=[
            "CREATE TABLE big (id int primary key, payload text)",
            "INSERT INTO big SELECT g, repeat('x',100) FROM generate_series(1,500) g",
        ],
        mutate_sql=[
            "INSERT INTO big SELECT g, repeat('y',100) FROM generate_series(1001,1500) g",
        ],
        expected_counts={"INSERT": 500, "UPDATE": 0, "DELETE": 0},
    ),
    Scenario(
        name="bulk-delete",
        setup_sql=[
            "CREATE TABLE d (id int primary key, v text)",
            "INSERT INTO d SELECT g, 'v'||g FROM generate_series(1,500) g",
        ],
        mutate_sql=[
            "DELETE FROM d WHERE id BETWEEN 100 AND 199",
        ],
        expected_counts={"INSERT": 0, "UPDATE": 0, "DELETE": 100},
    ),
    Scenario(
        name="update-with-nulls-and-quotes",
        setup_sql=[
            "CREATE TABLE q (id int primary key, name text, note text)",
            "INSERT INTO q VALUES (1,'a',NULL),(2,'b','hello'),(3,'c','x')",
        ],
        mutate_sql=[
            "UPDATE q SET note='it''s a test' WHERE id=1",
            "UPDATE q SET note=NULL WHERE id=2",
            "INSERT INTO q VALUES (4, NULL, 'has null name')",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 2, "DELETE": 0},
    ),
    Scenario(
        name="no-pk-basic-insert-delete",
        setup_sql=[
            "CREATE TABLE np (v text, n int)",
            "INSERT INTO np SELECT 'a'||g, g FROM generate_series(1,100) g",
        ],
        mutate_sql=[
            "INSERT INTO np VALUES ('zzz', 9999)",
            "DELETE FROM np WHERE n = 42",
        ],
        expected_counts={"INSERT": 1, "DELETE": 1},
    ),
    Scenario(
        name="no-pk-update-becomes-insert-plus-delete",
        setup_sql=[
            "CREATE TABLE np (v text, n int)",
            "INSERT INTO np SELECT 'r'||g, g FROM generate_series(1,50) g",
        ],
        mutate_sql=[
            "UPDATE np SET v='CHANGED' WHERE n = 10",
        ],
        expected_counts={"INSERT": 1, "DELETE": 1, "UPDATE": 0},
    ),
    Scenario(
        name="no-pk-with-duplicate-rows",
        setup_sql=[
            "CREATE TABLE np (v text, n int)",
            "INSERT INTO np VALUES ('dup',1),('dup',1),('dup',1),('uniq',2)",
        ],
        mutate_sql=[
            "DELETE FROM np WHERE ctid = (SELECT ctid FROM np WHERE v='dup' LIMIT 1)",
            "INSERT INTO np VALUES ('uniq',2)",
        ],
        expected_counts={"INSERT": 1, "DELETE": 1, "UPDATE": 0},
    ),
    Scenario(
        name="diff-without-manual-checkpoint",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u SELECT g, 'u-'||g FROM generate_series(1,200) g",
        ],
        mutate_sql=[
            "INSERT INTO u VALUES (999, 'nochk')",
            "DELETE FROM u WHERE id = 50",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 0, "DELETE": 1},
        skip_post_checkpoint=True,
    ),
    # ---- DDL ----
    Scenario(
        name="ddl-create-table",
        setup_sql=[
            "CREATE TABLE existing (id int primary key, v text)",
            "INSERT INTO existing VALUES (1,'a'),(2,'b')",
        ],
        mutate_sql=[
            "CREATE TABLE newt (id int primary key, name text not null)",
            "INSERT INTO newt VALUES (10,'x')",
        ],
        expected_counts={"DDL_PRE": 1, "INSERT": 1},
    ),
    Scenario(
        name="ddl-drop-table",
        setup_sql=[
            "CREATE TABLE t1 (id int primary key)",
            "CREATE TABLE t2 (id int primary key)",
        ],
        mutate_sql=[
            "DROP TABLE t1",
        ],
        expected_counts={"DDL_POST": 1},
    ),
    Scenario(
        name="ddl-add-column",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b')",
        ],
        mutate_sql=[
            "ALTER TABLE u ADD COLUMN extra int DEFAULT 0 NOT NULL",
            "UPDATE u SET extra = 99 WHERE id = 1",
        ],
        expected_counts={"DDL_PRE": 1, "UPDATE": 1},
    ),
    Scenario(
        name="ddl-drop-column",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text, doomed int)",
            "INSERT INTO u VALUES (1,'a',5),(2,'b',7)",
        ],
        mutate_sql=[
            "ALTER TABLE u DROP COLUMN doomed",
        ],
        expected_counts={"DDL_PRE": 1},
    ),
    Scenario(
        name="ddl-add-index",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b')",
        ],
        mutate_sql=[
            "CREATE INDEX u_name_idx ON u(name)",
        ],
        expected_counts={"DDL_PRE": 1},
    ),
    Scenario(
        name="ddl-foreign-key",
        setup_sql=[
            "CREATE TABLE parent (id int primary key)",
            "CREATE TABLE child (id int primary key, parent_id int)",
            "INSERT INTO parent VALUES (1),(2)",
        ],
        mutate_sql=[
            "ALTER TABLE child ADD CONSTRAINT child_parent_fk "
            "FOREIGN KEY (parent_id) REFERENCES parent(id) ON DELETE CASCADE",
            "INSERT INTO child VALUES (10, 1)",
        ],
        expected_counts={"DDL_PRE": 1, "INSERT": 1},
    ),
    Scenario(
        name="ddl-create-view",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text, age int)",
            "INSERT INTO u VALUES (1,'a',20),(2,'b',30)",
        ],
        mutate_sql=[
            "CREATE VIEW adults AS SELECT id, name FROM u WHERE age >= 18",
        ],
        expected_counts={"DDL_PRE": 1},
    ),
    Scenario(
        name="ddl-create-function",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
        ],
        mutate_sql=[
            "CREATE FUNCTION greet(n text) RETURNS text "
            "LANGUAGE sql AS $$ SELECT 'hello, '||n $$",
        ],
        expected_counts={"DDL_PRE": 1},
    ),
    Scenario(
        name="ddl-create-table-with-serial",
        setup_sql=[
            "CREATE TABLE other (id int primary key)",
            "INSERT INTO other VALUES (1)",
        ],
        mutate_sql=[
            "CREATE TABLE evt (id serial primary key, msg text)",
            "INSERT INTO evt(msg) VALUES ('first'),('second'),('third')",
        ],
        expected_counts={"INSERT": 3},
    ),
    Scenario(
        name="ddl-sequence-only-advanced",
        setup_sql=[
            "CREATE TABLE log (id serial primary key, msg text)",
            "INSERT INTO log(msg) VALUES ('a'),('b')",
        ],
        mutate_sql=[
            "INSERT INTO log(msg) VALUES ('c'),('d'),('e')",
        ],
        expected_counts={"INSERT": 3},
    ),
    Scenario(
        name="ddl-add-column-jsonb-default",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a'),(2,'b'),(3,'c')",
        ],
        mutate_sql=[
            "ALTER TABLE u ADD COLUMN meta jsonb NOT NULL DEFAULT '{\"v\":0}'::jsonb",
            "UPDATE u SET meta = '{\"v\":42}'::jsonb WHERE id = 2",
        ],
        expected_counts={"DDL_PRE": 1, "INSERT": 0, "UPDATE": 1, "DELETE": 0},
    ),
    Scenario(
        name="ddl-and-dml-combined",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u VALUES (1,'a')",
        ],
        mutate_sql=[
            "ALTER TABLE u ADD COLUMN score int",
            "INSERT INTO u VALUES (2,'b',100)",
            "UPDATE u SET score = 50 WHERE id = 1",
            "CREATE INDEX u_score_idx ON u(score)",
        ],
        expected_counts={"DDL_PRE": 2, "INSERT": 1, "UPDATE": 1},
    ),
    Scenario(
        name="toasted-large-value",
        setup_sql=[
            "CREATE TABLE big (id int primary key, blob text)",
            "INSERT INTO big VALUES (1, repeat('a', 200))",
            "INSERT INTO big VALUES (2, repeat('b', 100000))",
        ],
        mutate_sql=[
            "UPDATE big SET blob = repeat('c', 100000) WHERE id = 2",
            "INSERT INTO big VALUES (3, repeat('d', 100000))",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 1, "DELETE": 0},
    ),
]


def _scenario_id(scenario: Scenario) -> str:
    """Render a pytest parametrize id from a Scenario object."""
    return scenario.name


@pytest.mark.parametrize("scenario", SCENARIOS, ids=_scenario_id)
def test_cross_diff_scenario(foldout_env: PgCluster, scenario: Scenario) -> None:
    """Run one diff scenario end-to-end against the shared cluster."""
    runner = ScenarioRunner(foldout_env)
    runner.run(scenario)
