"""Command-line interface for foldout."""

from __future__ import annotations

import re
import subprocess
import time

import click


def _elapsed(start: float) -> str:
    """Format `time.perf_counter() - start` as `(123 ms)` or `(1.23 s)`.

    Used to tag CLI log lines with how long the preceding step took.
    Keeps it consistent with `page_diff.build_page_index`'s own timing.
    """
    dt_ms = (time.perf_counter() - start) * 1000
    if dt_ms < 1000:
        return f"({dt_ms:.0f} ms)"
    return f"({dt_ms / 1000:.2f} s)"

from . import __version__
from .db import (
    ActiveConnection,
    ActiveConnectionsTimeout,
    SourceHasActiveConnections,
    checkpoint,
    connect,
    copy_database_files,
    get_active_connections,
    terminate_database_connections,
    wait_for_no_connections,
    create_base_database,
    create_branch_database,
    create_snapshot_database,
    database_exists,
    delete_database_record,
    drop_base_for_branch,
    drop_database,
    format_connection_table,
    get_branch_base,
    get_branch_parent,
    get_data_directory,
    get_database_oid,
    get_databases_with_snapshots,
    get_file_copy_method,
    get_pg_major_version,
    get_snapshot_record,
    delete_page_index_for_branch,
    initialize_database,
    list_databases,
    load_page_index,
    lock_source_database,
    log_branch_operation,
    register_base_database,
    register_branch_database,
    register_snapshot_database,
    register_source_database,
    restore_database_from_snapshot,
    save_page_index,
)
from .change_capture import ChangeCaptureInstaller
from . import page_diff


def run_command(command: list[str]) -> None:
    """Run a shell command and echo its output."""
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    click.echo(result.stdout.strip())


def _report_active_connections_and_exit(exc: SourceHasActiveConnections) -> None:
    """Format a SourceHasActiveConnections for the user and abort cleanly.

    Always exits via `click.ClickException` so the caller can convert the
    raise into a non-zero exit code without losing the formatted body.
    """
    click.secho(
        f"\nERROR: '{exc.database_name}' has {len(exc.connections)} "
        f"active connection(s); cannot snapshot/branch safely.",
        fg="red", bold=True, err=True,
    )
    click.echo(format_connection_table(exc.connections), err=True)
    click.echo(
        "\nClose them yourself, or re-run with --force to terminate them.",
        err=True,
    )
    raise click.ClickException(
        f"refused: {len(exc.connections)} active connection(s) on "
        f"'{exc.database_name}'"
    )


def _report_termination_timeout_and_exit(exc: ActiveConnectionsTimeout) -> None:
    """Format an ActiveConnectionsTimeout for the user and abort cleanly."""
    click.secho(
        f"\nERROR: pg_terminate_backend ran on '{exc.database_name}' but "
        f"{len(exc.connections)} connection(s) are still attached after "
        f"{exc.timeout_s}s.",
        fg="red", bold=True, err=True,
    )
    click.echo(format_connection_table(exc.connections), err=True)
    raise click.ClickException(
        f"refused: backends on '{exc.database_name}' did not terminate"
    )


def _probe_clone_strategy() -> bool:
    """Decide whether to skip the manual `cp --reflink` step.

    On PG 18 with `file_copy_method = clone`, PostgreSQL's own
    `CREATE DATABASE ... STRATEGY = FILE_COPY` produces reflinks, so the
    manual copy step in `copy_database_files()` is redundant.

    On PG 18 with the default `copy` method, we emit a yellow warning
    pointing the operator at the GUC and fall back to the manual path
    (which still produces reflinks via `cp --reflink=always` on a CoW
    filesystem). On PG < 18 the GUC doesn't exist and we just use the
    manual path silently — that's the only option pre-18.
    """
    with connect() as conn:
        major = get_pg_major_version(conn)
        method = get_file_copy_method(conn)
    if major >= 18 and method == "clone":
        click.echo(
            "PostgreSQL 18 detected with file_copy_method='clone' — "
            "using native CREATE DATABASE clone (no manual copy)"
        )
        return True
    if major >= 18:
        click.secho(
            f"WARNING: PostgreSQL {major} detected but file_copy_method="
            f"'{method}'. For native CoW clones set "
            f"`file_copy_method = clone` in postgresql.conf. Falling back "
            f"to the manual reflink copy path used on PG < 18.",
            fg="yellow", err=True,
        )
    return False


