"""
End-to-end CLI integration tests for `vka branch` and `fld diff`.

These drive the actual `foldout` binary via subprocess, asserting on
its exit code, stdout, and the post-apply state of the databases. This
catches regressions in the user-facing path that the function-level
tests in test_page_diff*.py do not exercise:

  - argument parsing & subcommand routing
  - branch creation (fld_databases registration, snapshot files, base linkage)
  - 3-way vs. 2-way fallback selection
  - --apply success path (incl. auto-cleanup of base + parent snapshot)
  - --apply conflict path (must exit non-zero, parent untouched)

Each scenario sets up its own throwaway parent + branch DBs, runs the
CLI, asserts, and cleans up everything regardless of outcome (rows in
fld_databases, the branch + base PG databases, and snapshot JSON files
in ~/.foldout/snapshots/).

Run directly:    python tests/test_cli_diff.py
Run via pytest:  pytest tests/test_cli_diff.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parent.parent
FOLDOUT_BIN = os.environ.get("FOLDOUT_BIN") or str(ROOT / ".venv" / "bin" / "foldout")
USER = os.environ.get("USER", "aybarsb")
HOST = "127.0.0.1"
FLD_DATABASE = os.environ.get(
    "FLD_DATABASE_FOR_TEST",
    f"postgresql://{USER}@{HOST}:5432/postgres",
)


# ---------------- low-level helpers ----------------

def _dsn(db):
    return f"host={HOST} dbname={db} user={USER}"


def _admin(sql):
    with psycopg.connect(_dsn("postgres"), autocommit=True) as c, c.cursor() as cur:
        cur.execute(sql)


def _run(db, sqls):
    with psycopg.connect(_dsn(db), autocommit=True) as c, c.cursor() as cur:
        for s in sqls:
            cur.execute(s)


def _query_one(db, sql):
    with psycopg.connect(_dsn(db)) as c, c.cursor() as cur:
        cur.execute(sql)
        return cur.fetchone()


def vka(*args, expect_exit_code=0):
    """Run foldout CLI. Returns (returncode, stdout, stderr).
    By default asserts exit code matches expect_exit_code; pass None to skip."""
    env = dict(os.environ)
    env["FLD_DATABASE"] = FLD_DATABASE
    result = subprocess.run(
        [FOLDOUT_BIN, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if expect_exit_code is not None and result.returncode != expect_exit_code:
        raise AssertionError(
            f"`vka {' '.join(args)}` expected exit {expect_exit_code}, "
            f"got {result.returncode}\n"
            f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
    return result.returncode, result.stdout, result.stderr


def _lookup_branch(branch_name):
    """Return (branch_oid, base_oid) or (None, None)."""
    try:
        row = _query_one(
            "foldout",
            f"SELECT oid, base_oid FROM fld_databases WHERE datname = '{branch_name}'",
        )
    except Exception:
        return None, None
    if not row:
        return None, None
    return row[0], row[1]


def _cleanup(parent, branch):
    """Drop branch + base DBs, foldout metadata rows, and snapshot files."""
    branch_oid, _ = _lookup_branch(branch)
    base_name = f"__base__{branch}"

    for db in (branch, base_name, parent):
        try:
            _admin(f'DROP DATABASE IF EXISTS "{db}";')
        except Exception:
            pass

    try:
        _run("foldout", [
            f"DELETE FROM fld_databases WHERE datname IN "
            f"('{parent}','{branch}','{base_name}')",
        ])
    except Exception:
        pass

    if branch_oid is not None:
        for suffix in ("", "_parent"):
            p = Path(os.path.expanduser(
                f"~/.foldout/snapshots/{branch_oid}{suffix}.json"
            ))
            if p.exists():
                p.unlink()


# ---------------- test setup helpers ----------------

def strip_base(branch):
    """Detach the merge base from a branch: drop the __base__<branch> DB,
    remove its fld_databases row, clear branch.base_oid, delete parent
    snapshot file. Simulates a legacy branch (created pre-3way) or one
    that's already been --applied.
    """
    branch_oid, base_oid = _lookup_branch(branch)
    if base_oid is not None:
        row = _query_one(
            "foldout",
            f"SELECT datname FROM fld_databases WHERE oid = {base_oid}",
        )
        if row and row[0]:
            try:
                _admin(f'DROP DATABASE IF EXISTS "{row[0]}"')
            except Exception:
                pass
        _run("foldout", [f"DELETE FROM fld_databases WHERE oid = {base_oid}"])
    _run("foldout",
         [f"UPDATE fld_databases SET base_oid = NULL WHERE datname = '{branch}'"])
    if branch_oid is not None:
        parent_snap = Path(os.path.expanduser(
            f"~/.foldout/snapshots/{branch_oid}_parent.json"
        ))
        if parent_snap.exists():
            parent_snap.unlink()


# ---------------- assertion helpers (post-state) ----------------

def assert_table_exists(db, table, expected=True):
    row = _query_one(
        db,
        f"SELECT to_regclass('public.\"{table}\"') IS NOT NULL",
    )
    actually_exists = row[0]
    assert actually_exists == expected, (
        f"[{db}] table '{table}' exists={actually_exists}, expected {expected}"
    )


def assert_column_exists(db, table, column, expected=True):
    row = _query_one(
        db,
        f"SELECT EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema='public' AND table_name='{table}' "
        f"AND column_name='{column}')",
    )
    assert row[0] == expected, (
        f"[{db}] column {table}.{column} exists={row[0]}, expected {expected}"
    )


def assert_row_count(db, table, n):
    row = _query_one(db, f'SELECT count(*) FROM "{table}"')
    assert row[0] == n, f"[{db}] {table} has {row[0]} rows, expected {n}"


def assert_row_value(db, table, pk_col, pk_val, col, expected):
    row = _query_one(
        db,
        f"SELECT \"{col}\"::text FROM \"{table}\" WHERE \"{pk_col}\" = {pk_val}",
    )
    got = row[0] if row else None
    assert got == expected, (
        f"[{db}] {table}.{col} for {pk_col}={pk_val}: got {got!r}, "
        f"expected {expected!r}"
    )


def assert_base_gone(branch):
    """After successful --apply, base_oid in fld_databases should be NULL
    and the __base__<branch> database should not exist."""
    _, base_oid = _lookup_branch(branch)
    assert base_oid is None, f"branch '{branch}' still has base_oid={base_oid}"
    base_name = f"__base__{branch}"
    row = _query_one(
        "postgres",
        f"SELECT 1 FROM pg_database WHERE datname = '{base_name}'",
    )
    assert row is None, f"base DB '{base_name}' still exists"


# ---------------- scenario runner ----------------

class CliScenario:
    def __init__(self, name, *, setup, parent_mutate, branch_mutate, check):
        self.name = name
        self.setup = setup
        self.parent_mutate = parent_mutate
        self.branch_mutate = branch_mutate
        self.check = check    # callable(parent, branch)

    def run(self):
        suf = uuid.uuid4().hex[:8]
        parent = f"fld_cli_p_{suf}"
        branch = f"fld_cli_b_{suf}"
        try:
            _admin(f'CREATE DATABASE "{parent}";')
            _run(parent, self.setup)
            _run(parent, ["CHECKPOINT"])

            code, out, err = vka("branch", parent, branch)
            assert "Branch completed successfully" in out, (
                f"[{self.name}] branch output unexpected:\n{out}\n{err}"
            )
            # base snapshot DB must exist after `vka branch`
            base_name = f"__base__{branch}"
            assert _query_one(
                "postgres",
                f"SELECT 1 FROM pg_database WHERE datname = '{base_name}'",
            ) is not None, f"[{self.name}] expected base '{base_name}' to exist"

            _run(parent, self.parent_mutate)
            _run(branch, self.branch_mutate)

            self.check(parent, branch)

            print(f"  PASS  {self.name}")
        finally:
            _cleanup(parent, branch)


# ---------------- scenarios ----------------

def _no_changes_check(parent, branch):
    code, out, err = vka("diff", branch)
    assert "INSERT=0  UPDATE=0  DELETE=0" in out, out
    assert "DDL_PRE=0" in out, out
    assert "conflicts=0  drifts=0" in out, out


def _branch_adds_table_check(parent, branch):
    # preview
    _, out, _ = vka("diff", branch)
    assert "CREATE TABLE" in out and "new_t" in out, out
    assert "INSERT=2" in out, out
    assert "conflicts=0" in out, out
    # apply
    vka("diff", branch, "--apply")
    assert_table_exists(parent, "new_t", True)
    assert_row_count(parent, "new_t", 2)
    assert_base_gone(branch)


def _parent_drift_check(parent, branch):
    _, out, _ = vka("diff", branch)
    assert "CREATE TABLE" in out and "branch_t" in out, out
    # Parent's table must NOT be in the proposed SQL
    assert "DROP TABLE" not in out, out
    assert "parent_t" not in out.split("-- SQL diff --")[-1], (
        "parent_t leaked into SQL section:\n" + out
    )
    # Drift counter should be >= 1 (parent_t at table level)
    assert "drifts=" in out and "drifts=0" not in out, out

    vka("diff", branch, "--apply")
    assert_table_exists(parent, "parent_t", True)   # drift preserved
    assert_table_exists(parent, "branch_t", True)   # branch's add applied
    assert_row_count(parent, "branch_t", 1)
    assert_base_gone(branch)


def _column_changes_check(parent, branch):
    _, out, _ = vka("diff", branch)
    assert "ADD COLUMN" in out, out
    assert "branch_col" in out, out
    assert "UPDATE=1" in out, out
    vka("diff", branch, "--apply")
    assert_column_exists(parent, "u", "parent_col", True)  # drift preserved
    assert_column_exists(parent, "u", "branch_col", True)  # branch's col
    assert_row_value(parent, "u", "id", 1, "branch_col", "x")
    assert_base_gone(branch)


def _conflict_check(parent, branch):
    _, out, _ = vka("diff", branch)
    assert "conflicts=1" in out, out
    # --apply must exit non-zero and leave parent untouched
    vka("diff", branch, "--apply", expect_exit_code=1)
    assert_row_value(parent, "u", "id", 1, "v", "main-wins")
    # base must still exist (apply aborted)
    base_name = f"__base__{branch}"
    assert _query_one(
        "postgres",
        f"SELECT 1 FROM pg_database WHERE datname = '{base_name}'",
    ) is not None, "base must still exist after a refused apply"


def _no_base_warning_check(parent, branch):
    """Branch with base manually stripped: diff should print a prominent
    warning on stderr and still exit 0 (preview only)."""
    strip_base(branch)
    code, out, err = vka("diff", branch)
    assert "WARNING: no merge base" in err, (
        "expected fallback warning on stderr; got:\n" + err
    )
    # Body of diff still runs (just 2-way)
    assert "snapshot:" in out, out


def _no_base_refuses_apply_check(parent, branch):
    """--apply on baseless branch must exit non-zero and NOT modify parent."""
    strip_base(branch)
    code, out, err = vka("diff", branch, "--apply", expect_exit_code=1)
    assert "Refusing to apply" in err, err
    # Parent must still have only its original row
    assert_row_count(parent, "u", 1)


def _no_base_allow_2way_apply_check(parent, branch):
    """--apply --allow-2way-apply on baseless branch succeeds."""
    strip_base(branch)
    code, out, err = vka("diff", branch, "--apply", "--allow-2way-apply")
    # Branch added row id=2; parent should have it now.
    assert_row_count(parent, "u", 2)


def _apply_then_rerun_check(parent, branch):
    # Apply branch's INSERT
    vka("diff", branch, "--apply")
    assert_row_count(parent, "u", 2)
    assert_base_gone(branch)

    # Second run: no base any more, must fall back to 2-way with the
    # prominent stderr warning, and exit 0 (preview only).
    code, out, err = vka("diff", branch)
    assert "WARNING: no merge base" in err, (
        "Expected fallback warning on stderr after base was dropped; got:\n" + err
    )


SCENARIOS = [
    CliScenario(
        name="no-changes",
        setup=["CREATE TABLE u (id int primary key, name text)",
               "INSERT INTO u VALUES (1,'a')"],
        parent_mutate=[],
        branch_mutate=[],
        check=_no_changes_check,
    ),
    CliScenario(
        name="branch-adds-table-parent-untouched",
        setup=["CREATE TABLE base_t (id int primary key)",
               "INSERT INTO base_t VALUES (1)"],
        parent_mutate=[],
        branch_mutate=[
            "CREATE TABLE new_t (id int primary key, v text)",
            "INSERT INTO new_t VALUES (1,'a'),(2,'b')",
        ],
        check=_branch_adds_table_check,
    ),
    CliScenario(
        name="parent-drifts-branch-adds-table",
        setup=["CREATE TABLE base_t (id int primary key)"],
        parent_mutate=[
            "CREATE TABLE parent_t (id int primary key, v text)",
            "INSERT INTO parent_t VALUES (1,'p')",
        ],
        branch_mutate=[
            "CREATE TABLE branch_t (id int primary key, v text)",
            "INSERT INTO branch_t VALUES (1,'b')",
        ],
        check=_parent_drift_check,
    ),
    CliScenario(
        name="column-changes-on-both-sides",
        setup=["CREATE TABLE u (id int primary key, name text)",
               "INSERT INTO u VALUES (1,'a'),(2,'b')"],
        parent_mutate=["ALTER TABLE u ADD COLUMN parent_col int DEFAULT 0"],
        branch_mutate=[
            "ALTER TABLE u ADD COLUMN branch_col text",
            "UPDATE u SET branch_col='x' WHERE id=1",
        ],
        check=_column_changes_check,
    ),
    CliScenario(
        name="conflict-aborts-apply",
        setup=["CREATE TABLE u (id int primary key, v text)",
               "INSERT INTO u VALUES (1,'a')"],
        parent_mutate=["UPDATE u SET v='main-wins' WHERE id=1"],
        branch_mutate=["UPDATE u SET v='branch-wins' WHERE id=1"],
        check=_conflict_check,
    ),
    CliScenario(
        name="no-base-warning-on-diff",
        setup=["CREATE TABLE u (id int primary key)",
               "INSERT INTO u VALUES (1)"],
        parent_mutate=[],
        branch_mutate=["INSERT INTO u VALUES (2)"],
        check=_no_base_warning_check,
    ),
    CliScenario(
        name="no-base-refuses-apply",
        setup=["CREATE TABLE u (id int primary key)",
               "INSERT INTO u VALUES (1)"],
        parent_mutate=[],
        branch_mutate=["INSERT INTO u VALUES (2)"],
        check=_no_base_refuses_apply_check,
    ),
    CliScenario(
        name="no-base-allow-2way-apply",
        setup=["CREATE TABLE u (id int primary key)",
               "INSERT INTO u VALUES (1)"],
        parent_mutate=[],
        branch_mutate=["INSERT INTO u VALUES (2)"],
        check=_no_base_allow_2way_apply_check,
    ),
    CliScenario(
        name="apply-then-rerun-falls-back",
        setup=["CREATE TABLE u (id int primary key)",
               "INSERT INTO u VALUES (1)"],
        parent_mutate=[],
        branch_mutate=["INSERT INTO u VALUES (2)"],
        check=_apply_then_rerun_check,
    ),
]


# ---------------- entry points ----------------

def main():
    if not os.path.exists(FOLDOUT_BIN):
        print(f"FAIL: foldout binary not found at {FOLDOUT_BIN}")
        print("       set $FOLDOUT_BIN to override")
        sys.exit(1)
    failures = 0
    for sc in SCENARIOS:
        try:
            sc.run()
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
    print(f"all {len(SCENARIOS)} CLI scenarios passed")


try:
    import pytest

    @pytest.mark.parametrize("scenario", SCENARIOS,
                             ids=[s.name for s in SCENARIOS])
    def test_cli_scenario(scenario):
        scenario.run()
except ImportError:
    pass


if __name__ == "__main__":
    main()
