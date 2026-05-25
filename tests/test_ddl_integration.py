#!/usr/bin/env python3
"""
Integration tests for DDL logging functionality.

This module contains focused test cases that verify DDL command logging:
- CREATE TABLE and ALTER TABLE operations 
- DROP TABLE operations

Tests use shared database setup/teardown to avoid redundant branching operations.
Database names are unique and cleaned up on success, left for debugging on failure.
"""
import atexit
import os
import subprocess
import time
import uuid
from typing import Dict, List, Optional, Tuple

import psycopg

try:
    import pytest
    PYTEST_AVAILABLE = True
except ImportError:
    PYTEST_AVAILABLE = False

# Global test database state
_test_databases: Dict[str, str] = {}  # Maps test_id -> branch_db_name
_cleanup_dbs: List[str] = []  # Databases to clean up on success


def get_connection_dsn() -> str:
    """Get PostgreSQL connection string from environment."""
    dsn = os.environ.get("FLD_DATABASE")
    if not dsn:
        if PYTEST_AVAILABLE:
            pytest.skip("FLD_DATABASE environment variable not set")
        else:
            raise RuntimeError("FLD_DATABASE environment variable not set")
    return dsn


def run_foldout_command(cmd: List[str]) -> Tuple[int, str, str]:
    """Run a foldout command and return exit code, stdout, stderr."""
    # Use foldout executable from venv
    foldout_bin = ".venv/bin/foldout"
    if not os.path.exists(foldout_bin):
        if PYTEST_AVAILABLE:
            pytest.skip("foldout executable not found at .venv/bin/foldout")
        else:
            raise RuntimeError("foldout executable not found at .venv/bin/foldout")
    
    full_cmd = [foldout_bin] + cmd
    
    result = subprocess.run(
        full_cmd,
        capture_output=True,
        text=True,
        cwd=os.getcwd()
    )
    
    return result.returncode, result.stdout, result.stderr


def database_exists(dsn: str, dbname: str) -> bool:
    """Check if a database exists."""
    try:
        # Connect to postgres database to check if target database exists
        base_dsn = dsn.rsplit('/', 1)[0] + '/postgres'
        with psycopg.connect(base_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s",
                    (dbname,)
                )
                return cur.fetchone() is not None
    except Exception:
        return False


def drop_database_if_exists(dsn: str, dbname: str) -> None:
    """Drop a database if it exists."""
    if not database_exists(dsn, dbname):
        return
    
    try:
        # Connect to postgres database to drop target database
        base_dsn = dsn.rsplit('/', 1)[0] + '/postgres'
        with psycopg.connect(base_dsn) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                # Terminate connections to the target database
                cur.execute("""
                    SELECT pg_terminate_backend(pid)
                    FROM pg_stat_activity 
                    WHERE datname = %s AND pid <> pg_backend_pid()
                """, (dbname,))
                
                # Drop the database
                cur.execute(f"DROP DATABASE IF EXISTS {dbname}")
                
    except Exception as e:
        print(f"Warning: Failed to drop database {dbname}: {e}")


