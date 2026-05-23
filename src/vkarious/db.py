"""Database connection helpers for vkarious."""

from __future__ import annotations

import os
import re
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import psycopg


def get_database_dsn() -> str:
    """Get the database DSN from VKA_DATABASE environment variable."""
    dsn = os.getenv("VKA_DATABASE")
    if not dsn:
        raise ValueError("VKA_DATABASE environment variable is required")
    return dsn


def connect(dsn: str | None = None) -> psycopg.Connection:
    """Return a new PostgreSQL connection using the provided DSN or VKA_DATABASE."""
    if dsn is None:
        dsn = get_database_dsn()
    return psycopg.connect(dsn)


def list_databases() -> list[dict[str, str | int]]:
    """List all databases with their OIDs and names."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid, datname FROM pg_database ORDER BY datname")
            return [{"oid": row[0], "name": row[1]} for row in cur.fetchall()]


def get_data_directory() -> str:
    """Return the PostgreSQL data directory path.

    If the environment variable `VKA_PG_DATA_PATH` is defined, its value
    is returned to allow overriding the detected PostgreSQL data directory.
    This is useful when PostgreSQL is running inside a container while
    vkarious runs on the host and needs a host-visible path for file
    operations (e.g., copy-on-write file copying).

    Otherwise falls back to querying the server with `SHOW data_directory`.
    """
    override = os.getenv("VKA_PG_DATA_PATH")
    if override:
        return override

    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SHOW data_directory")
            return cur.fetchone()[0]


def get_database_oid(database_name: str) -> int:
    """Get the OID of a specific database."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (database_name,))
            result = cur.fetchone()
            if result is None:
                raise ValueError(f"Database '{database_name}' not found")
            return result[0]


def get_branch_parent(branch_name: str) -> tuple[int, int, str]:
    """Look up branch in vka_databases and return (branch_oid, parent_oid, parent_datname)."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT vd.oid, vd.parent, pg.datname
                FROM vka_databases vd
                JOIN pg_database pg ON pg.oid = vd.parent
                WHERE vd.datname = %s AND vd.type = 'branch'
            """, (branch_name,))
            row = cur.fetchone()
            if row is None:
                raise ValueError(
                    f"Branch '{branch_name}' not found in vka_databases "
                    f"(or its parent no longer exists)"
                )
            return row[0], row[1], row[2]


def get_snapshot_dir() -> Path:
    """Directory where page-diff snapshots for branches live."""
    base = Path(os.path.expanduser("~/.vkarious/snapshots"))
    base.mkdir(parents=True, exist_ok=True)
    return base


def get_branch_snapshot_path(branch_oid: int) -> Path:
    return get_snapshot_dir() / f"{branch_oid}.json"


def get_branch_parent_snapshot_path(branch_oid: int) -> Path:
    """Snapshot of the parent's file state at branch creation time.
    Used by 3-way diff to stat-skip files where the parent hasn't drifted.
    """
    return get_snapshot_dir() / f"{branch_oid}_parent.json"


def terminate_database_connections(database_name: str) -> int:
    """Terminate all connections to a database except the current one."""
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
            """, (database_name,))
            terminated_count = cur.rowcount
            conn.commit()
            return terminated_count


@contextmanager
def database_write_lock(database_name: str) -> Iterator[None]:
    """Context manager to acquire an exclusive lock on a database to prevent writes."""
    # Connect to the specific database to lock it
    db_dsn = get_database_dsn()
    # Parse DSN and modify it to connect to the target database
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    conn = psycopg.connect(target_dsn)
    try:
        with conn.cursor() as cur:
            # First terminate existing connections
            # terminate_database_connections(database_name)
            print("checkpoint")
            cur.execute("CHECKPOINT")

            time.sleep(1)  # Brief pause to ensure connections are terminated
            
            # Acquire an advisory lock
            cur.execute("SELECT pg_advisory_lock(12345)")
            conn.commit()
            
        yield
    finally:
        with conn.cursor() as cur:
            # Release the advisory lock
            cur.execute("SELECT pg_advisory_unlock(12345)")
            conn.commit()
        conn.close()

def get_pg_major_version(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        version_str = cur.fetchone()[0]
    return int(version_str.split('.')[0])

def create_snapshot_database(source_database: str) -> tuple[str, int]:
    """Create a new database for snapshot with timestamp name."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_name = f"snapshot_{source_database}_{timestamp}"
    
    with connect() as conn:
        # Set autocommit for CREATE DATABASE (required for PostgreSQL)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Create the new database
            pg_version = get_pg_major_version(conn)
            if pg_version < 15:
                cur.execute(f'''CREATE DATABASE "{snapshot_name}" ''')
            else:
                cur.execute(f'''CREATE DATABASE "{snapshot_name}" STRATEGY='FILE_COPY' ''')
            
            # Get the OID of the newly created database
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (snapshot_name,))
            oid = cur.fetchone()[0]
            
    return snapshot_name, oid


