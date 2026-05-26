#!/usr/bin/env bash
# Manual single-user PostgreSQL 17 cluster, separate from the test
# harness. Uses the theseus-rs PG bundle already downloaded under
# .test-cache/pg-binaries/.
#
# Usage:
#   scripts/my-pg17.sh init       # one-time: initdb + write conf overrides
#   scripts/my-pg17.sh configure  # apply conf overrides to an existing PGDATA
#   scripts/my-pg17.sh start      # start the server
#   scripts/my-pg17.sh stop       # stop the server (fast: wait for queries)
#   scripts/my-pg17.sh restart    # stop + start
#   scripts/my-pg17.sh status     # show port / socket / pid from postmaster.pid
#   scripts/my-pg17.sh psql       # open psql connected to this cluster
#   scripts/my-pg17.sh destroy    # stop + rm -rf the data dir
#
# Env overrides:
#   PGDATA   data directory (default: $REPO_ROOT/my-pg17-data)
#   PORT     listen port    (default: 5499)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PG_BUNDLE="$REPO_ROOT/.test-cache/pg-binaries/postgresql-17.9.0"
PGDATA="${PGDATA:-$REPO_ROOT/my-pg17-data}"
PORT="${PORT:-5499}"

BIN="$PG_BUNDLE/bin"
export LD_LIBRARY_PATH="$PG_BUNDLE/lib:${LD_LIBRARY_PATH:-}"

# Marker string used to detect whether our overrides have already been
# appended to postgresql.conf — so `configure` is idempotent.
CONF_MARKER="# foldout my-pg17.sh overrides"

write_conf_overrides() {
  if grep -qF "$CONF_MARKER" "$PGDATA/postgresql.conf" 2>/dev/null; then
    echo "overrides already present in $PGDATA/postgresql.conf"
    return 0
  fi
  cat >> "$PGDATA/postgresql.conf" <<EOF

$CONF_MARKER
port = $PORT
unix_socket_directories = '/tmp'
listen_addresses = '127.0.0.1'
EOF
  echo "appended overrides to $PGDATA/postgresql.conf (port=$PORT, socket=/tmp)"
}

case "${1:-help}" in
  init)
    if [ -e "$PGDATA/PG_VERSION" ]; then
      echo "error: $PGDATA looks already initialized. Use 'configure' to"
      echo "       add port/socket overrides, or 'destroy' to wipe + redo."
      exit 1
    fi
    rm -rf "$PGDATA"
    "$BIN/initdb" -D "$PGDATA" -U postgres --auth=trust --encoding=UTF8 --no-locale
    write_conf_overrides
    echo
    echo "Initialized $PGDATA. Next: $0 start"
    ;;

  configure)
    if [ ! -e "$PGDATA/PG_VERSION" ]; then
      echo "error: $PGDATA is not an initialized PG cluster."
      echo "       run '$0 init' first."
      exit 1
    fi
    write_conf_overrides
    echo "If the server is running, restart it: $0 restart"
    ;;

  start)
    "$BIN/pg_ctl" -D "$PGDATA" -l "$PGDATA/postgres.log" -w start
    echo
    "$0" status || true
    echo
    echo "Connect with: $0 psql"
    ;;

  stop)
    "$BIN/pg_ctl" -D "$PGDATA" -m fast stop
    ;;

  restart)
    "$0" stop || true
    "$0" start
    ;;

  status)
    if [ ! -e "$PGDATA/postmaster.pid" ]; then
      echo "not running (no $PGDATA/postmaster.pid)"
      exit 1
    fi
    awk 'NR==1{print "pid:    " $0}
         NR==4{print "port:   " $0}
         NR==5{print "socket: " $0}
         NR==6{print "listen: " $0}' "$PGDATA/postmaster.pid"
    ;;

  psql)
    shift || true
    PGHOST=127.0.0.1 PGPORT="$PORT" PGUSER=postgres PGDATABASE=postgres \
      "$BIN/psql" "$@"
    ;;

  destroy)
    "$BIN/pg_ctl" -D "$PGDATA" -m immediate stop 2>/dev/null || true
    rm -rf "$PGDATA"
    echo "destroyed $PGDATA"
    ;;

  help|--help|-h|"")
    sed -n '2,21p' "$0"
    ;;

  *)
    echo "unknown command: $1" >&2
    sed -n '2,21p' "$0" >&2
    exit 2
    ;;
esac