def create_database(dsn: str, dbname: str) -> None:
    """Create a new database."""
    # Connect to postgres database to create target database
    base_dsn = dsn.rsplit('/', 1)[0] + '/postgres'
    with psycopg.connect(base_dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE {dbname}")


def execute_sql(dsn: str, dbname: str, sql: str) -> List[Tuple]:
    """Execute SQL on a specific database and return results."""
    # Connect to the target database
    db_dsn = dsn.rsplit('/', 1)[0] + f'/{dbname}'
    with psycopg.connect(db_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                return cur.fetchall()
            return []


def setup_test_database() -> str:
    """
    Set up a test database with foldout DDL logging enabled.
    
    Creates a unique source database, branches it, and returns the branch database name.
    This setup is shared across multiple test cases.
    
    Returns:
        The branch database name that can be used for testing.
    """
    dsn = get_connection_dsn()
    
    # Generate unique database names using timestamp and UUID
    test_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    source_db = f"fld_test_source_{test_id}"
    branch_db = f"fld_test_branch_{test_id}"
    
    try:
        print(f"Setting up test databases: {source_db} -> {branch_db}")
        
        # Step 1: Create source database
        print(f"Creating source database: {source_db}")
        create_database(dsn, source_db)
        _cleanup_dbs.append(source_db)
        
        # Step 2: Create branch using foldout
        print(f"Creating branch: {source_db} -> {branch_db}")
        exit_code, stdout, stderr = run_foldout_command(["branch", source_db, branch_db])
        
        if exit_code != 0:
            print(f"foldout branch command failed:")
            print(f"Exit code: {exit_code}")
            print(f"Stdout: {stdout}")
            print(f"Stderr: {stderr}")
            raise Exception(f"foldout branch command failed with exit code {exit_code}")
        
        _cleanup_dbs.append(branch_db)
        print("Branch created successfully")
        
        # Step 3: Verify branch database exists and has foldout schema
        print(f"Verifying branch database {branch_db} has foldout schema")
        result = execute_sql(dsn, branch_db, """
            SELECT 1 FROM pg_namespace WHERE nspname = 'foldout'
        """)
        assert len(result) > 0, "foldout schema not found in branch database"
        
        result = execute_sql(dsn, branch_db, """
            SELECT 1 FROM pg_class c 
            JOIN pg_namespace n ON n.oid = c.relnamespace 
            WHERE n.nspname = 'foldout' AND c.relname = 'ddl_log'
        """)
        assert len(result) > 0, "foldout.ddl_log table not found in branch database"
        
        # Clear any existing DDL log entries to start fresh
        execute_sql(dsn, branch_db, "DELETE FROM foldout.ddl_log")
        
        # Store the branch database for reuse
        _test_databases[test_id] = branch_db
        
        print(f"Test database setup complete: {branch_db}")
        return branch_db
        
    except Exception as e:
        print(f"❌ Test database setup failed: {e}")
        print(f"Leaving test databases for debugging: {_cleanup_dbs}")
        print(f"To connect to source database: psql {dsn.rsplit('/', 1)[0]}/{source_db}")
        if branch_db in _cleanup_dbs:
            print(f"To connect to branch database: psql {dsn.rsplit('/', 1)[0]}/{branch_db}")
        raise


def cleanup_test_databases() -> None:
    """Clean up all test databases on successful completion."""
    dsn = get_connection_dsn()
    
    if _cleanup_dbs:
        print("Cleaning up test databases...")
        for db in _cleanup_dbs:
            drop_database_if_exists(dsn, db)
            print(f"Dropped database: {db}")
        _cleanup_dbs.clear()
        _test_databases.clear()


def test_create_alter_table_logging():
    """
    Test that CREATE TABLE and ALTER TABLE commands are properly logged.
    
    This test verifies:
    - CREATE TABLE operations are logged with correct metadata
    - ALTER TABLE operations are logged with correct metadata  
    - SQL text is captured for both operations
    """
    branch_db = setup_test_database()
    dsn = get_connection_dsn()
    
    try:
        print("Testing CREATE TABLE and ALTER TABLE logging...")
        
        # Perform CREATE TABLE
        execute_sql(dsn, branch_db, """
            CREATE TABLE test_create_alter (
                id SERIAL PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        
        # Perform ALTER TABLE
        execute_sql(dsn, branch_db, """
            ALTER TABLE test_create_alter 
            ADD COLUMN email VARCHAR(255),
            ADD COLUMN status VARCHAR(50) DEFAULT 'active'
        """)
        
        # Verify CREATE and ALTER operations were logged
        ddl_entries = execute_sql(dsn, branch_db, """
            SELECT 
                id, 
                command_tag, 
                object_type, 
                schema_name, 
                object_identity, 
                phase,
                CASE WHEN sql_text IS NOT NULL THEN 'HAS_SQL' ELSE 'NO_SQL' END as has_sql
            FROM foldout.ddl_log 
            WHERE object_type = 'table' 
              AND schema_name = 'public'
              AND object_identity = 'public.test_create_alter'
            ORDER BY id
        """)
        
        print(f"Found {len(ddl_entries)} DDL log entries for test_create_alter:")
        for entry in ddl_entries:
            print(f"  {entry}")
        
        # Verify we have the expected operations
        command_tags = [entry[1] for entry in ddl_entries]
        
        create_commands = [tag for tag in command_tags if tag == 'CREATE TABLE']
        assert len(create_commands) >= 1, f"Expected at least 1 CREATE TABLE command, got {len(create_commands)}"
        
        alter_commands = [tag for tag in command_tags if tag == 'ALTER TABLE']
        assert len(alter_commands) >= 1, f"Expected at least 1 ALTER TABLE command, got {len(alter_commands)}"
        
        # Verify all entries have SQL text
        sql_statuses = [entry[6] for entry in ddl_entries]
        has_sql_count = sum(1 for status in sql_statuses if status == 'HAS_SQL')
        assert has_sql_count == len(ddl_entries), f"Expected all {len(ddl_entries)} entries to have SQL text, got {has_sql_count}"
        
        print("✅ CREATE TABLE and ALTER TABLE logging test passed!")
        
    except Exception as e:
        print(f"❌ CREATE/ALTER TABLE logging test failed: {e}")
        print(f"Leaving test databases for debugging: {_cleanup_dbs}")
        print(f"To check DDL log: psql {dsn.rsplit('/', 1)[0]}/{branch_db} -c \"SELECT * FROM foldout.ddl_log ORDER BY id;\"")
        raise


def test_drop_table_logging():
    """
    Test that DROP TABLE commands are properly logged.
    
    This test verifies:
    - DROP TABLE operations are logged with correct metadata
    - SQL text is captured for DROP operations
    - This was the main bug that was fixed
    """
    # Reuse the existing test database if available, otherwise set up new one
    if not _test_databases:
        branch_db = setup_test_database()
    else:
        branch_db = list(_test_databases.values())[0]
    
    dsn = get_connection_dsn()
    
    try:
        print("Testing DROP TABLE logging...")
        
        # Create a table specifically for dropping
        execute_sql(dsn, branch_db, """
            CREATE TABLE test_drop_table (
                id INT PRIMARY KEY,
                data TEXT NOT NULL
            )
        """)
        
        # Drop the table
        execute_sql(dsn, branch_db, """
            DROP TABLE test_drop_table
        """)
        
        # Verify DROP operation was logged
        ddl_entries = execute_sql(dsn, branch_db, """
            SELECT 
                id, 
                command_tag, 
                object_type, 
                schema_name, 
                object_identity, 
                phase,
                CASE WHEN sql_text IS NOT NULL THEN 'HAS_SQL' ELSE 'NO_SQL' END as has_sql
            FROM foldout.ddl_log 
            WHERE object_type = 'table' 
              AND schema_name = 'public'
              AND object_identity = 'public.test_drop_table'
            ORDER BY id
        """)
        
        print(f"Found {len(ddl_entries)} DDL log entries for test_drop_table:")
        for entry in ddl_entries:
            print(f"  {entry}")
        
        # Verify we have both CREATE and DROP operations
        command_tags = [entry[1] for entry in ddl_entries]
        
        create_commands = [tag for tag in command_tags if tag == 'CREATE TABLE']
        assert len(create_commands) >= 1, f"Expected at least 1 CREATE TABLE command, got {len(create_commands)}"
        
        drop_commands = [tag for tag in command_tags if tag == 'DROP TABLE']
        assert len(drop_commands) >= 1, f"Expected at least 1 DROP TABLE command, got {len(drop_commands)}"
        
        # Verify all entries have SQL text
        sql_statuses = [entry[6] for entry in ddl_entries]
        has_sql_count = sum(1 for status in sql_statuses if status == 'HAS_SQL')
        assert has_sql_count == len(ddl_entries), f"Expected all {len(ddl_entries)} entries to have SQL text, got {has_sql_count}"
        
        print("✅ DROP TABLE logging test passed!")
        
    except Exception as e:
        print(f"❌ DROP TABLE logging test failed: {e}")
        print(f"Leaving test databases for debugging: {_cleanup_dbs}")
        print(f"To check DDL log: psql {dsn.rsplit('/', 1)[0]}/{branch_db} -c \"SELECT * FROM foldout.ddl_log ORDER BY id;\"")
        raise


# Pytest-compatible test functions
def test_ddl_create_alter_logging():
    """Pytest wrapper for CREATE/ALTER TABLE logging test."""
    test_create_alter_table_logging()
    cleanup_test_databases()


def test_ddl_drop_logging():
    """Pytest wrapper for DROP TABLE logging test.""" 
    test_drop_table_logging()
    cleanup_test_databases()


def run_all_tests():
    """Run all DDL logging integration tests."""
    try:
        print("🚀 Starting DDL logging integration tests...")
        
        # Test 1: CREATE and ALTER TABLE logging
        test_create_alter_table_logging()
        
        # Test 2: DROP TABLE logging (reuses the same database)
        test_drop_table_logging()
        
        print("🎉 All DDL logging integration tests passed!")
        
        # Clean up on success
        cleanup_test_databases()
        
    except Exception as e:
        print(f"💥 DDL logging integration tests failed: {e}")
        raise


if __name__ == "__main__":
    # Allow running as a standalone script
    run_all_tests()