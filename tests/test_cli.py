"""
CLI tests for foldout.

`test_version_command` is a fast unit-style check via Click's CliRunner.
`test_snapshot_command` is a real integration test: it stands up a
parent DB with a row, invokes `vka snapshot <parent>` as a subprocess
against a live Postgres, and verifies the snapshot DB was created with
the source's content. Requires FLD_DATABASE pointing at a Postgres
instance the test user can create/drop databases on.
"""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from foldout import __version__
from foldout.cli import cli

ROOT = Path(__file__).resolve().parent.parent
FOLDOUT_BIN = os.environ.get("FOLDOUT_BIN") or str(ROOT / ".venv" / "bin" / "foldout")
USER = os.environ.get("USER", "aybarsb")
HOST = "127.0.0.1"
FLD_DATABASE = os.environ.get(
    "FLD_DATABASE_FOR_TEST",
    f"postgresql://{USER}@{HOST}:5432/postgres",
)


def test_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def _dsn(db: str) -> str:
    return f"host={HOST} dbname={db} user={USER}"


def _admin(sql: str) -> None:
    with psycopg.connect(_dsn("postgres"), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql)


def _run(db: str, sqls: list[str]) -> None:
    with psycopg.connect(_dsn(db), autocommit=True) as c, c.cursor() as cur:
        for s in sqls:
            cur.execute(s)


def _query_one(db: str, sql: str):
    with psycopg.connect(_dsn(db)) as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


def _foldout_available() -> bool:
    if not Path(FOLDOUT_BIN).exists():
        return False
    try:
        with psycopg.connect(_dsn("postgres"), connect_timeout=2):
            return True
    except Exception:
        return False


def _list_snapshot_dbs_for(parent: str) -> list[str]:
    with psycopg.connect(_dsn("postgres")) as c, c.cursor() as cur:
        cur.execute(
            "SELECT datname FROM pg_database "
            "WHERE datname LIKE %s ORDER BY datname",
            (f"snapshot_{parent}_%",),
        )
        return [row[0] for row in cur.fetchall()]


@pytest.mark.skipif(
    not _foldout_available(),
    reason="foldout binary or local Postgres not available",
)
def test_snapshot_command() -> None:
    suf = uuid.uuid4().hex[:8]
    parent = f"fld_snap_test_{suf}"
    created_snapshots: list[str] = []
    try:
        _admin(f'DROP DATABASE IF EXISTS "{parent}";')
        _admin(f'CREATE DATABASE "{parent}";')
        _run(parent, [
            "CREATE TABLE widget (id int primary key, name text)",
            "INSERT INTO widget VALUES (1,'a'),(2,'b')",
        ])

        env = dict(os.environ)
        env["FLD_DATABASE"] = FLD_DATABASE
        result = subprocess.run(
            [FOLDOUT_BIN, "snapshot", parent],
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, (
            f"`vka snapshot {parent}` exited {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
        assert "Snapshot completed successfully" in result.stdout

        created_snapshots = _list_snapshot_dbs_for(parent)
        assert len(created_snapshots) == 1, (
            f"expected exactly 1 snapshot DB, got {created_snapshots}"
        )
        snap_db = created_snapshots[0]

        # The snapshot must hold the source's content.
        row = _query_one(
            snap_db,
            "SELECT count(*), max(name) FROM widget",
        )
        assert row == (2, "b"), (
            f"snapshot content mismatch: got {row}, expected (2, 'b')"
        )

        # fld_databases should know about both rows.
        agg = _query_one(
            "foldout",
            "SELECT array_agg(datname) FROM fld_databases "
            f"WHERE datname IN ('{parent}','{snap_db}')",
        )
        names = set(agg[0] or [])
        assert names == {parent, snap_db}, (
            f"fld_databases missing rows: have {names}"
        )
    finally:
        for db in (*created_snapshots, parent):
            try:
                _admin(f'DROP DATABASE IF EXISTS "{db}";')
            except Exception:
                pass
        try:
            with psycopg.connect(_dsn("foldout"), autocommit=True) as c, c.cursor() as cur:
                cur.execute(
                    "DELETE FROM fld_databases WHERE datname IN "
                    f"('{parent}', "
                    + ",".join(f"'{s}'" for s in created_snapshots) + ")"
                    if created_snapshots else
                    f"DELETE FROM fld_databases WHERE datname = '{parent}'"
                )
        except Exception:
            pass
