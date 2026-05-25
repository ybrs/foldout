from click.testing import CliRunner

from foldout import __version__
from foldout.cli import cli


def test_version_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_snapshot_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["snapshot", "maindb"])
    assert result.exit_code == 0
    assert "Snapshotting database maindb" in result.output