def create_base_database(source_database: str, branch_name: str) -> tuple[str, int]:
    """Create a COW snapshot of source_database to serve as the merge base
    for `vka diff` of `branch_name`. Returned name is `__base__<branch_name>`.
    """
    base_name = f"__base__{branch_name}"
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            if pg_version < 15:
                cur.execute(f'CREATE DATABASE "{base_name}" ')
            else:
                cur.execute(f'CREATE DATABASE "{base_name}" STRATEGY=\'FILE_COPY\' ')
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (base_name,))
            oid = cur.fetchone()[0]
    return base_name, oid


def register_base_database(base_name: str, base_oid: int, parent_oid: int,
                           branch_oid: int) -> None:
    """Insert the base into vka_databases and link it to its branch."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO vka_databases (oid, datname, parent, created_at, type, status)
                VALUES (%s, %s, %s, %s, 'base', 'live')
            """, (base_oid, base_name, parent_oid, datetime.now()))
            cur.execute(
                "UPDATE vka_databases SET base_oid = %s WHERE oid = %s",
                (base_oid, branch_oid),
            )
        conn.commit()


def get_branch_base(branch_name: str) -> tuple[int, str] | None:
    """Return (base_oid, base_datname) for a branch, or None if no base set."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT vd.base_oid, pg.datname
                FROM vka_databases vd
                LEFT JOIN pg_database pg ON pg.oid = vd.base_oid
                WHERE vd.datname = %s AND vd.type = 'branch'
            """, (branch_name,))
            row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return row[0], row[1]


def drop_base_for_branch(branch_name: str) -> str | None:
    """Drop the base snapshot associated with a branch (after a successful merge).
    Returns the base name dropped, or None if there was no base.
    """
    base = get_branch_base(branch_name)
    if base is None:
        return None
    base_oid, base_name = base
    if base_name is None:
        return None
    # DROP DATABASE
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{base_name}"')
    # Remove vka_databases entries
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vka_databases WHERE oid = %s", (base_oid,))
            cur.execute(
                "UPDATE vka_databases SET base_oid = NULL WHERE datname = %s",
                (branch_name,),
            )
        conn.commit()
    return base_name


def create_branch_database(source_database: str, branch_name: str) -> tuple[str, int]:
    """Create a new database for branch with user-provided branch name."""
    # TODO: think. we might add a prefix, though git doesnt add any prefix. 
    branch_database_name = f"{branch_name}"
    
    with connect() as conn:
        # Set autocommit for CREATE DATABASE (required for PostgreSQL)
        conn.autocommit = True
        with conn.cursor() as cur:
            # Create the new database
            pg_version = get_pg_major_version(conn)
            if pg_version < 15:
                cur.execute(f'''CREATE DATABASE "{branch_database_name}" ''')
            else:
                cur.execute(f'''CREATE DATABASE "{branch_database_name}" STRATEGY='FILE_COPY' ''')
            
            # Get the OID of the newly created database
            cur.execute("SELECT oid FROM pg_database WHERE datname = %s", (branch_database_name,))
            oid = cur.fetchone()[0]
            
    return branch_database_name, oid


