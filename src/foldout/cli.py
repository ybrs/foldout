"""Command-line interface for foldout."""

from __future__ import annotations

import subprocess

import click

from . import __version__
from .db import (
    connect,
    copy_database_files,
    create_base_database,
    create_branch_database,
    create_snapshot_database,
    database_exists,
    database_write_lock,
    delete_database_record,
    drop_base_for_branch,
    drop_database,
    get_branch_base,
    get_branch_parent,
    get_branch_parent_snapshot_path,
    get_branch_snapshot_path,
    get_data_directory,
    get_database_oid,
    get_databases_with_snapshots,
    get_file_copy_method,
    get_pg_major_version,
    get_snapshot_record,
    initialize_database,
    list_databases,
    log_branch_operation,
    register_base_database,
    register_branch_database,
    register_snapshot_database,
    register_source_database,
    restore_database_from_snapshot,
    should_use_native_clone,
)
from .change_capture import ChangeCaptureInstaller
from . import page_diff


def run_command(command: list[str]) -> None:
    """Run a shell command and echo its output."""
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    click.echo(result.stdout.strip())


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
def branch(database_name: str, branch_name: str) -> None:
    """Create a branch of DATABASE_NAME with the given BRANCH_NAME."""
    try:
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

        if use_native_clone:
            # PG clones the source itself for both target and base.
            branch_database_name, target_oid = create_branch_database(
                database_name, branch_name, template=database_name,
            )
            click.echo(f"Created branch database '{branch_database_name}' with OID: {target_oid}")

            base_database_name, base_oid = create_base_database(
                database_name, branch_name, template=database_name,
            )
            click.echo(f"Created base snapshot '{base_database_name}' with OID: {base_oid}")

            click.echo("Branch and base files cloned by PostgreSQL (no manual copy)")
            # The parent page-diff snapshot only reads stat() values on the
            # source's files, but we keep it under a brief lock so the
            # recorded (size, mtime) matches what was just cloned.
            with database_write_lock(database_name):
                parent_snap_path = get_branch_parent_snapshot_path(target_oid)
                page_diff.snapshot(data_directory, database_name, str(parent_snap_path))
                click.echo(f"Saved parent snapshot: {parent_snap_path}")
        else:
            # Empty FILE_COPY DBs, then overwrite both with manual reflink cps.
            branch_database_name, target_oid = create_branch_database(database_name, branch_name)
            click.echo(f"Created branch database '{branch_database_name}' with OID: {target_oid}")

            base_database_name, base_oid = create_base_database(database_name, branch_name)
            click.echo(f"Created base snapshot '{base_database_name}' with OID: {base_oid}")

            with database_write_lock(database_name):
                click.echo(f"Acquired write lock on database '{database_name}'")

                copy_database_files(data_directory, source_oid, target_oid)
                click.echo("Database files copied successfully")

                copy_database_files(data_directory, source_oid, base_oid)
                click.echo("Base snapshot files copied successfully")

                parent_snap_path = get_branch_parent_snapshot_path(target_oid)
                page_diff.snapshot(data_directory, database_name, str(parent_snap_path))
                click.echo(f"Saved parent snapshot: {parent_snap_path}")

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

        # Take a page-diff snapshot of the branch so `fld diff <branch>`
        # can later report row-level changes against the parent.
        snap_path = get_branch_snapshot_path(target_oid)
        page_diff.snapshot(data_directory, branch_database_name, str(snap_path))
        click.echo(f"Saved page-diff snapshot: {snap_path}")

        click.echo(f"Branch completed successfully: {branch_database_name}")
        
    except Exception as e:
        click.echo(f"Error creating branch: {e}", err=True)
        raise click.ClickException(str(e))


@cli.command()
@click.argument("database_name")
def snapshot(database_name: str) -> None:
    """Create a snapshot of DATABASE_NAME."""
    try:
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

        if use_native_clone:
            # PG clones source itself. CREATE DATABASE ... TEMPLATE source
            # locks out new connections to the source until it finishes and
            # fails if any other backend is already connected.
            snapshot_name, target_oid = create_snapshot_database(
                database_name, template=database_name,
            )
            click.echo(f"Created snapshot database '{snapshot_name}' with OID: {target_oid}")
            click.echo("Database files cloned by PostgreSQL (no manual copy)")
        else:
            # Empty FILE_COPY DB, then overwrite with manual reflink cp.
            snapshot_name, target_oid = create_snapshot_database(database_name)
            click.echo(f"Created snapshot database '{snapshot_name}' with OID: {target_oid}")
            with database_write_lock(database_name):
                click.echo(f"Acquired write lock on database '{database_name}'")
                copy_database_files(data_directory, source_oid, target_oid)
                click.echo("Database files copied successfully")

        # Register snapshot database in fld_databases table
        register_snapshot_database(snapshot_name, target_oid, source_oid)
        click.echo(f"Registered snapshot '{snapshot_name}' in fld_databases with parent OID {source_oid}")

        click.echo(f"Snapshot completed successfully: {snapshot_name}")

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


@cli.command()
@click.argument("branch_name")
@click.option("--apply", is_flag=True,
              help="Apply the generated SQL to the parent database "
                   "(default: print SQL only).")
