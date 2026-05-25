#!/usr/bin/env python3
"""Persistent PostgreSQL test harness for the foldout integration suite.

Spins up one long-running cluster per `ClusterVariant` from
`tests/integration/clusters.py`. State (PGDATA path, port) is written to
`.test-harness.json` at the repo root; the pytest session picks that up
and attaches instead of spawning its own clusters.

Usage:
    python scripts/run-harness.py start      # bring up every variant
    python scripts/run-harness.py stop       # tear them down
    python scripts/run-harness.py status     # show what's running
    python scripts/run-harness.py restart    # stop + start

The script is idempotent: `start` skips variants that are already up and
healthy, `stop` is harmless when nothing's running. Designed for local
dev loops where you want to run `pytest -m integration` many times in a
row without paying the initdb cost on every invocation.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests"))

from integration.clusters import VARIANTS, ClusterVariant  # noqa: E402
from integration.pg_cluster import PgCluster  # noqa: E402
from integration.pg_runtime import PgBinaryManager  # noqa: E402


HARNESS_DATA_ROOT = REPO_ROOT / ".test-harness-data"
STATE_FILE = REPO_ROOT / ".test-harness.json"


def _pgdata_for(variant: ClusterVariant) -> Path:
    """Stable PGDATA path per variant (so restarts reuse the existing one)."""
    return HARNESS_DATA_ROOT / variant.name


def _is_listening(port: int, host: str = "127.0.0.1",
                  timeout_s: float = 0.5) -> bool:
    """Return True if `host:port` accepts a TCP connection right now."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _read_state() -> dict[str, dict[str, Any]]:
    """Read the harness state file, returning an empty dict if missing."""
    if not STATE_FILE.exists():
        return {}
    with open(STATE_FILE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _write_state(state: dict[str, dict[str, Any]]) -> None:
    """Atomically write the harness state file."""
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")
    tmp.rename(STATE_FILE)


def _start_variant(variant: ClusterVariant,
                   manager: PgBinaryManager,
                   state: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Bring one variant up (or reuse if already running). Update state in place."""
    existing = state.get(variant.name)
    if existing and _is_listening(int(existing["port"])):
        print(f"  {variant.name}: already running on port {existing['port']}")
        return existing

    binary = manager.ensure(variant.pg_major)
    pgdata = _pgdata_for(variant)
    cluster = PgCluster(
        binary,
        data_root=HARNESS_DATA_ROOT,
        extra_conf=variant.extra_conf,
    )
    # Force the stable per-variant pgdata path (PgCluster.__init__ picks
    # a random one by default). We do this AFTER init so we keep its
    # mkdir of the data_root.
    cluster.pgdata = pgdata
    cluster.socket_dir = pgdata / ".s"
    cluster.log_file = pgdata.with_suffix(".log")
    cluster.cluster_id = variant.name

    cluster.initdb()
    cluster.start()
    print(f"  {variant.name}: started on port {cluster.port} "
          f"(pgdata={cluster.pgdata})")

    record: dict[str, Any] = {
        "pg_major": variant.pg_major,
        "pgdata": str(cluster.pgdata),
        "port": cluster.port,
        "log_file": str(cluster.log_file),
        "binary_install_dir": str(binary.install_dir),
    }
    state[variant.name] = record
    return record


def _stop_variant(variant_name: str,
                  state: dict[str, dict[str, Any]],
                  manager: PgBinaryManager) -> None:
    """Stop one variant's cluster and wipe its PGDATA."""
    record = state.get(variant_name)
    if not record:
        print(f"  {variant_name}: not tracked, skipping")
        return
    pgdata = Path(record["pgdata"])
    binary_install_dir = Path(record["binary_install_dir"])
    pg_ctl = binary_install_dir / "bin" / "pg_ctl"
    if pg_ctl.exists() and (pgdata / "postmaster.pid").exists():
        subprocess.run(
            [str(pg_ctl), "-D", str(pgdata), "-m", "immediate",
             "-w", "-t", "20", "stop"],
            check=False, capture_output=True, text=True,
        )
    if pgdata.exists():
        subprocess.run(["rm", "-rf", str(pgdata)],
                       check=False, capture_output=True, text=True)
    log_file = Path(record["log_file"])
    if log_file.exists():
        try:
            log_file.unlink()
        except OSError:
            pass
    state.pop(variant_name, None)
    print(f"  {variant_name}: stopped, pgdata removed")


def cmd_start() -> int:
    """Start every variant that isn't already running. Updates state file."""
    HARNESS_DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manager = PgBinaryManager()
    state = _read_state()
    print(f"Starting harness clusters under {HARNESS_DATA_ROOT}/")
    for variant in VARIANTS:
        _start_variant(variant, manager, state)
    _write_state(state)
    print(f"State written to {STATE_FILE}")
    return 0


def cmd_stop() -> int:
    """Stop every variant listed in the state file and remove the file."""
    state = _read_state()
    if not state:
        print("No harness state — nothing to stop.")
        return 0
    manager = PgBinaryManager()
    print(f"Stopping harness clusters listed in {STATE_FILE}")
    # Iterate over a snapshot so _stop_variant can pop from state.
    names: list[str] = []
    for name in state:
        names.append(name)
    for name in names:
        _stop_variant(name, state, manager)
    if state:
        _write_state(state)
    else:
        STATE_FILE.unlink(missing_ok=True)
    return 0


def cmd_status() -> int:
    """Print what the state file says is up and whether the ports respond."""
    state = _read_state()
    if not state:
        print("No harness running.")
        return 0
    print(f"{'variant':<16}{'port':<8}{'reachable':<12}pgdata")
    print("-" * 70)
    for name in sorted(state):
        record = state[name]
        port = int(record["port"])
        reachable = "yes" if _is_listening(port) else "no"
        print(f"{name:<16}{port:<8}{reachable:<12}{record['pgdata']}")
    return 0


def cmd_restart() -> int:
    """Stop + start. Convenience for picking up config changes."""
    rc = cmd_stop()
    if rc != 0:
        return rc
    # Brief pause so the kernel reclaims any TIME_WAIT sockets.
    time.sleep(0.5)
    return cmd_start()


def main(argv: list[str] | None = None) -> int:
    """Entrypoint. Dispatches to cmd_start / cmd_stop / cmd_status / cmd_restart."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command",
                        choices=("start", "stop", "status", "restart"))
    args = parser.parse_args(argv)
    dispatch = {
        "start": cmd_start,
        "stop": cmd_stop,
        "status": cmd_status,
        "restart": cmd_restart,
    }
    return dispatch[args.command]()


if __name__ == "__main__":
    sys.exit(main())
