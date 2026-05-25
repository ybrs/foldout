"""
End-to-end tests for page-LSN database diff (page_diff_v2.cross_diff).

Each scenario:
  1. create SOURCE database, populate
  2. clone to TARGET database (pg_dump | psql)
  3. snapshot TARGET (records LSN + file stats)
  4. mutate TARGET (inserts / updates / deletes / no-ops)
  5. cross_diff(TARGET, SOURCE) -> SQL diff
  6. apply SQL to SOURCE
  7. assert SOURCE and TARGET are content-equal (per-table hash)

Run directly:    python tests/test_page_diff.py
Run via pytest:  pytest tests/test_page_diff.py -v

The test assumes a local PostgreSQL 17 server reachable as the current user
on 127.0.0.1. Set $PG_DUMP to override the pg_dump binary path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from foldout import page_diff as page_diff_v2  # noqa: E402

USER = os.environ.get("USER", "aybarsb")
HOST = "127.0.0.1"
PG_DUMP = os.environ.get("PG_DUMP", "/opt/homebrew/opt/postgresql@17/bin/pg_dump")
PSQL = os.environ.get("PSQL", "/opt/homebrew/opt/postgresql@17/bin/psql")


def _dsn(db):
    return f"host={HOST} dbname={db} user={USER}"


def _server_pgdata():
    """Return the server's data directory.

    Honors `FLD_PG_DATA_PATH` (for container-on-host setups where the
    server's path differs from the host-visible path). Otherwise asks
    the running server via `SHOW data_directory`.
    """
    override = os.environ.get("FLD_PG_DATA_PATH")
    if override:
        return override
    with psycopg.connect(_dsn("postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            return cur.fetchone()[0]


def _admin_sql(sql):
    """Run a SQL statement against the 'postgres' DB (for CREATE/DROP DATABASE)."""
    with psycopg.connect(_dsn("postgres"), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def _exec(db, sql):
    with psycopg.connect(_dsn(db), autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)


def _exec_many(db, sqls):
    with psycopg.connect(_dsn(db), autocommit=True) as conn:
        with conn.cursor() as cur:
            for s in sqls:
                cur.execute(s)


def _drop_db(name):
    _admin_sql(f'DROP DATABASE IF EXISTS "{name}";')


def _create_db(name):
    _admin_sql(f'CREATE DATABASE "{name}";')


def _clone_db(src, dst):
    """Copy src -> dst via pg_dump | psql. Both DBs must exist; dst empty."""
    if not os.path.exists(PG_DUMP):
        raise RuntimeError(f"pg_dump not found at {PG_DUMP} (set $PG_DUMP)")
    p1 = subprocess.Popen(
        [PG_DUMP, "-h", HOST, "-U", USER, "-d", src],
        stdout=subprocess.PIPE,
    )
    p2 = subprocess.Popen(
        [PSQL, "-q", "-v", "ON_ERROR_STOP=1", _dsn(dst)],
        stdin=p1.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    p1.stdout.close()
    _, err = p2.communicate()
    p1.wait()
    if p2.returncode != 0:
        raise RuntimeError(f"clone failed: {err.decode()[:500]}")


def _checkpoint(db):
    _exec(db, "CHECKPOINT")


def _content_hash(db):
    """Return a dict of {schema.table: md5_of_ordered_rows} for all user tables."""
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
                    f'SELECT md5(coalesce(string_agg(row, \'|\' ORDER BY row), \'\')) '
                    f'FROM (SELECT to_jsonb(x.*)::text AS row '
                    f'      FROM "{nsp}"."{rel}" AS x) s'
                )
                (h,) = cur.fetchone()
            out[f"{nsp}.{rel}"] = h
    return out


def _apply_sql(db, sql_statements):
    with psycopg.connect(_dsn(db), autocommit=False) as conn:
        with conn.cursor() as cur:
            for s in sql_statements:
                cur.execute(s)
        conn.commit()


class Scenario:
    """One end-to-end test case."""

    def __init__(self, name, setup_sql, mutate_sql, expected_counts=None,
                 skip_post_checkpoint=False):
        self.name = name
        self.setup_sql = setup_sql
        self.mutate_sql = mutate_sql
        self.expected = expected_counts  # dict like {"INSERT": 2, ...} or None
        self.skip_post_checkpoint = skip_post_checkpoint

    def run(self, pgdata):
        suffix = uuid.uuid4().hex[:8]
        src = f"fld_t_src_{suffix}"
        tgt = f"fld_t_tgt_{suffix}"
        snap_path = f"/tmp/fld_t_{suffix}.json"
        try:
            _drop_db(src)
            _drop_db(tgt)
            _create_db(src)
            _exec_many(src, self.setup_sql)
            _checkpoint(src)

            _create_db(tgt)
            _clone_db(src, tgt)
            _checkpoint(tgt)

            page_diff_v2.snapshot(pgdata, tgt, snap_path)

            _exec_many(tgt, self.mutate_sql)
            if not getattr(self, "skip_post_checkpoint", False):
                _checkpoint(tgt)

            result = page_diff_v2.cross_diff(pgdata, src, tgt, snap_path, verbose=False)
            sql = result["sql"]

            if self.expected is not None:
                for k, v in self.expected.items():
                    got = result.get(k, 0)
                    assert got == v, (
                        f"[{self.name}] expected {k}={v}, got {got}. "
                        f"sql={sql}"
                    )

            if sql:
                _apply_sql(src, sql)

            h_src = _content_hash(src)
            h_tgt = _content_hash(tgt)
            assert h_src == h_tgt, (
                f"[{self.name}] post-merge hash mismatch.\n"
                f"  src={h_src}\n  tgt={h_tgt}\n  sql={sql}"
            )
            print(f"  PASS  {self.name}  "
                  f"({len(sql)} stmts, {result['elapsed_ms']:.0f} ms)")
        finally:
            if os.path.exists(snap_path):
                os.unlink(snap_path)
            _drop_db(src)
            _drop_db(tgt)


# ----------------- scenarios -----------------

SCENARIOS = [
    Scenario(
        name="no-changes",
        setup_sql=[
            "CREATE TABLE u (id int primary key, name text)",
            "INSERT INTO u SELECT g, 'u-'||g FROM generate_series(1,500) g",
        ],
        mutate_sql=[],  # do nothing on target
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
            # UPDATE on no-PK table: shows up as INSERT(new) + DELETE(old)
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
            # delete one of three 'dup' rows; should produce one DELETE
            "DELETE FROM np WHERE ctid = (SELECT ctid FROM np WHERE v='dup' LIMIT 1)",
            # add a new duplicate
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
        skip_post_checkpoint=True,  # critical: regression for forgotten flush
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
            # Advance the sequence further on branch via more INSERTs.
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
            # jsonb default — Python couldn't sensibly parse this; Postgres can.
            "ALTER TABLE u ADD COLUMN meta jsonb NOT NULL DEFAULT '{\"v\":0}'::jsonb",
            "UPDATE u SET meta = '{\"v\":42}'::jsonb WHERE id = 2",
        ],
        # Only id=2 should generate UPDATE; id=1 and id=3 keep the default
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
            # < TOAST threshold, regular tuple
            "INSERT INTO big VALUES (1, repeat('a', 200))",
            # > TOAST threshold (~2KB), forces TOAST
            "INSERT INTO big VALUES (2, repeat('b', 100000))",
        ],
        mutate_sql=[
            "UPDATE big SET blob = repeat('c', 100000) WHERE id = 2",
            "INSERT INTO big VALUES (3, repeat('d', 100000))",
        ],
        expected_counts={"INSERT": 1, "UPDATE": 1, "DELETE": 0},
    ),
]


def main():
    if not os.path.exists(PSQL):
        print(f"FAIL: psql not found at {PSQL} (set $PSQL)")
        sys.exit(1)
    pgdata = _server_pgdata()
    print(f"PGDATA: {pgdata}")
    print(f"pg_dump: {PG_DUMP}")
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
    print(f"all {len(SCENARIOS)} scenarios passed")


# ---- pytest entry points (one parametrized test per scenario) ----

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
    def test_scenario(scenario, pgdata):
        scenario.run(pgdata)
except ImportError:
    pass


if __name__ == "__main__":
    main()
