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
foldout diff branch_name                       # preview: SQL + conflicts + drift summary
foldout diff branch_name --sql-only            # print just the SQL (script-friendly)
foldout diff branch_name --apply               # run the SQL against the parent
foldout diff branch_name --apply --allow-2way-apply
                                               # opt-in apply for branches without
                                               # a merge base (see "Three-way diff")
```

`foldout diff` is a true three-way merge against the branch's frozen
merge base (`__base__<branch>`) — it won't drop tables or rows the
parent added independently. See **Three-way diff** below for the full
decision matrix, conflict semantics, and limits.

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

## Three-way diff (what `foldout diff` actually does)

`foldout diff` compares **three** database states, not two:

```
   BASE   = parent's state at the moment the branch was created
   BRANCH = branch's current state
   MAIN   = parent's current state
```

Without `BASE`, a two-way diff cannot distinguish *"branch added X"*
from *"parent removed X"* — both look identical when you only compare
branch vs. parent right now. With `BASE` as the merge base, every
difference is attributed to exactly one side, just like in `git merge`.

### How we get BASE — instant, free, queryable

When you run `foldout branch main feat1`, foldout takes **two** COW
copies of `main` inside a single write lock:

- `feat1` — the branch you'll work on.
- `__base__feat1` — a frozen reference; you don't touch this directly.

Both copies are page-level copy-on-write clones, so they cost ~0 disk
until either side writes. `__base__feat1` is a real Postgres database
— `foldout diff` queries it directly with regular SQL while computing
the merge.

Alongside the branch snapshot file, foldout also writes
`~/.foldout/snapshots/<branch_oid>_parent.json`, a stat-baseline for
`main` captured inside the same lock. That lets the diff engine
**stat-skip** unchanged files on the parent side just as it does on
the branch side; without it, every diff would have to LSN-scan every
page of MAIN (which is the difference between ~170 ms and ~22 s on a
4.76 GB database).

### The decision matrix

For every catalog object O (schema, table, column, index, constraint,
view, function, sequence) and every row, foldout answers three
yes/no questions: *was O in BASE? in MAIN? in BRANCH?* — and looks up
the action:

| BASE | MAIN | BRANCH | Meaning                                  | Action                          |
|:----:|:----:|:------:|------------------------------------------|---------------------------------|
|  –   |  –   |  yes   | branch added O                           | CREATE / INSERT on main         |
|  –   |  yes |   –    | main added O independently               | drift — leave alone             |
|  –   |  yes |  yes   | both added; same definition?             | no-op if equal, else CONFLICT   |
|  yes |  yes |   –    | branch removed O                         | DROP / DELETE on main           |
|  yes |   –  |  yes   | main removed O independently             | drift — leave alone             |
|  yes |   –  |   –    | both removed                             | no-op                           |
|  yes |  yes |  yes   | branch changed, main unchanged           | apply branch's change           |
|  yes |  yes |  yes   | branch unchanged, main changed           | drift — leave alone             |
|  yes |  yes |  yes   | both changed, same result                | no-op                           |
|  yes |  yes |  yes   | both changed, different results          | CONFLICT — abort                |

The same matrix runs at every granularity — table existence, column
existence, column type, primary key, index definition, FK, view body,
function body, row presence, row value.

### Examples

**1) Independent additions:**
```
-- branch creates t1 (since branching); main creates t2 (since branching).
-- BASE has neither. foldout diff emits:
CREATE TABLE t1 (...);            -- branch's add
INSERT INTO t1 VALUES (...);      -- and its data
-- t2 is NOT dropped. It is reported under "parent drift".
```

**2) Branch ADD COLUMN, main untouched:**
```sql
-- branch:  ALTER TABLE events ADD COLUMN severity int;
-- main:    no change.
-- BASE doesn't have `severity`; MAIN doesn't have it. Apply branch's intent:
ALTER TABLE events ADD COLUMN severity int;
```

**3) Both sides ADD COLUMN to the same table — different columns:**
```sql
-- branch:  ALTER TABLE events ADD COLUMN severity int;
-- main:    ALTER TABLE events ADD COLUMN region text;
-- Compatible: different columns. foldout emits ALTER for `severity`;
-- `region` is parent drift, left alone.
```

**4) Both ADD COLUMN with the same name, different types → CONFLICT:**
```
-- branch: ALTER TABLE u ADD COLUMN v integer;
-- main:   ALTER TABLE u ADD COLUMN v text;
-- foldout aborts. No SQL emitted. Reported in the conflict block.
```

**5) Row-level: branch updates row 1, main updates row 2:**
```sql
-- branch: UPDATE u SET v='branch-1' WHERE id=1;
-- main:   UPDATE u SET v='main-2' WHERE id=2;
-- foldout emits only the branch's update; row 2 is drift.
UPDATE u SET v='branch-1' WHERE id=1;
```

**6) Both update row 1 to different values → CONFLICT.**

**7) Branch DELETEs row 1, main UPDATEd row 1 → CONFLICT** (ambiguous
intent: branch wants it gone, main was actively editing it).

### Row identity: PK and no-PK

**Tables with a primary key** use the PK as row identity. foldout
LSN-scans changed pages on BRANCH and MAIN, collects candidate PKs
(including from BASE's same pages, so DELETEs of rows on unchanged
pages are still caught), then does authoritative PK-based fetches on
all three sides. Each row is classified into one of INSERT/UPDATE/DELETE/NOOP
on each side, and the matrix above is applied.

**Tables without a primary key** can't use PK identity, so foldout
falls back to **multiset deltas** of changed-page contents:

```
bD[row] = count(row in BRANCH's changed pages) - count(row in BASE's same pages)
mD[row] = count(row in MAIN's changed pages)   - count(row in BASE's same pages)
```

| bD  | mD  | Meaning                                  | Action                       |
|:---:|:---:|------------------------------------------|------------------------------|
|  0  |  0  | nothing on either side                   | skip                         |
|  0  |  ≠0 | parent drift                             | leave alone (reported)       |
|  ≠0 |  0  | branch's intent                          | INSERT / DELETE              |
| +N  | +M  | both inserted                            | INSERT max(0, N − M)         |
| −N  | −M  | both deleted                             | DELETE max(0, N − M)         |
| +N  | −M  | branch inserts, main deletes (same row)  | CONFLICT                     |
| −N  | +M  | branch deletes, main inserts (same row)  | CONFLICT                     |

Deletes go out as `DELETE … WHERE ctid = (SELECT ctid FROM t WHERE
<full-row-match> LIMIT 1)` so duplicates aren't over-deleted.

### Conflicts and drifts

- **Conflict** = both sides changed the same thing in incompatible
  ways. `foldout diff` (preview) prints all conflicts and still exits
  0 so you can read them. `foldout diff --apply` exits non-zero and
  applies **nothing** — v1 has no partial apply.
- **Drift** = the parent's independent changes since the branch was
  created. Drift is reported for transparency but `foldout diff`
  never touches it; applying the branch shouldn't undo work the
  parent did on its own.

### What `foldout diff --apply` does on success

The SQL is executed against `main`. On success, foldout:

- Drops `__base__<branch>` (its job is done; main is the new reference point).
- Drops the row in `fld_databases` for the base.
- Deletes the parent snapshot file.

The branch DB itself is left alone. You can keep working on it; the
next `foldout diff` from that branch will fall back to a plain
two-way diff (see below).

### Branches without a base (2-way fallback)

A branch can lack a base in two situations:

1. The branch was created before 3-way diff existed.
2. You've already run `foldout diff --apply`, which drops the base by design.

In both cases `foldout diff` falls back to a plain **two-way diff**
(BRANCH vs. MAIN), prints a prominent yellow warning on stderr, and
**refuses `--apply`** unless you pass `--allow-2way-apply`. A 2-way
apply can incorrectly DROP / DELETE things the parent added
independently — opt in only when you're sure main hasn't drifted
since.

To recover full 3-way behavior after an apply, drop and rebranch.

### What we can and can't do

**Handled end-to-end:**

- DDL: schemas, tables, columns (add / drop / type change), primary
  keys, indexes, FK / UNIQUE / CHECK constraints, views, materialized
  views, functions, sequences (including `SERIAL` and `setval`).
- DML: INSERT / UPDATE / DELETE on tables with a primary key.
- DML: INSERT / DELETE on tables without a primary key (multiset
  semantics — UPDATEs appear as DELETE + INSERT, which is the only
  correct interpretation for an unidentifiable row).
- Type-agnostic values: jsonb, arrays, custom enums, ranges, PostGIS,
  any extension type with normal text I/O. Python never interprets
  the value.

**Deliberately NOT handled in v1 (and why):**

- **View / function bodies at "line-merge" granularity.** Bodies are
  compared whole. If both sides rewrote the same function: identical
  text → no-op, different text → CONFLICT. We don't try to 3-way
  merge function source. *Why:* SQL/PLpgSQL line-merging is its own
  product; for v1 the safe default is "you resolve it".
- **No-PK tables with a branch-side `ADD COLUMN`.** The no-PK matcher
  uses full-row content as identity. Once the column count differs
  between BASE and BRANCH, `('a',)` and `('a', NULL)` look like
  different rows to a `Counter` — the matcher would report spurious
  inserts and deletes. foldout records this as
  `no_pk_with_added_columns` drift and skips the row-level diff for
  that table; the DDL still applies. *Workaround:* add a primary key
  (we strongly recommend one anyway), or pre-apply the column change
  to main before diffing.
- **Auto-rebase.** foldout does not pull main's drift into the branch
  before diffing. If main has drifted heavily, conflicts may be
  unavoidable. *Why:* rebase semantics are a separate feature and
  belong in `vka rebase`, not silently inside `diff`.
- **`--ours` / `--theirs` overrides.** No global conflict-resolution
  flags. *Why:* in v1 we keep the failure mode loud and explicit; the
  human resolves. We'll add these once we've seen real workflows that
  actually want them.
- **Cross-tablespace tables.** Relations under custom tablespaces
  (`pg_tblspc/`) aren't followed yet. *Why:* not in scope for v1;
  uncommon in dev workflows.
- **Unlogged tables.** WAL-less writes don't bump `pd_lsn`
  predictably; diff would silently underreport. *Why:* by design,
  these tables aren't crash-safe — we don't try to merge them.
- **Managed cloud Postgres** (RDS, Cloud SQL, etc.). The diff reads
  `PGDATA` files directly. The CLI must run on the same host as
  Postgres.

**Why no partial apply on conflict?** v1 treats `foldout diff --apply`
like `git merge` with no resolution strategy: it either applies
cleanly or it applies nothing. A partial apply would silently mix
"branch's wins" with "main's drifts" in non-obvious ways. If you need
to ship a subset, resolve the conflicts in source first.

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

### Background docs
- `DATABASE-DIFF-TASK.md` — original design notes for `foldout diff`.
- `3-WAY-DIFF-TASK.md` — three-way diff design, decision matrix, and
  implementation status.
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
