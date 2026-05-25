from __future__ import annotations

from typing import Any, Optional

import pytest
from click.testing import CliRunner

import foldout.cli as cli_mod
from foldout.change_capture import ChangeCaptureInstaller


class _FakeCursor:
    def __init__(self) -> None:
        self._last_sql: Optional[str] = None
        self.executed_sql: list[str] = []
        self._install_mode: bool = False
        self._exists_calls: int = 0

    def execute(self, sql: str, params: Any = None) -> None:
        self._last_sql = sql
        self.executed_sql.append(sql)
        if sql.strip().lower().startswith("create schema"):
            self._install_mode = True
        if sql.strip().lower().startswith("drop event trigger"):
            self._install_mode = True

    def fetchone(self) -> Any:
        # Simulate is_installed checks toggling from False to True
        if self._last_sql and "pg_namespace" in self._last_sql:
            self._exists_calls += 1
            if self._exists_calls <= 1:
                return (False,)
            return (True,)
        if self._last_sql and "pg_class" in self._last_sql:
            if self._exists_calls <= 1:
                return (False,)
            return (True,)
        if self._last_sql and "pg_proc" in self._last_sql:
            if self._exists_calls <= 1:
                return (False,)
            return (True,)
        return (True,)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeConn:
    def __init__(self) -> None:
        self.cursor_obj = _FakeCursor()
        self.commits: int = 0

    def cursor(self) -> _FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1

    def __enter__(self) -> "_FakeConn":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def test_installer_installs_when_missing(monkeypatch) -> None:
    calls: list[str] = []

    def fake_connect(dsn: str) -> _FakeConn:  # type: ignore[override]
        return _FakeConn()

    import foldout.change_capture as cc

    monkeypatch.setattr(cc.psycopg, "connect", fake_connect)
    monkeypatch.setattr(cc, "get_database_dsn", lambda: "postgresql://ignored")

    installer = ChangeCaptureInstaller()
    changed = installer.ensure_installed("sourcedb")
    assert changed is True


@pytest.mark.skip(
    reason=(
        "vka branch no longer wires change-capture / event triggers — "
        "page-LSN diff superseded that path. Kept around because we may "
        "revisit query/trigger capture for some use cases later."
    )
)
def test_branch_wires_change_capture(monkeypatch) -> None:
    # Prepare a fake installer capturing the databases
    recorded: list[str] = []

    class FakeInstaller:
        def __init__(self) -> None:
            return None

        def ensure_installed(self, dbname: str) -> bool:  # type: ignore[override]
            recorded.append(dbname)
            return True

    # Patch CLI dependencies to avoid real DB operations
    monkeypatch.setattr(cli_mod, "ChangeCaptureInstaller", FakeInstaller)
    monkeypatch.setattr(cli_mod, "get_database_oid", lambda name: 111)
    monkeypatch.setattr(cli_mod, "register_source_database", lambda a, b: None)
    monkeypatch.setattr(cli_mod, "get_data_directory", lambda: "/tmp")
    monkeypatch.setattr(cli_mod, "create_branch_database", lambda a, b: (b, 222))

    class _DummyLock:
        def __enter__(self) -> None:
            return None

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr(cli_mod, "database_write_lock", lambda name: _DummyLock())
    monkeypatch.setattr(cli_mod, "copy_database_files", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli_mod, "register_branch_database", lambda a, b, c: None)
    monkeypatch.setattr(cli_mod, "log_branch_operation", lambda a, b, c: 1)

    runner = CliRunner()
    result = runner.invoke(cli_mod.cli, ["branch", "sourcedb", "feature_x"])
    assert result.exit_code == 0
    # Source and new branch DB should be ensured
    assert recorded == ["sourcedb", "feature_x"]