@click.option("--sql-only", is_flag=True,
              help="Print only the SQL statements, no summary.")
@click.option("--allow-2way-apply", is_flag=True,
              help="Allow --apply when the branch has no merge base. "
                   "USE WITH CAUTION: may propose DROPs for objects the "
                   "parent added independently since the branch was created.")
def diff(branch_name: str, apply: bool, sql_only: bool,
         allow_2way_apply: bool) -> None:
    """Show row-level changes on BRANCH_NAME relative to its parent.

    Uses the page-diff snapshot taken at branch time to identify exactly
    which pages changed, then emits INSERT/UPDATE/DELETE SQL.
    """
    try:
        branch_oid, parent_oid, parent_name = get_branch_parent(branch_name)
        snap_path = get_branch_snapshot_path(branch_oid)
        if not snap_path.exists():
            raise click.ClickException(
                f"No page-diff snapshot for branch '{branch_name}' at {snap_path}. "
                f"It must have been created by `vka branch` after this feature was added."
            )

        data_directory = get_data_directory()
        base = get_branch_base(branch_name)

        if not sql_only:
            click.echo(f"Diffing branch '{branch_name}' against parent '{parent_name}'")
            if base is not None:
                click.echo(f"  base:     {base[1]} (3-way merge)")
            else:
                # Prominent warning on stderr — yellow/bold if TTY.
                click.secho(
                    "WARNING: no merge base for this branch — running 2-way diff",
                    fg="yellow", bold=True, err=True,
                )
                click.secho(
                    "  Without a merge base we cannot tell branch's intent apart\n"
                    "  from parent's independent changes. The diff below may\n"
                    "  propose DROPs for objects the parent added since this\n"
                    "  branch was created.\n"
                    f"  To enable 3-way merge, drop and recreate the branch:\n"
                    f"     DROP DATABASE \"{branch_name}\";\n"
                    f"     vka branch {parent_name} {branch_name}",
                    fg="yellow", err=True,
                )
                click.echo(f"  base:     (none — 2-way diff)")
            click.echo(f"  snapshot: {snap_path}")
            click.echo(f"  pgdata:   {data_directory}")
            click.echo()

        if base is not None:
            parent_snap_path = get_branch_parent_snapshot_path(branch_oid)
            result = page_diff.cross_diff_3way(
                data_directory, parent_name, branch_name, base[1], str(snap_path),
                parent_snap_path=str(parent_snap_path) if parent_snap_path.exists() else None,
                verbose=not sql_only,
            )
        else:
            result = page_diff.cross_diff(
                data_directory, parent_name, branch_name, str(snap_path),
                verbose=not sql_only,
            )

        if sql_only:
            for s in result["sql"]:
                click.echo(s)

        # Best-effort warnings (e.g. no-PK table with parallel writes on
        # both sides). Not a conflict — diff still runs — but the user
        # should know the result may be ambiguous.
        for w in result.get("warnings") or []:
            click.secho(
                f"\nWARNING: {w['kind']} on {w['key']}",
                fg="yellow", bold=True, err=True,
            )
            click.echo(f"  {w['note']}", err=True)

        # In preview mode, conflicts are reported but we exit 0 — the user
        # wants to SEE the conflicts. Only --apply refuses.
        if apply and result.get("conflicts"):
            click.echo()
            click.echo(
                f"{len(result['conflicts'])} conflict(s). Not applying.",
                err=True,
            )
            raise click.ClickException(
                "Merge conflict. Resolve the listed conflicts on the branch "
                "and re-run `fld diff`."
            )

        # Refuse --apply on a baseless branch unless explicitly authorized.
        if apply and base is None and not allow_2way_apply:
            click.secho(
                "\nRefusing to apply a 2-way diff (branch has no merge base).",
                fg="red", bold=True, err=True,
            )
            click.echo(
                "  Without a base we cannot distinguish branch's changes from\n"
                "  parent's independent drift. Applying could DROP things the\n"
                "  parent added since this branch was created.\n"
                "  If you understand the risks, re-run with --allow-2way-apply.",
                err=True,
            )
            raise click.ClickException("Refused: missing merge base.")

        if apply and result["sql"]:
            click.echo()
            click.echo(f"Applying {len(result['sql'])} statements to '{parent_name}'...")
            import psycopg
            from .db import get_database_dsn
            dsn = psycopg.conninfo.conninfo_to_dict(get_database_dsn())
            dsn["dbname"] = parent_name
            target_dsn = psycopg.conninfo.make_conninfo(**dsn)
            with psycopg.connect(target_dsn) as conn:
                with conn.cursor() as cur:
                    for s in result["sql"]:
                        cur.execute(s)
                conn.commit()
            click.echo("Applied. Parent now contains branch's data changes.")

            # On successful apply, the base snapshot is no longer needed.
            dropped = drop_base_for_branch(branch_name)
            if dropped:
                click.echo(f"Dropped base snapshot '{dropped}'.")
            # Parent stats snapshot also no longer needed.
            parent_snap_path = get_branch_parent_snapshot_path(branch_oid)
            if parent_snap_path.exists():
                parent_snap_path.unlink()

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        raise click.ClickException(str(e))


@cli.command()
def version() -> None:
    """Display the foldout version."""
    click.echo(__version__)
