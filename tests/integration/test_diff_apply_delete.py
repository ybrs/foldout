"""End-to-end tests for the v0.2 diff/apply/delete-branch workflow.

Covers:
  - `foldout diff <branch>` writes a SQL diff to stdout with a
    `-- foldout-diff` header on stdout and progress/summary on stderr.
  - `foldout diff <left> <right>` works on two arbitrary databases with
    no shared branch history (full-scan path).
  - `foldout apply <file>` reads the header, applies the SQL to the
    `parent` field; `--target` overrides.
  - `foldout delete-branch <name>` drops branch DB + base DB +
    page-index rows in one go.

Runs on every cluster variant. Each test starts with a clean cluster
(per the `_clean_between_tests` autouse fixture) so it can create
its own branches without colliding with other tests.
"""

from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from click.testing import CliRunner

from foldout.cli import cli

from .pg_cluster import PgCluster


pytestmark = pytest.mark.integration


SOURCE_DB = "appdb"
BRANCH_DB = "feature_x"
BASE_DB = f"__base__{BRANCH_DB}"


def _seed_source(cluster: PgCluster) -> None:
    """Create SOURCE_DB with a tiny `items` table for diff scenarios."""
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


def _row_count(cluster: PgCluster, database: str, table: str) -> int:
    """Count rows in `table` in `database`."""
    with psycopg.connect(cluster.dsn(database=database)) as conn:
        with conn.cursor() as cur:
            cur.execute(f'SELECT count(*) FROM "{table}"')
            return int(cur.fetchone()[0])


def _make_branch(runner: CliRunner) -> None:
    """Invoke `foldout branch` and assert it succeeded."""
    result = runner.invoke(cli, ["branch", SOURCE_DB, BRANCH_DB],
                           catch_exceptions=False)
    assert result.exit_code == 0, (
        f"foldout branch failed:\nstdout:\n{result.output}\n"
        f"stderr:\n{result.stderr}"
    )


def test_diff_writes_header_to_stdout_and_summary_to_stderr(
    foldout_env: PgCluster,
) -> None:
    """`foldout diff <branch>` separates SQL output from progress."""
    cluster = foldout_env
    _seed_source(cluster)

    runner = CliRunner()
    _make_branch(runner)

    # Mutate the branch so the diff has actual SQL to emit.
    cluster.psql(
        "INSERT INTO items VALUES (4,'four'); "
        "UPDATE items SET label='ONE' WHERE id=1;",
        database=BRANCH_DB,
    )

    result = runner.invoke(cli, ["diff", BRANCH_DB], catch_exceptions=False)
    assert result.exit_code == 0, (
        f"diff failed:\nstdout:\n{result.output}\nstderr:\n{result.stderr}"
    )

    # Header + SQL on stdout (use .stdout, not .output which is mixed).
    stdout = result.stdout
    assert stdout.startswith("-- foldout-diff v1"), (
        f"stdout should start with the header marker, got:\n{stdout}"
    )
    assert f"-- parent: {SOURCE_DB}" in stdout
    assert f"-- branch: {BRANCH_DB}" in stdout
    assert "-- mode: 3-way" in stdout, (
        f"branch should have a merge base, expected 3-way:\n{stdout}"
    )
    assert "INSERT INTO" in stdout or "UPDATE" in stdout, (
        f"expected DML in stdout, got:\n{stdout}"
    )

    # Diagnostics on stderr, not stdout.
    assert "Diffing branch" in result.stderr
    assert "SQL statement(s)" in result.stderr
    assert "Diffing branch" not in stdout


