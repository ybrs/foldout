"""Download and manage self-contained PostgreSQL distributions for tests.

We use the theseus-rs PostgreSQL binary releases — fully relocatable bundles
that don't require root or distro packages, and ship `initdb`/`pg_ctl`/`psql`
inside `bin/`. Each version is downloaded once into the on-disk cache and
reused across test runs.
"""

from __future__ import annotations

import os
import platform
import subprocess
import tarfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = REPO_ROOT / ".test-cache" / "pg-binaries"

# Scaffolded for all six versions in test-download-urls.txt. The active
# matrix lives in conftest.py — extend it there to enable more versions.
PG_VERSIONS: dict[int, str] = {
    13: "13.23.0",
    14: "14.22.0",
    15: "15.17.0",
    16: "16.13.0",
    17: "17.9.0",
    18: "18.3.0",
}


def _platform_triple() -> str:
    """Return the theseus-rs platform triple for this host."""
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Linux":
        if machine in ("x86_64", "amd64"):
            return "x86_64-unknown-linux-gnu"
        if machine in ("aarch64", "arm64"):
            return "aarch64-unknown-linux-gnu"
    if system == "Darwin":
        if machine in ("arm64", "aarch64"):
            return "aarch64-apple-darwin"
        if machine in ("x86_64", "amd64"):
            return "x86_64-apple-darwin"
    raise RuntimeError(
        f"Unsupported platform for theseus-rs PG binaries: {system}/{machine}"
    )


@dataclass(frozen=True)
class PgBinary:
    """A resolved PostgreSQL binary distribution on disk."""

    major: int
    full_version: str
    install_dir: Path

    @property
    def bin_dir(self) -> Path:
        return self.install_dir / "bin"

    @property
    def lib_dir(self) -> Path:
        return self.install_dir / "lib"

    @property
    def share_dir(self) -> Path:
        return self.install_dir / "share"

    @property
    def initdb(self) -> Path:
        return self.bin_dir / "initdb"

    @property
    def pg_ctl(self) -> Path:
        return self.bin_dir / "pg_ctl"

    @property
    def postgres(self) -> Path:
        return self.bin_dir / "postgres"

    @property
    def psql(self) -> Path:
        return self.bin_dir / "psql"

    def env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Return an environment dict with PATH/LD_LIBRARY_PATH wired up."""
        base = dict(os.environ)
        base["PATH"] = f"{self.bin_dir}:{base.get('PATH', '')}"
        ld_var = "DYLD_LIBRARY_PATH" if platform.system() == "Darwin" else "LD_LIBRARY_PATH"
        base[ld_var] = f"{self.lib_dir}:{base.get(ld_var, '')}"
        if extra:
            base.update(extra)
        return base


class PgBinaryManager:
    """Downloads and unpacks theseus-rs PG tarballs into a shared cache."""

    def __init__(self, cache_dir: Path = CACHE_DIR) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def url_for(self, full_version: str) -> str:
        triple = _platform_triple()
        return (
            "https://github.com/theseus-rs/postgresql-binaries/releases/download/"
            f"{full_version}/postgresql-{full_version}-{triple}.tar.gz"
        )

    def tarball_path(self, full_version: str) -> Path:
        return self.cache_dir / f"postgresql-{full_version}.tar.gz"

    def install_dir(self, full_version: str) -> Path:
        return self.cache_dir / f"postgresql-{full_version}"

    def ensure(self, major: int) -> PgBinary:
        """Return a PgBinary for `major`, downloading/extracting if needed."""
        if major not in PG_VERSIONS:
            raise ValueError(
                f"PG major {major} not in known versions: {sorted(PG_VERSIONS)}"
            )
        full_version = PG_VERSIONS[major]
        install = self.install_dir(full_version)
        marker = install / ".ready"
        if not marker.exists():
            self._download_and_extract(full_version, install)
            marker.touch()
        binary = PgBinary(major=major, full_version=full_version, install_dir=install)
        if not binary.initdb.exists():
            raise RuntimeError(
                f"initdb missing after extraction at {binary.initdb}"
            )
        return binary

    def _download_and_extract(self, full_version: str, install: Path) -> None:
        tarball = self.tarball_path(full_version)
        if not tarball.exists():
            url = self.url_for(full_version)
            tmp = tarball.with_suffix(tarball.suffix + ".part")
            with urllib.request.urlopen(url) as resp, open(tmp, "wb") as out:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
            tmp.rename(tarball)
        # Extract into a sibling staging dir, then atomically rename so we
        # never leave a half-extracted tree under install/.
        if install.exists():
            subprocess.run(["rm", "-rf", str(install)], check=True)
        staging = install.with_name(install.name + ".staging")
        if staging.exists():
            subprocess.run(["rm", "-rf", str(staging)], check=True)
        staging.mkdir(parents=True)
        with tarfile.open(tarball, "r:gz") as tf:
            tf.extractall(staging)
        # The tarball top-level is a single directory; flatten it.
        entries = list(staging.iterdir())
        if len(entries) == 1 and entries[0].is_dir():
            entries[0].rename(install)
            staging.rmdir()
        else:
            staging.rename(install)