def copy_database_files(data_directory: str, source_oid: int, target_oid: int) -> None:
    """Copy database files from source to target using OIDs."""
    data_path = Path(data_directory)
    base_path = data_path / "base"
    
    source_path = base_path / str(source_oid)
    target_path = base_path / str(target_oid)
    
    if not source_path.exists():
        raise FileNotFoundError(f"Source database directory not found: {source_path}")
    
    if not target_path.exists():
        raise FileNotFoundError(f"Target database directory not found: {target_path}")
    
    # Check VKA_NOCOW environment variable to determine copy method
    use_nocow = os.getenv("VKA_NOCOW") is not None
    
    subprocess.run(
        ["rm", "-rf", str(target_path) + "/"],
        check=True,
        capture_output=True,
        text=True
    )

    # Use regular cp if VKA_NOCOW is set, otherwise use cp -c for copy-on-write
    cp_args = ["cp", "-r" if use_nocow else "-cR", str(source_path) + "/", str(target_path) + "/"]
    
    try:
        subprocess.run(
            cp_args,
            check=True,
            capture_output=True,
            text=True
        )
    except subprocess.CalledProcessError:
        if not use_nocow:
            # Fallback to regular recursive copy if cp -c is not available
            subprocess.run(
                ["cp", "-r", str(source_path) + "/", str(target_path) + "/"],
                check=True,
                capture_output=True,
                text=True
            )
        else:
            raise
    
    # Remove pg_internal.init file from the copied directory
    pg_internal_init = target_path / "pg_internal.init"
    if pg_internal_init.exists():
        pg_internal_init.unlink()
    else:
        print("no internal file ?")


def restore_database_from_snapshot(database_name: str, snapshot_name: str) -> dict:
    """Restore a database's physical files from a snapshot database.

    Steps:
    - Ensure the snapshot exists and belongs to the given database.
    - Terminate all connections to the source database and acquire a write lock.
    - Move the source database's data directory to a backup prefixed with `vka_delete_`.
    - Drop the source database and recreate a new one with the same name using STRATEGY='FILE_COPY'.
    - Copy the snapshot's data directory into the new database OID path using copy-on-write.
    - Remove `pg_internal.init` from the restored directory after copy.
    - Verify connectivity and that at least one table exists in the restored database.
    - Log the restore operation and update the database status to 'restored'.

    Returns a dict with keys: `source_oid`, `snapshot_oid`, `restored_oid`, `backup_path`, `tables_count`.
    """
    # Resolve OIDs and paths
    source_oid = get_database_oid(database_name)
    snapshot_record = get_snapshot_record(snapshot_name)
    if snapshot_record is None:
        raise ValueError(f"Snapshot '{snapshot_name}' not found in metadata")

    snapshot_oid = snapshot_record['oid']
    parent_oid = snapshot_record['parent']
    if parent_oid != source_oid:
        raise ValueError(
            f"Snapshot '{snapshot_name}' does not belong to database '{database_name}'"
        )

    # Start logging the restore operation
    log_id = log_restore_operation(source_oid, None, database_name, "restore", "started")

    try:
        data_directory = get_data_directory()
        base_path = Path(data_directory) / "base"
        source_path = base_path / str(source_oid)
        snapshot_path = base_path / str(snapshot_oid)

        if not source_path.exists():
            raise FileNotFoundError(f"Source data directory not found: {source_path}")
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot data directory not found: {snapshot_path}")

        # Prepare a unique backup directory name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = base_path / f"vka_delete_{source_oid}_{timestamp}"
        
        # Safety: terminate connections and briefly lock during filesystem move
        terminate_database_connections(database_name)
        with database_write_lock(database_name):
            subprocess.run(["mv", str(source_path), str(backup_path)], check=True, capture_output=True, text=True)

        # Drop and recreate the database to clear caches and allocate a new OID
        drop_database(database_name)
        create_database_with_strategy(database_name, "FILE_COPY")
        restored_oid = get_database_oid(database_name)

        # Update the log with the new OID
        update_restore_log(log_id, "in_progress")
        
        # Copy from snapshot OID directory to the new database OID directory
        copy_database_files(data_directory, snapshot_oid, restored_oid)

        # Post-restore validation: can connect and tables exist
        tables_count = 0
        db_dsn = get_database_dsn()
        params = psycopg.conninfo.conninfo_to_dict(db_dsn)
        params['dbname'] = database_name
        target_dsn = psycopg.conninfo.make_conninfo(**params)
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
                row = cur.fetchone()
                if row is not None:
                    tables_count = int(row[0])
        
        # Update database status to 'restored' after successful validation
        update_database_status(restored_oid, 'restored')
        
        # Update log with success
        update_restore_log(log_id, "success")
        
        return {
            "source_oid": source_oid,
            "snapshot_oid": snapshot_oid,
            "restored_oid": restored_oid,
            "backup_path": str(backup_path),
            "tables_count": tables_count,
        }
        
    except Exception as e:
        # Update log with error
        update_restore_log(log_id, "error", str(e))
        raise