def test_diff_two_arbitrary_databases_full_scan(
    foldout_env: PgCluster,
) -> None:
    """`foldout diff <left> <right>` works without a branch relationship."""
    cluster = foldout_env
    cluster.create_database("alpha")
    cluster.create_database("beta")
    cluster.psql(
        "CREATE TABLE items (id int primary key, label text); "
        "INSERT INTO items VALUES (1,'a'),(2,'b');",
        database="alpha",
    )
    cluster.psql(
        "CREATE TABLE items (id int primary key, label text); "
        "INSERT INTO items VALUES (1,'a'),(2,'CHANGED'),(3,'extra');",
        database="beta",
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["diff", "beta", "alpha"],
                           catch_exceptions=False)
    assert result.exit_code == 0, (
        f"two-arg diff failed:\nstdout:\n{result.output}\n"
        f"stderr:\n{result.stderr}"
    )

    stdout = result.stdout
    assert "-- mode: full-scan" in stdout, (
        f"expected full-scan mode in header:\n{stdout}"
    )
    assert "full scan" in result.stderr.lower(), (
        f"expected full-scan warning on stderr:\n{result.stderr}"
    )
    # beta has the extra row + the updated label, so SQL must reflect both.
    assert "INSERT" in stdout and "UPDATE" in stdout, (
        f"expected INSERT and UPDATE in the SQL:\n{stdout}"
    )