@click.group()
def cli() -> None:
    """Manage PostgreSQL database snapshots."""
    initialize_database()


@cli.command()
@click.argument("database_name")
@click.argument("branch_name")
@click.option("--force", is_flag=True,
              help="Terminate any active connections to DATABASE_NAME "
                   "before branching. Without --force, foldout refuses "
                   "to branch a database with active connections.")
def branch(database_name: str, branch_name: str, force: bool) -> None:
    """Create a branch of DATABASE_NAME with the given BRANCH_NAME."""
    try:
        t_total = time.perf_counter()
        click.echo(f"Creating branch '{branch_name}' of database '{database_name}'...")

        # Get source database OID
        source_oid = get_database_oid(database_name)
        click.echo(f"Source database OID: {source_oid}")

        # Register source database in fld_databases table
        register_source_database(database_name, source_oid)
        click.echo(f"Registered source database '{database_name}' in fld_databases")

        # Ensure change-capture is installed on the source
        installer = ChangeCaptureInstaller()
        if installer.ensure_installed(database_name):
            click.echo("Installed foldout change-capture on source database")
        else:
            click.echo("foldout change-capture already present on source database")

        use_native_clone = _probe_clone_strategy()

        # Get PostgreSQL data directory
        data_directory = get_data_directory()
        click.echo(f"PostgreSQL data directory: {data_directory}")

        # Capture the parent's page-index BEFORE locking, while we can
        # still connect to it. Once `lock_source_database` sets
        # ALLOW_CONNECTIONS=false, no new sessions are allowed — including
        # ours. The stat values may be a few µs stale relative to lock
        # time, but cross_diff uses them as a "skip if unchanged" filter:
        # mild staleness causes a few extra pages to be scanned (resolving
        # to no-diff), never missed changes.
        parent_index = page_diff.build_page_index(
            data_directory, database_name
        )

        try:
            t_lock = time.perf_counter()
            with lock_source_database(database_name, force=force):
                click.echo(
                    f"Locked source database '{database_name}' for branch "
                    f"{_elapsed(t_lock)}"
                )
                # Explicit CHECKPOINT under the lock so the file mtime/size
                # we capture and the cp source bytes are committed state.
                t = time.perf_counter()
                checkpoint()
                click.echo(f"CHECKPOINT {_elapsed(t)}")
                # Both paths use STRATEGY=FILE_COPY (required — see the
                # comment on _create_database_sql in db.py for why
                # WAL_LOG silently corrupts the snapshot via stale
                # shared_buffers). FILE_COPY forces TWO internal
                # CHECKPOINTs per CREATE DATABASE; on busy clusters
                # these dominate wall time.
                if use_native_clone:
                    t = time.perf_counter()
                    branch_database_name, target_oid = create_branch_database(
                        database_name, branch_name, template=database_name,
                    )
                    click.echo(
                        f"Created branch database '{branch_database_name}' "
                        f"with OID: {target_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    t = time.perf_counter()
                    base_database_name, base_oid = create_base_database(
                        database_name, branch_name, template=database_name,
                    )
                    click.echo(
                        f"Created base snapshot '{base_database_name}' "
                        f"with OID: {base_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    click.echo("Branch and base files cloned by PostgreSQL (no manual copy)")
                else:
                    t = time.perf_counter()
                    branch_database_name, target_oid = create_branch_database(
                        database_name, branch_name
                    )
                    click.echo(
                        f"Created branch database '{branch_database_name}' "
                        f"with OID: {target_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    t = time.perf_counter()
                    base_database_name, base_oid = create_base_database(
                        database_name, branch_name
                    )
                    click.echo(
                        f"Created base snapshot '{base_database_name}' "
                        f"with OID: {base_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    t = time.perf_counter()
                    copy_database_files(data_directory, source_oid, target_oid)
                    click.echo(f"Database files copied successfully {_elapsed(t)}")
                    t = time.perf_counter()
                    copy_database_files(data_directory, source_oid, base_oid)
                    click.echo(f"Base snapshot files copied successfully {_elapsed(t)}")

                # Branch DB is brand-new and not locked — capture its
                # page-index now while we're still under the source lock
                # (so source's state is guaranteed stable past this point).
                t = time.perf_counter()
                branch_index = page_diff.build_page_index(
                    data_directory, branch_database_name
                )
                save_page_index(target_oid, "branch", branch_index)
                save_page_index(target_oid, "parent", parent_index)
                click.echo(
                    f"Saved page-indexes for branch_oid={target_oid} "
                    f"(parent + branch) {_elapsed(t)}"
                )
        except SourceHasActiveConnections as exc:
            _report_active_connections_and_exit(exc)
        except ActiveConnectionsTimeout as exc:
            _report_termination_timeout_and_exit(exc)

        # Ensure change-capture is present on the new branch database as well
        if installer.ensure_installed(branch_database_name):
            click.echo("Installed foldout change-capture on branch database")
        else:
            click.echo("foldout change-capture already present on branch database")
        
        # Register branch database in fld_databases table
        register_branch_database(branch_database_name, target_oid, source_oid)
        click.echo(f"Registered branch '{branch_database_name}' in fld_databases with parent OID {source_oid}")

        # Register the base snapshot and link it to this branch (for 3-way diff)
        register_base_database(base_database_name, base_oid, source_oid, target_oid)
        click.echo(f"Registered base snapshot '{base_database_name}' (linked to branch '{branch_database_name}')")
        
        # Log branch creation operation
        log_branch_operation(source_oid, target_oid, branch_database_name)
        click.echo(f"Logged branch creation operation to fld_log")

        click.echo(
            f"Branch completed successfully: {branch_database_name} "
            f"{_elapsed(t_total)}"
        )

    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error creating branch: {e}", err=True)
        raise click.ClickException(str(e))


@cli.command()
@click.argument("database_name")
@click.option("--force", is_flag=True,
              help="Terminate any active connections to DATABASE_NAME "
                   "before snapshotting. Without --force, foldout refuses "
                   "to snapshot a database with active connections.")
def snapshot(database_name: str, force: bool) -> None:
    """Create a snapshot of DATABASE_NAME."""
    try:
        t_total = time.perf_counter()
        click.echo(f"Creating snapshot of database '{database_name}'...")

        # Get source database OID
        source_oid = get_database_oid(database_name)
        click.echo(f"Source database OID: {source_oid}")

        # Register source database in fld_databases table
        register_source_database(database_name, source_oid)
        click.echo(f"Registered source database '{database_name}' in fld_databases")

        use_native_clone = _probe_clone_strategy()

        # Get PostgreSQL data directory (only needed for the manual path,
        # but harmless to fetch eagerly so the operator sees it in the log).
        data_directory = get_data_directory()
        click.echo(f"PostgreSQL data directory: {data_directory}")

        # Take the database lock for both paths. PG's native CREATE
        # DATABASE ... TEMPLATE also needs no concurrent connections to
        # source, so the lock is useful even on the native-clone path —
        # and it gives us the same --force UX everywhere.
        try:
            t_lock = time.perf_counter()
            with lock_source_database(database_name, force=force):
                click.echo(
                    f"Locked source database '{database_name}' for snapshot "
                    f"{_elapsed(t_lock)}"
                )
                # Explicit CHECKPOINT under the lock — see branch() for
                # the same rationale.
                t = time.perf_counter()
                checkpoint()
                click.echo(f"CHECKPOINT {_elapsed(t)}")
                # Both paths use STRATEGY=FILE_COPY (required — see
                # _create_database_sql in db.py). FILE_COPY forces 2
                # internal CHECKPOINTs per CREATE DATABASE.
                if use_native_clone:
                    t = time.perf_counter()
                    snapshot_name, target_oid = create_snapshot_database(
                        database_name, template=database_name,
                    )
                    click.echo(
                        f"Created snapshot database '{snapshot_name}' "
                        f"with OID: {target_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    click.echo("Database files cloned by PostgreSQL (no manual copy)")
                else:
                    t = time.perf_counter()
                    snapshot_name, target_oid = create_snapshot_database(database_name)
                    click.echo(
                        f"Created snapshot database '{snapshot_name}' "
                        f"with OID: {target_oid} {_elapsed(t)} "
                        f"[FILE_COPY: 2 internal CHECKPOINTs]"
                    )
                    t = time.perf_counter()
                    copy_database_files(data_directory, source_oid, target_oid)
                    click.echo(f"Database files copied successfully {_elapsed(t)}")
        except SourceHasActiveConnections as exc:
            _report_active_connections_and_exit(exc)
        except ActiveConnectionsTimeout as exc:
            _report_termination_timeout_and_exit(exc)

        # Register snapshot database in fld_databases table
        register_snapshot_database(snapshot_name, target_oid, source_oid)
        click.echo(f"Registered snapshot '{snapshot_name}' in fld_databases with parent OID {source_oid}")

        click.echo(
            f"Snapshot completed successfully: {snapshot_name} "
            f"{_elapsed(t_total)}"
        )

    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error creating snapshot: {e}", err=True)
        raise click.ClickException(str(e))


@cli.group()
def snapshots() -> None:
    """Manage snapshots."""


@snapshots.command(name="list")
def list_snapshots() -> None:
    """List available snapshots organized by database."""
    try:
        databases = get_databases_with_snapshots()
        
        if not databases:
            click.echo("No databases with snapshots found")
            return
        
        for db_oid, db_info in databases.items():
            # Show database name with indication if renamed
            db_name = db_info['current_name']
            if db_info['current_name'] != db_info['stored_name']:
                db_name = f"{db_info['current_name']} (was: {db_info['stored_name']})"
            
            click.echo(f"\nDatabase: {db_name} (OID: {db_oid})")
            
            if not db_info['snapshots']:
                click.echo("  No snapshots available")
            else:
                click.echo(f"  {'OID':<10} {'Snapshot Name':<30} {'Created'}")
                click.echo("  " + "-" * 60)
                for snapshot in db_info['snapshots']:
                    created = snapshot['created_at'].strftime('%Y-%m-%d %H:%M:%S') if snapshot['created_at'] else 'Unknown'
                    click.echo(f"  {snapshot['oid']:<10} {snapshot['current_name']:<30} {created}")
    except Exception as e:
        click.echo(f"Error listing snapshots: {e}", err=True)
        raise click.ClickException(str(e))


@snapshots.command(name="delete")
@click.argument("snapshot_name")
def delete_snapshot(snapshot_name: str) -> None:
    """Delete a snapshot by name."""
    try:
        click.echo(f"Deleting snapshot '{snapshot_name}'...")
        
        # Check if snapshot exists in fld_databases with type 'snapshot'
        snapshot_record = get_snapshot_record(snapshot_name)
        if snapshot_record is None:
            click.echo(f"Error: Snapshot '{snapshot_name}' does not exist", err=True)
            raise click.ClickException(f"Snapshot '{snapshot_name}' does not exist")
        
        # Check if the snapshot database exists
        if not database_exists(snapshot_name):
            # Database doesn't exist, remove from fld_databases table and exit with warning
            delete_database_record(snapshot_name)
            click.echo(f"Warning: Database '{snapshot_name}' does not exist but was tracked in metadata. Removed from tracking.")
            return
        
        # Drop the database
        drop_database(snapshot_name)
        click.echo(f"Dropped database '{snapshot_name}'")
        
        # Delete the record from fld_databases
        delete_database_record(snapshot_name)
        click.echo(f"Removed record for '{snapshot_name}' from fld_databases")
        
        click.echo(f"Snapshot '{snapshot_name}' deleted successfully")
        
    except Exception as e:
        click.echo(f"Error deleting snapshot: {e}", err=True)
        raise click.ClickException(str(e))

@snapshots.command(name="restore")
@click.argument("database_name")
@click.argument("snapshot_name")
def restore_snapshot_cmd(database_name: str, snapshot_name: str) -> None:
    """Restore a database from a snapshot's physical files.

    Usage: snapshots restore <database_name> <snapshot_name>
    """
    try:
        click.echo(
            f"Restoring database '{database_name}' from snapshot '{snapshot_name}'..."
        )

        details = restore_database_from_snapshot(database_name, snapshot_name)

        click.echo(
            f"Moved original data directory to: {details['backup_path']}"
        )
        click.echo(
            f"Restored database OID {details['restored_oid']} from snapshot OID {details['snapshot_oid']}"
        )
        click.echo(
            f"Connected successfully. Public tables found: {details['tables_count']}"
        )
        click.echo("Restore completed successfully")

    except Exception as e:
        try:
            base = get_data_directory()
            base_msg = f" Check '{base}/base' for a directory prefixed with 'fld_delete_' containing the original files."
        except Exception:
            base_msg = " Original data files were moved aside with a 'fld_delete_' prefix."
        click.echo(f"Error restoring snapshot: {e}.{base_msg}", err=True)
        raise click.ClickException(str(e))


@cli.group()
def databases() -> None:
    """Manage databases."""


@databases.command(name="list")
def list_databases_cmd() -> None:
    """List databases with their OIDs and names."""
    try:
        dbs = list_databases()
        if not dbs:
            click.echo("No databases found")
            return
        
        click.echo(f"{'OID':<10} {'Database Name'}")
        click.echo("-" * 30)
        for db in dbs:
            click.echo(f"{db['oid']:<10} {db['name']}")
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.ClickException(str(e))


_DIFF_HEADER_VERSION = 1
_FULL_SCAN_WARN_BYTES = 100 * 1024 * 1024  # 100 MB


def _database_exists_byname(name: str) -> bool:
    """Return True if `name` exists as a registered foldout branch."""
    try:
        get_branch_parent(name)
        return True
    except Exception:
        return False


def _emit_sql_header(lines: list[str]) -> None:
    """Print a `-- foldout-diff` header block on stdout (parsed by apply)."""
    click.echo(f"-- foldout-diff v{_DIFF_HEADER_VERSION}")
    for line in lines:
        click.echo(f"-- {line}")
    click.echo("--")


def _emit_warnings(result: dict) -> None:
    """Print any best-effort warnings from a diff result to stderr."""
    for w in result.get("warnings") or []:
        click.secho(
            f"\nWARNING: {w['kind']} on {w['key']}",
            fg="yellow", bold=True, err=True,
        )
        click.echo(f"  {w['note']}", err=True)


def _emit_conflicts_to_stderr(result: dict) -> None:
    """List conflicts on stderr if any. Doesn't raise — caller decides."""
    conflicts = result.get("conflicts") or []
    if not conflicts:
        return
    click.secho(
        f"\n{len(conflicts)} conflict(s) — review before applying:",
        fg="red", bold=True, err=True,
    )
    for conflict in conflicts:
        click.echo(f"  - {conflict['kind']} {conflict.get('key', '')}",
                   err=True)


def _database_size_bytes(database_name: str) -> int:
    """Return `pg_database_size(database_name)`. Reads via the postgres DB."""
    import psycopg as _psycopg
    from .db import get_database_dsn
    params = _psycopg.conninfo.conninfo_to_dict(get_database_dsn())
    params["dbname"] = "postgres"
    target_dsn = _psycopg.conninfo.make_conninfo(**params)
    with _psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_database_size(%s)", (database_name,))
            row = cur.fetchone()
    return int(row[0]) if row else 0


@cli.command()
@click.argument("left_name")
@click.argument("right_name", required=False)
def diff(left_name: str, right_name: str | None) -> None:
    """Show row-level changes as a SQL diff (stdout) plus summary (stderr).

    \b
    Two forms:
      foldout diff <branch_name>
          Diffs the registered branch against its parent. Uses the stored
          page-index for fast filtering. Falls back to 2-way diff if there
          is no merge base.

      foldout diff <left_db> <right_db>
          Arbitrary two-database diff: walks every page of <left_db> and
          compares against <right_db>. No registered relationship needed.
          Slow on large DBs (O(<left_db> size)) — a warning is printed
          to stderr if <left_db> exceeds ~100 MB.

    SQL goes to stdout (so `foldout diff feature > diff.sql` Just Works).
    Headers, summary, warnings, and conflicts go to stderr. Apply the
    output with `foldout apply diff.sql`.
    """
    try:
        if right_name is None:
            _diff_registered_branch(left_name)
        else:
            _diff_arbitrary_pair(left_name, right_name)
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.ClickException(str(e))


def _diff_registered_branch(branch_name: str) -> None:
    """Diff a registered foldout branch against its parent (2-way or 3-way)."""
    branch_oid, parent_oid, parent_name = get_branch_parent(branch_name)
    data_directory = get_data_directory()
    base = get_branch_base(branch_name)
    branch_index = load_page_index(branch_oid, "branch")
    if branch_index is None:
        raise click.ClickException(
            f"No page-index found for branch '{branch_name}' "
            f"(branch_oid={branch_oid}). Re-create the branch with "
            f"`foldout branch {parent_name} {branch_name}`."
        )

    mode = "3-way" if base is not None else "2-way"
    click.echo(
        f"Diffing branch '{branch_name}' against parent '{parent_name}' "
        f"({mode})",
        err=True,
    )
    if base is not None:
        click.echo(f"  base:    {base[1]}", err=True)
    else:
        click.secho(
            "WARNING: no merge base — running 2-way diff",
            fg="yellow", bold=True, err=True,
        )
        click.echo(
            "  Without a base we can't tell branch intent from parent\n"
            "  drift. The SQL may propose DROPs for objects the parent\n"
            "  added since this branch was created. Review before apply.",
            err=True,
        )
    click.echo(f"  pgdata:  {data_directory}", err=True)
    click.echo("", err=True)

    if base is not None:
        parent_index = load_page_index(branch_oid, "parent")
        result = page_diff.cross_diff_3way(
            data_directory, parent_name, branch_name, base[1],
            branch_index, parent_index=parent_index, verbose=False,
        )
    else:
        result = page_diff.cross_diff(
            data_directory, parent_name, branch_name,
            branch_index, verbose=False,
        )

    header_lines = [
        f"parent: {parent_name}",
        f"branch: {branch_name}",
        f"mode: {mode}",
    ]
    if base is not None:
        header_lines.append(f"base: {base[1]}")
    _emit_sql_header(header_lines)
    for stmt in result["sql"]:
        click.echo(stmt)

    _emit_warnings(result)
    _emit_conflicts_to_stderr(result)

    n_sql = len(result["sql"])
    n_conf = len(result.get("conflicts") or [])
    click.echo(
        f"\n{n_sql} SQL statement(s), {n_conf} conflict(s), "
        f"{result.get('elapsed_ms', 0):.0f} ms",
        err=True,
    )


def _diff_arbitrary_pair(left_name: str, right_name: str) -> None:
    """Diff two arbitrary databases — no registered relationship, no filter."""
    data_directory = get_data_directory()
    left_size = _database_size_bytes(left_name)

    click.echo(
        f"Diffing '{left_name}' against '{right_name}' "
        f"(full scan — no shared branch history)",
        err=True,
    )
    if left_size > _FULL_SCAN_WARN_BYTES:
        click.secho(
            f"WARNING: '{left_name}' is {left_size / (1024 * 1024):.1f} MB — "
            f"full scan will read every page. This can take a while.",
            fg="yellow", bold=True, err=True,
        )
    click.echo(f"  pgdata: {data_directory}", err=True)
    click.echo("", err=True)

    # No index → full scan: every page of `left_name` is a candidate.
    result = page_diff.cross_diff(
        data_directory, right_name, left_name, None, verbose=False,
    )

    header_lines = [
        f"parent: {right_name}",
        f"branch: {left_name}",
        "mode: full-scan",
    ]
    _emit_sql_header(header_lines)
    for stmt in result["sql"]:
        click.echo(stmt)

    _emit_warnings(result)
    _emit_conflicts_to_stderr(result)

    n_sql = len(result["sql"])
    n_conf = len(result.get("conflicts") or [])
    click.echo(
        f"\n{n_sql} SQL statement(s), {n_conf} conflict(s), "
        f"{result.get('elapsed_ms', 0):.0f} ms",
        err=True,
    )


_DIFF_HEADER_FIELD_RE = re.compile(
    r"^--\s+(?P<key>[a-z][a-z0-9_-]*):\s*(?P<value>.+?)\s*$"
)


def _parse_diff_header(text: str) -> dict[str, str]:
    """Extract `-- foldout-diff` header key/value lines from a diff file.

    Reads `--` comment lines at the top of the file up to the first
    non-comment line; pulls out simple `key: value` pairs (parent, branch,
    mode, base). Returns an empty dict if no `-- foldout-diff vN` marker
    appears in the first 50 lines — caller decides whether that's fatal.
    """
    fields: dict[str, str] = {}
    found_marker = False
    for index, line in enumerate(text.splitlines()):
        if index > 50:
            break
        stripped = line.strip()
        if not stripped:
            continue
        if not stripped.startswith("--"):
            break
        if stripped.lower().startswith("-- foldout-diff"):
            found_marker = True
            continue
        match = _DIFF_HEADER_FIELD_RE.match(stripped)
        if match:
            fields[match.group("key")] = match.group("value")
    if not found_marker:
        return {}
    return fields


@cli.command()
@click.argument("sql_file", type=click.Path(exists=True, dir_okay=False,
                                             readable=True))
@click.option("--target", "target_override", default=None,
              help="Override the target database from the diff header.")
def apply(sql_file: str, target_override: str | None) -> None:
    """Apply a SQL diff (produced by `foldout diff`) to its target database.

    The target DB is taken from the `-- parent: <name>` header line that
    `foldout diff` writes at the top of its output. Pass `--target <db>`
    to override (useful when applying to a sibling DB or a freshly-named
    parent).

    This command is pure: it runs the SQL and nothing else. It does NOT
    drop the branch, the merge base, or any page-index rows. Use
    `foldout delete-branch <name>` to clean up after a successful merge.
    """
    try:
        with open(sql_file, "r", encoding="utf-8") as fh:
            text = fh.read()

        header = _parse_diff_header(text)
        if not header and target_override is None:
            raise click.ClickException(
                f"{sql_file}: no `-- foldout-diff vN` header found, and "
                f"no --target was given. Either re-generate the file with "
                f"`foldout diff` or pass --target <db>."
            )

        target = target_override or header.get("parent")
        if not target:
            raise click.ClickException(
                f"{sql_file}: header missing `-- parent: <db>` and no "
                f"--target supplied. Don't know where to apply."
            )

        if header.get("mode"):
            click.echo(
                f"apply: {sql_file} -> '{target}' "
                f"(mode={header['mode']})",
                err=True,
            )
        else:
            click.echo(f"apply: {sql_file} -> '{target}'", err=True)

        import psycopg
        from .db import get_database_dsn
        params = psycopg.conninfo.conninfo_to_dict(get_database_dsn())
        params["dbname"] = target
        target_dsn = psycopg.conninfo.make_conninfo(**params)

        executed = 0
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                # psycopg accepts a single execute() with multiple
                # statements; the whole file runs in one transaction so
                # an error rolls back everything.
                cur.execute(text)
                executed = cur.rowcount  # rowcount of the last statement
            conn.commit()

        click.echo(
            f"Applied {sql_file} successfully (last statement affected "
            f"{executed} row(s)). Use `foldout delete-branch` to clean up.",
            err=True,
        )
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error applying {sql_file}: {e}", err=True)
        raise click.ClickException(str(e))


@cli.command(name="delete-branch")
@click.argument("branch_name")
@click.option("--force", is_flag=True,
              help="Terminate any active connections to the branch or its "
                   "base before dropping. Without --force, delete-branch "
                   "refuses if anything is connected — and importantly, "
                   "performs NO destructive operation in that case (so the "
                   "branch and base stay in a consistent state).")
def delete_branch(branch_name: str, force: bool) -> None:
    """Drop a branch database, its merge base, and all foldout metadata.

    Removes (in order, only after the safety check below has passed):
      1. The branch DB.
      2. The merge base DB (`__base__<branch>`) and its `fld_databases` row.
      3. The branch's `fld_databases` row.
      4. Every `fld_page_index` row tied to the branch's OID.

    Safety: before doing anything destructive, we check `pg_stat_activity`
    for backends connected to either the branch or its base. If any are
    present and `--force` was not passed, we abort cleanly — nothing is
    dropped, metadata stays intact, the user can retry once they close
    the offending sessions. With `--force` we terminate them first.
    """
    try:
        # Resolve everything from metadata before touching anything.
        branch_oid, parent_oid, parent_name = get_branch_parent(branch_name)
        base = get_branch_base(branch_name)
        base_name = base[1] if base else None

        # Collect any active connections on BOTH databases up front so we
        # can decide go/no-go atomically. The historical bug here was
        # dropping the base first and then failing on the branch — that
        # left a half-cleaned state. We now decide before touching disk.
        active: list[ActiveConnection] = []
        active.extend(get_active_connections(branch_name))
        if base_name:
            active.extend(get_active_connections(base_name))

        if active and not force:
            click.secho(
                f"\nERROR: cannot delete-branch '{branch_name}' — "
                f"{len(active)} active connection(s) on the branch or its base.",
                fg="red", bold=True, err=True,
            )
            click.echo(format_connection_table(active), err=True)
            click.echo(
                "\nClose them yourself, or re-run with --force to terminate them.\n"
                "Nothing has been dropped.",
                err=True,
            )
            raise click.ClickException(
                f"refused: {len(active)} active connection(s) on branch/base"
            )

        if active:
            # --force path: kick everyone off both DBs, then verify.
            for db in (branch_name, base_name):
                if not db:
                    continue
                terminate_database_connections(db)
            for db in (branch_name, base_name):
                if not db:
                    continue
                try:
                    wait_for_no_connections(db, timeout_s=10.0)
                except ActiveConnectionsTimeout as exc:
                    _report_termination_timeout_and_exit(exc)
            click.echo(
                f"Terminated {len(active)} connection(s) "
                f"on {branch_name}/{base_name or '(no base)'}"
            )

        # Drop branch DB first. If anything goes wrong here we haven't
        # touched the base yet — recoverable.
        if database_exists(branch_name):
            drop_database(branch_name)
            click.echo(f"Dropped branch database '{branch_name}'")

        # Drop base + clean its metadata row.
        if base_name:
            dropped = drop_base_for_branch(branch_name)
            if dropped:
                click.echo(f"Dropped base snapshot '{dropped}'")

        # Clean up the branch's own metadata.
        delete_database_record(branch_name)
        click.echo(f"Removed '{branch_name}' from fld_databases")

        removed = delete_page_index_for_branch(branch_oid)
        if removed:
            click.echo(
                f"Removed {removed} page-index row(s) for branch_oid={branch_oid}"
            )

        click.echo(f"delete-branch '{branch_name}' complete")
    except click.ClickException:
        raise
    except Exception as e:
        click.echo(f"Error deleting branch '{branch_name}': {e}", err=True)
        raise click.ClickException(str(e))


@cli.command()
def version() -> None:
    """Display the foldout version."""
    click.echo(__version__)