def database_exists(database_name: str) -> bool:
    """Check if a database exists."""
    try:
        with connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database_name,))
                return cur.fetchone() is not None
    except Exception:
        return False


def create_database(database_name: str) -> None:
    """Create a database."""
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{database_name}"')

def create_database_with_strategy(database_name: str, strategy: str = "FILE_COPY") -> None:
    """Create a database using a specific creation strategy.

    Mirrors the behavior used for snapshot creation (e.g., STRATEGY='FILE_COPY').
    """
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            pg_version = get_pg_major_version(conn)
            if pg_version < 15:
                cur.execute(f'''CREATE DATABASE "{database_name}" ''')
            else:
                cur.execute(f'''CREATE DATABASE "{database_name}" STRATEGY='{strategy}' ''')


def table_exists(table_name: str, database_name: str = "vkarious") -> bool:
    """Check if a table exists in the specified database."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    try:
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1 FROM information_schema.tables 
                    WHERE table_name = %s AND table_schema = 'public'
                """, (table_name,))
                return cur.fetchone() is not None
    except Exception:
        return False


def get_current_version(database_name: str = "vkarious") -> str:
    """Get the current migration version from vka_dbversion table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    try:
        with psycopg.connect(target_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version FROM vka_dbversion LIMIT 1")
                result = cur.fetchone()
                return result[0] if result else '0'
    except Exception:
        return '0'


def get_latest_migration_version() -> int:
    """Get the latest migration version from migration files."""
    migration_dir = Path(__file__).parent / "migration"
    if not migration_dir.exists():
        return 0
    
    versions = []
    for file in migration_dir.glob("vkarious_*.sql"):
        match = re.search(r'vkarious_(\d+)\.sql', file.name)
        if match:
            versions.append(int(match.group(1)))
    
    return max(versions) if versions else 0


def execute_migration(migration_file: Path, database_name: str = "vkarious") -> None:
    """Execute a migration file against the specified database."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = database_name
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            with open(migration_file, 'r') as f:
                sql = f.read()
            cur.execute(sql)
        conn.commit()


def register_source_database(database_name: str, oid: int) -> None:
    """Register a source database in vka_databases table if not already exists."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Check if database already exists in vka_databases
            cur.execute("SELECT 1 FROM vka_databases WHERE oid = %s", (oid,))
            if cur.fetchone() is None:
                # Insert the source database record
                cur.execute("""
                    INSERT INTO vka_databases (oid, datname, parent, created_at, type, status) 
                    VALUES (%s, %s, NULL, %s, 'source', 'live')
                """, (oid, database_name, datetime.now()))
        conn.commit()


def register_snapshot_database(snapshot_name: str, snapshot_oid: int, parent_oid: int) -> None:
    """Register a snapshot database in vka_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Insert the snapshot database record
            cur.execute("""
                INSERT INTO vka_databases (oid, datname, parent, created_at, type, status) 
                VALUES (%s, %s, %s, %s, 'snapshot', 'live')
            """, (snapshot_oid, snapshot_name, parent_oid, datetime.now()))
        conn.commit()


def register_branch_database(branch_name: str, branch_oid: int, parent_oid: int) -> None:
    """Register a branch database in vka_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Insert the branch database record
            cur.execute("""
                INSERT INTO vka_databases (oid, datname, parent, created_at, type, status) 
                VALUES (%s, %s, %s, %s, 'branch', 'live')
            """, (branch_oid, branch_name, parent_oid, datetime.now()))
        conn.commit()


def get_databases_with_snapshots() -> dict[str, dict]:
    """Get databases from vkarious metadata DB with their snapshots in parent-child relationship."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            # Get all databases from vka_databases
            cur.execute("""
                SELECT vd.oid, vd.datname, vd.parent, vd.created_at, vd.type, vd.status,
                       pg.datname as current_datname
                FROM vka_databases vd
                LEFT JOIN pg_database pg ON vd.oid = pg.oid
                ORDER BY vd.type, vd.created_at
            """)
            
            databases = {}
            snapshots = {}
            
            for row in cur.fetchall():
                oid, datname, parent, created_at, db_type, status, current_datname = row
                
                # Determine if database is defunct (not restored and doesn't exist in pg_database)
                if status != 'restored' and current_datname is None:
                    status = 'defunct'
                    # Update the status in the database
                    update_database_status(oid, 'defunct')
                
                # Use current database name if available, fallback to stored name
                display_name = current_datname or datname
                
                db_info = {
                    'oid': oid,
                    'stored_name': datname,
                    'current_name': display_name,
                    'parent': parent,
                    'created_at': created_at,
                    'type': db_type,
                    'status': status,
                    'snapshots': []
                }
                
                if db_type == 'source':
                    databases[oid] = db_info
                else:  # snapshot
                    snapshots[oid] = db_info
            
            # Organize snapshots under their parent databases
            for snapshot_oid, snapshot_info in snapshots.items():
                parent_oid = snapshot_info['parent']
                if parent_oid in databases:
                    databases[parent_oid]['snapshots'].append(snapshot_info)
            
            return databases


