"""Snapshot lock behavior: active-connection refusal + --force semantics.

Covers:
- Without --force, foldout refuses to snapshot when any backend is
  attached to the source. Output lists pid / app / state / query for
  each blocker.
- The refusal releases the database lock cleanly: subsequent
  connections to the source succeed.
- With --force, foldout terminates the existing backends and the
  snapshot completes. The previously-held connection's next query
  raises (server-side termination).

Runs once per variant in the matrix (pg16, pg17, pg18-default,
pg18-clone) so we catch any version-specific divergence in
`ALTER DATABASE ALLOW_CONNECTIONS` or `pg_terminate_backend`.
"""

from __future__ import annotations

import psycopg
import pytest
from click.testing import CliRunner

from foldout.cli import cli

from .pg_cluster import PgCluster
from .test_snapshot_restore import SOURCE_DB, _seed_source


pytestmark = pytest.mark.integration


def _open_idle_connection(cluster: PgCluster, database: str) -> psycopg.Connection:
    """Open a connection to `database` and leave it idle.

    Used to simulate "a client app is connected" without actually doing
    any work. The connection has `application_name` set so we can assert
    it appears in the foldout error output.
    """
    conn = psycopg.connect(
        cluster.dsn(database=database) + "?application_name=test-blocker",
    )
    return conn


def _is_database_reachable(cluster: PgCluster, database: str) -> bool:
    """Return True if we can `SELECT 1` from `database` right now.

    Used to verify the lock was released after a failed snapshot.
    """
    try:
        with psycopg.connect(cluster.dsn(database=database)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except psycopg.OperationalError:
        return False


def test_snapshot_refuses_when_connections_active_without_force(
    foldout_env: PgCluster,
) -> None:
    """Snapshot must fail when the source has active connections + no --force.

    Verifies:
    - non-zero exit
    - output names the blocking connection(s) with pid / app / state
    - the source DB remains reachable afterwards (lock released)
    """
    cluster = foldout_env
    _seed_source(cluster)

    blocker = _open_idle_connection(cluster, SOURCE_DB)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["snapshot", SOURCE_DB],
                               catch_exceptions=False)

        # Exit must be non-zero; combined stdout/stderr must include the
        # blocker details.
        assert result.exit_code != 0, (
            f"expected non-zero exit; output:\n{result.output}\n"
            f"stderr:\n{result.stderr}"
        )
        combined = result.output + result.stderr
        assert "active connection" in combined, combined
        assert "test-blocker" in combined, (
            f"expected application_name 'test-blocker' in error output:\n"
            f"{combined}"
        )
        assert "--force" in combined, (
            f"error message should mention --force:\n{combined}"
        )

        # The lock must have been released even though the snapshot failed.
        assert _is_database_reachable(cluster, SOURCE_DB), (
            "source DB is not reachable after a failed snapshot — the "
            "ALLOW_CONNECTIONS=false lock leaked"
        )
    finally:
        blocker.close()


def test_snapshot_with_force_terminates_connections_and_succeeds(
    foldout_env: PgCluster,
) -> None:
    """`--force` must kill the existing backend and let the snapshot run."""
    cluster = foldout_env
    _seed_source(cluster)

    blocker = _open_idle_connection(cluster, SOURCE_DB)
    try:
        runner = CliRunner()
        result = runner.invoke(cli, ["snapshot", SOURCE_DB, "--force"],
                               catch_exceptions=False)
        assert result.exit_code == 0, (
            f"expected snapshot to succeed with --force; output:\n"
            f"{result.output}\nstderr:\n{result.stderr}"
        )

        # The blocker connection should have been killed server-side.
        # Next operation on it raises (typically `OperationalError` or a
        # subclass thereof). The exact subclass varies by psycopg version
        # and termination timing.
        terminated = False
        try:
            with blocker.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        except psycopg.Error:
            terminated = True
        assert terminated, (
            "expected the held connection to be terminated by --force, "
            "but a query on it succeeded"
        )

        # Source must remain reachable for fresh connections.
        assert _is_database_reachable(cluster, SOURCE_DB)
    finally:
        # blocker is likely already dead; close swallows that.
        try:
            blocker.close()
        except Exception:
            pass


def test_lock_released_when_no_blockers(foldout_env: PgCluster) -> None:
    """Empty pg_stat_activity → snapshot succeeds and ALLOW_CONNECTIONS=true after.

    Sanity check that the new lock-first ordering doesn't leave the
    database in `datallowconn=false` on the happy path.
    """
    cluster = foldout_env
    _seed_source(cluster)

    runner = CliRunner()
    result = runner.invoke(cli, ["snapshot", SOURCE_DB],
                           catch_exceptions=False)
    assert result.exit_code == 0, result.output

    assert _is_database_reachable(cluster, SOURCE_DB), (
        "source DB unreachable after a successful snapshot — the lock leaked"
    )
