# foldout

foldout is a tool to create snapshots and branches of PostgreSQL databases.

Important: This project is an active work-in-progress. Expect rapid changes, occasional instability, and breaking changes as features evolve. To get notified about updates, use the Watch button on the repository (choose "All Activity"). Manage your watch settings for this repo at: https://github.com/ybrs/foldout/subscription

## Configuration

Set the `FLD_DATABASE` environment variable to your PostgreSQL connection string:

```bash
export FLD_DATABASE="postgresql://username:password@localhost:5432/database_name"
```

Optionally, set `FLD_PG_DATA_PATH` to override the detected PostgreSQL data directory. This is useful when PostgreSQL runs in a container but foldout runs on the host and needs a host-visible path for copying database files (e.g., COW file copies):

```bash
# Example: host path where the container's PGDATA is mounted
export FLD_PG_DATA_PATH="/Users/me/docker-volumes/postgres-data"
```

## Usage

List all databases:
```bash
foldout databases list
```

Create a snapshot:
```bash
foldout snapshot database_name
```

Create a branch with a custom name:
```bash
foldout branch database_name branch_name
```

When `FLD_PG_DATA_PATH` is set, foldout uses that directory for physical file operations instead of querying `SHOW data_directory` from PostgreSQL.

List snapshots:
```bash
foldout snapshots list
```

Restore from a snapshot:
```bash
foldout snapshots restore database_name snapshot_name
```

Delete a snapshot:
```bash
foldout snapshots delete snapshot_name
```

Check version:
```bash
foldout version
```

Show what changed on a branch (relative to its parent):
```bash
foldout diff branch_name           # preview SQL only
foldout diff branch_name --apply   # apply changes to the parent
```

## How `foldout diff` works

At `foldout branch` time, foldout records a small snapshot file under
`~/.foldout/snapshots/<branch_oid>.json` capturing:

- The WAL LSN at the moment of branching.
- For every relation: `(relfilenode, segment_path, size, mtime_ns)`.

At `foldout diff` time, we do two things on top of these snapshots —
**both entirely read-only** on the parent and the branch.

### Schema (DDL) diff — catalog comparison
We dump each side's relevant `pg_catalog` state (schemas, tables, columns,
primary keys, indexes, FK/CHECK/UNIQUE constraints, views, materialized
views, functions, sequences) and emit the SQL statements that would make
the parent look like the branch:
`CREATE SCHEMA`, `CREATE SEQUENCE`, `CREATE TABLE`, `ALTER TABLE ADD/DROP/ALTER COLUMN`,
`CREATE INDEX`, `ADD CONSTRAINT`, `CREATE VIEW`, `CREATE OR REPLACE FUNCTION`,
plus the matching `DROP …` and `setval(…)` statements where appropriate.

### Row-level (DML) diff — page-LSN filtering
Every 8 KB Postgres page has a header containing `pd_lsn` — the LSN of
the WAL record that last wrote it. The diff uses three nested filters:

1. **File `(size, mtime, relfilenode)`** — one `stat()` per relation
   file. Equal to snapshot → skip the file entirely. Most files are
   skipped.
2. **Per-page `pd_lsn`** — for the few files that changed, mmap and
   read the 24-byte header of each 8 KB page. `pd_lsn > snapshot_lsn`
   → page was modified after the branch was created.
3. **Per-page line pointers + tuple headers** — parse the live tuples
   on each changed page, fetch row data via
   `SELECT ctid::text, col::text AS col … WHERE ctid = ANY(…)`, then
   compute the per-row diff against the same pages on the parent.

Cost scales with **what actually changed**, not with database size.
On a 4.76 GB database, a no-change diff takes ~2 ms; a typical diff
(handful of rows changed in one table) takes 25–100 ms.

### Type-agnostic value handling
Values are fetched cast to text (`col::text`) and emitted as
`'<text>'::<typename>` SQL literals. Python never interprets Postgres
values. This works for any type with normal text I/O — built-in types
(`int`, `text`, `jsonb`, arrays, ranges), custom enums and domains,
and extension types (e.g. PostGIS geometry). The diff code itself is
type-free.

## `foldout diff` — requirements, limitations, portability

### Hard requirements
- **CLI must run on the same host as PostgreSQL.** We read `PGDATA`
  files directly. This is a same-host dev tool — it won't work against
  managed cloud Postgres (RDS, Cloud SQL) where you can't see `PGDATA`.
- **Read access to `PGDATA`.** Typically the Postgres OS user only.
- **Permission to `CHECKPOINT`.** Superuser, or member of the
  `pg_checkpoint` role on PG15+. Used to flush dirty buffers to disk
  so committed writes are visible to file-level scanning.

### Soft assumptions (default settings work)
- **`block_size = 8192`.** Default everywhere. Currently hardcoded;
  will be auto-detected later.
- **`segment_size = 1 GB`.** Postgres compile-time setting; default
  everywhere. Same auto-detect plan.
- **Default tablespace.** Relations are expected under
  `PGDATA/base/<dboid>/<relfilenode>`. Tables in a custom tablespace
  aren't currently followed via `pg_tblspc/`.

### What's NOT relevant (common misconceptions)
- `wal_level` (`minimal`/`replica`/`logical`) — `pd_lsn` is stamped on
  every buffer modification regardless.
- `full_page_writes`, `wal_compression`, `synchronous_commit`,
  `data_checksums`, `shared_buffers` — none affect this approach.
- Postgres major version — page header layout (and `pd_lsn` in
  particular) has been unchanged since at least PG 9.x. Tested on PG17.

### Current limitations
- **Unlogged tables** (`CREATE UNLOGGED TABLE`) — pages aren't routed
  through WAL on normal writes, so `pd_lsn` may be `0` / stale. Diff
  would silently underreport. Workaround: avoid unlogged tables in
  branched databases.
- **Hot standby replicas** can't run `CHECKPOINT`.
- **Two-way diff only (for now).** `foldout diff` compares branch
  vs. parent's *current* state. If the parent has independent changes
  since the branch was created, those would appear as inverse DROPs.
  See `3-WAY-DIFF-TASK.md` for the planned three-way diff using the
  snapshot as the merge base.

### Background docs
- `DATABASE-DIFF-TASK.md` — design and approach for `foldout diff`.
- `3-WAY-DIFF-TASK.md` — planned three-way diff.
- `notes.md` — hashing benchmark numbers (alternative diff approach).

# Example
```
export FLD_DATABASE="postgresql://@localhost:5432/postgres"
(foldout) $ uv run foldout databases list
OID        Database Name
------------------------------
14042      postgres
4          template0
1          template1
65786      test
```



## Development

Install the project in editable mode and run the CLI using
[uv](https://docs.astral.sh/uv/):

```bash
uv venv
uv pip install -e .
uv run foldout --help
```

Run the test suite with:

```bash
uv run pytest
```