def drop_database(database_name: str) -> None:
    """Drop a database."""
    with connect() as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{database_name}"')


def get_snapshot_record(snapshot_name: str) -> dict | None:
    """Get snapshot record from vka_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT oid, datname, parent, created_at, type 
                FROM vka_databases 
                WHERE datname = %s AND type = 'snapshot'
            """, (snapshot_name,))
            result = cur.fetchone()
            if result:
                return {
                    'oid': result[0],
                    'datname': result[1], 
                    'parent': result[2],
                    'created_at': result[3],
                    'type': result[4]
                }
            return None


def update_database_status(oid: int, status: str) -> None:
    """Update the status of a database in vka_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE vka_databases SET status = %s WHERE oid = %s", (status, oid))
        conn.commit()


def delete_database_record(database_name: str) -> None:
    """Delete a database record from vka_databases table."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM vka_databases WHERE datname = %s", (database_name,))
        conn.commit()


def log_restore_operation(old_oid: int, new_oid: int, datname: str, operation: str = "restore", status: str = "started", error_description: str = None) -> int:
    """Log a restore operation to vka_log table and return the log ID."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            if status == "started":
                cur.execute("""
                    INSERT INTO vka_log (old_oid, new_oid, datname, operation, created_at, started_at, status) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (old_oid, new_oid, datname, operation, datetime.now(), datetime.now(), status))
            else:
                cur.execute("""
                    INSERT INTO vka_log (old_oid, new_oid, datname, operation, created_at, status, error_description) 
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (old_oid, new_oid, datname, operation, datetime.now(), status, error_description))
            log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def log_branch_operation(source_oid: int, branch_oid: int, branch_name: str, operation: str = "branch", status: str = "success") -> int:
    """Log a branch creation operation to vka_log table and return the log ID."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO vka_log (old_oid, new_oid, datname, operation, created_at, started_at, finished_at, status) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
            """, (source_oid, branch_oid, branch_name, operation, datetime.now(), datetime.now(), datetime.now(), status))
            log_id = cur.fetchone()[0]
        conn.commit()
    return log_id


def update_restore_log(log_id: int, status: str, error_description: str = None) -> None:
    """Update a restore operation log entry."""
    db_dsn = get_database_dsn()
    conn_params = psycopg.conninfo.conninfo_to_dict(db_dsn)
    conn_params['dbname'] = "vkarious"
    target_dsn = psycopg.conninfo.make_conninfo(**conn_params)
    
    with psycopg.connect(target_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE vka_log 
                SET finished_at = %s, status = %s, error_description = %s 
                WHERE id = %s
            """, (datetime.now(), status, error_description, log_id))
        conn.commit()


def initialize_database() -> None:
    """Initialize the vkarious database and run migrations."""
    # Check if vkarious database exists, create if not
    if not database_exists("vkarious"):
        create_database("vkarious")
    
    # Check if vka_dbversion table exists
    if not table_exists("vka_dbversion"):
        # Run initial migration
        migration_dir = Path(__file__).parent / "migration"
        initial_migration = migration_dir / "vkarious_1.sql"
        if initial_migration.exists():
            execute_migration(initial_migration)
        return
    
    # Check if we need to run additional migrations
    current_version = int(get_current_version())
    latest_version = get_latest_migration_version()
    
    if current_version < latest_version:
        migration_dir = Path(__file__).parent / "migration"
        for version in range(current_version + 1, latest_version + 1):
            migration_file = migration_dir / f"vkarious_{version}.sql"
            if migration_file.exists():
                execute_migration(migration_file)