def test_apply_runs_diff_against_parent_from_header(
    foldout_env: PgCluster,
    tmp_path: Path,
) -> None:
    """`foldout apply diff.sql` parses the header and applies to parent."""
    cluster = foldout_env
    _seed_source(cluster)

    runner = CliRunner()
    _make_branch(runner)

    # Mutate the branch.
    cluster.psql(
        "INSERT INTO items VALUES (4,'four'); "
        "UPDATE items SET label='ONE' WHERE id=1; "
        "DELETE FROM items WHERE id=3;",
        database=BRANCH_DB,
    )

    diff_result = runner.invoke(cli, ["diff", BRANCH_DB],
                                catch_exceptions=False)
    assert diff_result.exit_code == 0, diff_result.stderr

    diff_file = tmp_path / "diff.sql"
    diff_file.write_text(diff_result.stdout)

    # Source should NOT yet have the changes.
    assert _labels(cluster, SOURCE_DB) == ["one", "two", "three"]

    apply_result = runner.invoke(cli, ["apply", str(diff_file)],
                                 catch_exceptions=False)
    assert apply_result.exit_code == 0, (
        f"apply failed:\nstdout:\n{apply_result.output}\n"
        f"stderr:\n{apply_result.stderr}"
    )

    # Source should now match the branch.
    assert _labels(cluster, SOURCE_DB) == _labels(cluster, BRANCH_DB)
    # apply must NOT have dropped the base or branch.
    assert _row_count(cluster, BRANCH_DB, "items") == _row_count(
        cluster, SOURCE_DB, "items"
    )
    # Base DB still exists.
    with psycopg.connect(cluster.dsn(database="postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (BASE_DB,)
            )
            assert cur.fetchone() is not None, (
                "apply should not have dropped the merge base"
            )


def test_apply_target_override(
    foldout_env: PgCluster,
    tmp_path: Path,
) -> None:
    """`foldout apply --target <db>` ignores the parent in the header."""
    cluster = foldout_env
    _seed_source(cluster)

    runner = CliRunner()
    _make_branch(runner)

    cluster.psql(
        "INSERT INTO items VALUES (99,'override');",
        database=BRANCH_DB,
    )

    diff_result = runner.invoke(cli, ["diff", BRANCH_DB],
                                catch_exceptions=False)
    assert diff_result.exit_code == 0, diff_result.stderr

    diff_file = tmp_path / "diff.sql"
    diff_file.write_text(diff_result.stdout)

    # Create a sibling database to apply against instead of the parent.
    cluster.create_database("sibling")
    cluster.psql(
        "CREATE TABLE items (id int primary key, label text); "
        "INSERT INTO items VALUES (1,'one'),(2,'two'),(3,'three');",
        database="sibling",
    )

    apply_result = runner.invoke(
        cli, ["apply", str(diff_file), "--target", "sibling"],
        catch_exceptions=False,
    )
    assert apply_result.exit_code == 0, (
        f"apply --target failed:\nstdout:\n{apply_result.output}\n"
        f"stderr:\n{apply_result.stderr}"
    )

    # Sibling got the new row; original parent is untouched.
    assert _row_count(cluster, "sibling", "items") == 4
    assert _row_count(cluster, SOURCE_DB, "items") == 3


def _open_idle_branch_connection(cluster: PgCluster,
                                 database: str) -> psycopg.Connection:
    """Open + leave-idle a connection to `database` for delete-branch tests."""
    return psycopg.connect(
        cluster.dsn(database=database) + "?application_name=test-blocker"
    )


def test_delete_branch_refuses_when_branch_has_connections(
    foldout_env: PgCluster,
) -> None:
    """No --force + a live connection on the branch → fail, nothing dropped."""
    cluster = foldout_env
    _seed_source(cluster)
    runner = CliRunner()
    _make_branch(runner)

    blocker = _open_idle_branch_connection(cluster, BRANCH_DB)
    try:
        result = runner.invoke(cli, ["delete-branch", BRANCH_DB],
                               catch_exceptions=False)
        assert result.exit_code != 0, (
            f"expected non-zero exit on active connection; output:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        combined = result.stdout + result.stderr
        assert "active connection" in combined, combined
        assert "test-blocker" in combined, combined
        assert "--force" in combined, combined
        assert "Nothing has been dropped" in combined, combined

        # Crucially: BOTH databases must still exist. The old bug dropped
        # the base before failing on the branch.
        with psycopg.connect(cluster.dsn(database="postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_database "
                    "WHERE datname IN (%s, %s)", (BRANCH_DB, BASE_DB),
                )
                assert cur.fetchone()[0] == 2, (
                    "delete-branch refusal must leave branch AND base intact"
                )
    finally:
        blocker.close()


def test_delete_branch_with_force_terminates_and_drops(
    foldout_env: PgCluster,
) -> None:
    """--force kicks the connection and finishes the deletion cleanly."""
    cluster = foldout_env
    _seed_source(cluster)
    runner = CliRunner()
    _make_branch(runner)

    blocker = _open_idle_branch_connection(cluster, BRANCH_DB)
    try:
        result = runner.invoke(cli, ["delete-branch", BRANCH_DB, "--force"],
                               catch_exceptions=False)
        assert result.exit_code == 0, (
            f"delete-branch --force failed:\nstdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )

        # Both DBs gone, metadata clean.
        with psycopg.connect(cluster.dsn(database="postgres")) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_database "
                    "WHERE datname IN (%s, %s)", (BRANCH_DB, BASE_DB),
                )
                assert cur.fetchone()[0] == 0
    finally:
        try:
            blocker.close()
        except Exception:
            pass


def test_delete_branch_removes_branch_base_and_page_index(
    foldout_env: PgCluster,
) -> None:
    """`foldout delete-branch` cleans up DB, base, and metadata."""
    cluster = foldout_env
    _seed_source(cluster)

    runner = CliRunner()
    _make_branch(runner)

    # Verify pre-state.
    with psycopg.connect(cluster.dsn(database="postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_database "
                "WHERE datname IN (%s, %s)", (BRANCH_DB, BASE_DB),
            )
            assert cur.fetchone()[0] == 2

    with psycopg.connect(cluster.dsn(database="foldout")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM fld_page_index "
                "WHERE branch_oid = (SELECT oid FROM pg_database "
                "                    WHERE datname = %s)",
                (BRANCH_DB,),
            )
            assert cur.fetchone()[0] == 2, (
                "expected 2 page-index rows (branch + parent) pre-delete"
            )

    result = runner.invoke(cli, ["delete-branch", BRANCH_DB],
                           catch_exceptions=False)
    assert result.exit_code == 0, (
        f"delete-branch failed:\nstdout:\n{result.output}\n"
        f"stderr:\n{result.stderr}"
    )

    # Both DBs gone.
    with psycopg.connect(cluster.dsn(database="postgres")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_database "
                "WHERE datname IN (%s, %s)", (BRANCH_DB, BASE_DB),
            )
            assert cur.fetchone()[0] == 0, (
                "branch and base DBs should be dropped"
            )

    # fld_databases and fld_page_index purged for this branch.
    with psycopg.connect(cluster.dsn(database="foldout")) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM fld_databases WHERE datname = %s",
                (BRANCH_DB,),
            )
            assert cur.fetchone()[0] == 0, (
                "fld_databases row for branch should be gone"
            )
            cur.execute("SELECT count(*) FROM fld_page_index")
            assert cur.fetchone()[0] == 0, (
                "no page-index rows should remain for this branch"
            )
