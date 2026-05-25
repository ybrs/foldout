# Repository Guidelines

## Project Structure & Module Organization
- `src/foldout/`: core package. CLI entry in `cli.py`; database helpers in `db.py`; SQL migrations in `migration/`.
- `tests/`: pytest suite for CLI and internals.
- `pyproject.toml`: project metadata (Hatchling), script entry point `foldout= foldout.cli:cli`.
- `README.md`: setup, configuration, and usage examples.

## Build, Test, and Development Commands
- `uv venv`: create a Python 3.11+ virtual env.
- `uv pip install -e .`: install in editable mode.
- `uv run foldout --help`: verify CLI wiring.
- `uv run foldout databases list`: example command; requires `FLD_DATABASE`.
- `uv run pytest -q`: run tests; adds `src/` to `PYTHONPATH` via pytest config.

## Coding Style & Naming Conventions
- Python: follow PEP 8; include type hints and docstrings for public APIs.
- Names: modules and functions use snake_case; classes use PascalCase; constants UPPER_SNAKE_CASE.
- Paths: prefer `pathlib.Path` for filesystem operations.
- CLI: use Click patterns (groups/commands) consistent with `cli.py`.
- Never use list compherensions or lambda in python code. 
- Write code object oriented as possible. 

## Testing Guidelines
- Framework: pytest (see `tool.pytest.ini_options` in `pyproject.toml`).
- Location/naming: place tests in `tests/` as `test_*.py` with `test_*` functions.
- CLI tests: use `click.testing.CliRunner()`; keep output stable and human-readable.
- Run `uv run pytest` before pushing; add tests with new features and for bug fixes.

## Commit & Pull Request Guidelines
- Commits: concise, imperative mood. Conventional Commits are welcome (e.g., `feat: add snapshot delete`, `docs: update README`).
- PRs: include a clear description, linked issues, reproduction/verification steps, and relevant CLI output (paste snippets). Ensure tests pass locally.

## Security & Configuration Tips
- Configure `FLD_DATABASE` with a non-production DSN during development. Do not commit secrets.
- Snapshot/restore operations can be destructive; test against disposable databases.
