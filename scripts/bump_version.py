#!/usr/bin/env python3
"""Bump the version in pyproject.toml and src/foldout/__init__.py.

Usage:
    bump_version.py patch|minor|major
    bump_version.py <bump> <explicit_version>   # explicit wins if non-empty
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "src" / "foldout" / "__init__.py"

VERSION_RE = re.compile(r'^version = "(\d+)\.(\d+)\.(\d+)"', re.MULTILINE)
INIT_RE = re.compile(r'^__version__ = "(\d+)\.(\d+)\.(\d+)"', re.MULTILINE)


def read_current() -> tuple[int, int, int]:
    text = PYPROJECT.read_text()
    m = VERSION_RE.search(text)
    if not m:
        raise SystemExit("Could not find version in pyproject.toml")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def compute_next(current: tuple[int, int, int], bump: str, explicit: str | None) -> str:
    if explicit:
        if not re.fullmatch(r"\d+\.\d+\.\d+", explicit):
            raise SystemExit(f"VERSION must be X.Y.Z, got: {explicit}")
        return explicit
    major, minor, patch = current
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "major":
        return f"{major + 1}.0.0"
    raise SystemExit(f"Unknown bump type: {bump}. Use patch|minor|major.")


def write_pyproject(new_version: str) -> None:
    text = PYPROJECT.read_text()
    text = VERSION_RE.sub(f'version = "{new_version}"', text, count=1)
    PYPROJECT.write_text(text)


def write_init(new_version: str) -> None:
    if not INIT.exists():
        return
    text = INIT.read_text()
    if INIT_RE.search(text):
        text = INIT_RE.sub(f'__version__ = "{new_version}"', text, count=1)
        INIT.write_text(text)


def main() -> None:
    bump = sys.argv[1] if len(sys.argv) > 1 else "patch"
    explicit = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else None
    current = read_current()
    new_version = compute_next(current, bump, explicit)
    if new_version == ".".join(str(p) for p in current):
        raise SystemExit(f"Version unchanged: {new_version}")
    write_pyproject(new_version)
    write_init(new_version)
    print(new_version)


if __name__ == "__main__":
    main()
