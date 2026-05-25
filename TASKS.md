# foldout open tasks

This file tracks design decisions and follow-up work that isn't tied to
a single PR. Anything here is fair game to pick up — start a discussion
in the issue tracker before non-trivial changes.

---

## Snapshot / branch consistency safety

**Status**: design agreed, not yet implemented.

### Background

`foldout snapshot` and `foldout branch` currently use a `cp --reflink=always`
copy from the source PGDATA to the new database's PGDATA. This is only
safe if **no one is writing to the source while the copy runs**. Reflinks
are atomic per file but not across the dozens of files that make up a
PostgreSQL database — a transaction committing mid-copy can leave the
snapshot with index pages newer than the heap they reference, TOAST
chunks missing, or catalog inconsistencies.

The existing `pg_advisory_lock(12345)` in `database_write_lock()` does
**not** protect against this — advisory locks are cooperative and SQL
clients ignore them. It was a leftover from when foldout was an internal
tool with all callers under our control.

PG 18 with `file_copy_method=clone` is already safe because it uses
`CREATE DATABASE foo TEMPLATE src STRATEGY=FILE_COPY`, which makes PG
itself enforce the no-connections constraint. Older PG and PG 18 with
`file_copy_method=copy` still go through our manual `cp` path and need
explicit handling.

### Agreed design

The lock goes on **first**, before checking `pg_stat_activity`. This
eliminates the race where a client connects between the "is anyone
there?" check and the start of the copy. Existing connections are not
affected by `ALLOW_CONNECTIONS=false`; only new ones are rejected.

1. `ALTER DATABASE src WITH ALLOW_CONNECTIONS false` — lock first.
2. Query `pg_stat_activity` for any backend on `src` other than ours.
3. Dispatch on the result:
   - **Empty** → CHECKPOINT, do the snapshot work, restore `ALLOW_CONNECTIONS true`.
   - **Non-empty without `--force`** → restore `ALLOW_CONNECTIONS true`,
     fail loudly with the full list (pid, application_name, state,
     client_addr, query) and tell the user to close clients or re-run with
     `--force`.
   - **Non-empty with `--force`** →
     `pg_terminate_backend(pid)` for every non-self backend, poll
     `pg_stat_activity` until empty with a 10s timeout (fail loudly with
     the still-connected list on timeout), CHECKPOINT, do the work,
     restore `ALLOW_CONNECTIONS true`.

In every path the restore goes in a `try / finally` so a Python
exception or `Ctrl-C` releases the lock automatically. The only
unrecoverable case is `kill -9` between lock and unlock — covered under
"Known limitation" below.

The existing `pg_advisory_lock(12345)` in `database_write_lock()` is
**removed** as part of this change. It locked nothing in practice and
gave false confidence. CHECKPOINT remains (narrows the dirty-buffer
window) but isn't load-bearing for correctness on its own — the lock is.

`--force` is a single flag, applied to `snapshot` and `branch`. Restore
is unchanged; it already terminates connections because the user is
explicitly destroying the source.

`--force` is a single flag meaning "I authorize you to kick everyone." No
sub-flags, no `--unsafe` synonym. Restore is unchanged (it already
terminates connections because the user is explicitly destroying the source).

### Known limitation: crash recovery

If `foldout snapshot --force` crashes (or is `kill -9`'d) between the
`ALLOW_CONNECTIONS false` and the corresponding `true` in the finally,
the source database stays unreachable. Users will see:

```
FATAL: database "source_db" is not currently accepting connections
```

**Recovery (manual)**: connect to the `postgres` database as a superuser and run:

```sql
ALTER DATABASE source_db WITH ALLOW_CONNECTIONS true;
```

We are intentionally **not** building self-heal tracking for this in v1.
Reasoning:

- This is targeted at development workloads where the same operator
  controls the snapshot and the source.
- The recovery is a single, well-documented `ALTER DATABASE`.
- Self-heal would require persisting "DBs I locked" to durable storage
  and reconciling on next foldout invocation — extra state for a rare
  edge case.

If this turns out to bite real users, revisit by tracking locked DBs in
the `foldout` metadata DB (new column on `fld_databases` or a small
`fld_locks` table) and offering a `foldout admin unlock <db>` command.

---

## Other deferred items

- **Block size / segment size auto-detection** in `page_diff`. Currently
  hardcoded to 8192 / 1 GB. See README "Soft assumptions".
- **Unlogged tables** are silently missed by `page_diff` (their pages
  carry stale `pd_lsn`). README documents this — no fix planned until
  someone needs it.
- **Hot standby replicas** can't run `CHECKPOINT`, so `foldout diff` is
  primary-only. Documented in README.
- **`pg_internal.init` removal** in `copy_database_files()` is "remove
  if present, debug-log if missing." Acceptable, but the original code
  had a `print("no internal file ?")` which suggests the author wasn't
  sure. Worth confirming the file's lifecycle (it's a cache, regenerated
  on first relmap load — so missing is fine post-`CREATE DATABASE FILE_COPY`).
