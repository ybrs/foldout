# DATABASE-DIFF-TASK

## Goal

Given two databases that share a common ancestor (a foldout branch and its
source, or two branches of the same source), efficiently identify the
row-level and schema-level differences so we can:

1. Merge changes from a branch back into the source.
2. Verify a merge actually converged.
3. Show developers what changed on a branch.

Primary use case is the clone / work / merge loop driven by AI coding agents:
a developer hands an agent a git worktree and a database branch. Agents
mostly write migrations; raw DML changes are the minority. Branching must be
effectively instant (every agent gets one). Merging can take a few seconds.

## Why not just "hash both sides and diff"

We can hash a 5 GB DB in ~9 s on a MacBook Air with 8 parallel workers (see
`notes.md`). That's fine as a verification step but it's still O(database
size) regardless of how little changed. It also only tells us *that*
something differs, not *what* — for merge we need the actual changed rows.

A scan-everything approach ignores the fact that we already know a lot about
how the two databases diverged: they share a recent common ancestor, and
Postgres tracks every write at the page level.

## The key insight: layered filtering, finest possible signal at each layer

When foldout branches a database it does a COW copy of PGDATA at a known
point in time. Three pieces of metadata together let us pinpoint changes
without scanning data:

1. **File `(size, mtime, relfilenode)`** — coarse, "did any byte in this file
   change?". One `stat()` per relation file; microseconds.
2. **Per-page `pd_lsn`** — every 8 KB page stamps the LSN of the WAL record
   that last wrote it. Compare against the LSN we recorded at clone time.
   Tells us *which specific 8 KB pages* changed within a changed file.
3. **Per-page line pointers + tuple headers** — once we know a page changed,
   parsing its line pointers and tuple xmin/xmax tells us which specific
   tuples were inserted, updated, or deleted.

Each layer is a strict refinement of the previous: do the cheap filter
first, only descend when it tells you to. The naive "walk every page header
in the DB" approach — which I initially benchmarked — was missing the outer
file-stat filter and as a result wasn't faster than hashing. Once the outer
filter is in place, the cost scales with what changed, not with DB size.

## Resilience against clock skew

The outer filter compares `(size, mtime_ns, relfilenode)` for **equality**
against values recorded at snapshot time — not a `>` comparison against
current wall-clock time. So NTP corrections, DST changes, and manual clock
edits cannot cause false negatives: a write modifies the file in a way that
changes its `mtime_ns`, regardless of what the wall clock says.

The only way to fool the outer filter is for a write to leave `size`,
`mtime_ns`, and `relfilenode` all bit-identical to the recorded values,
which on APFS (nanosecond mtime resolution) is essentially impossible.

The inner filter is exact — `pd_lsn` comparison is a numeric check against a
durable LSN value stored in the page itself; no time involved.

## Benchmark — `page_diff_v2.py` on `coinleverprod` (4.76 GB, 153 relations)

| Operation | Wall time |
|---|---:|
| `snapshot` (stat all files + record LSN) | 320 ms |
| `diff` — no changes | **2 ms** |
| `diff` — 3 rows changed in 1 small table (fresh test DB) | **9 ms** |

The snapshot cost is dominated by psycopg connection setup + the catalog
query for `pg_relation_filepath` across all relations; the `stat()` calls
themselves are microseconds. (Worth revisiting — see "Open questions".)

The diff time scales with **what actually changed**, not with database size.

## Approach (current state)

### Phase 0 — record clone metadata ✅ implemented

`page_diff_v2.py snapshot <pgdata> <dbname> <out.json>`

For every user relation:

- `(schema, name, oid, relfilenode, relpath)`
- For every segment file: `(path, size, mtime_ns)`

Plus a single `pg_current_wal_lsn()` capture.

### Phase 1 — identify changed pages ✅ implemented

`page_diff_v2.py diff <pgdata> <dbname> <snap.json>`

Layered:

1. Compare `relfilenode` against snapshot — mismatch means the relation was
   rewritten (`VACUUM FULL`, `TRUNCATE`, `CLUSTER`). Whole relation changed.
2. For each segment file: compare `(size, mtime_ns)` against snapshot.
   Equal → skip the file entirely.
3. For segments that differ: mmap and stride through page headers, comparing
   `pd_lsn` against the snapshot LSN. Emit `(relation, block_number, lsn)`
   for every page with `pd_lsn > snapshot_lsn`.

Output: a list of (relation, block_numbers) — the candidate page set.

### Phase 2 — extract changed tuples ⏳ next

For each candidate page:

- Read the full 8 KB page.
- Parse the line pointer array (4-byte ItemIdData entries between offset 24
  and `pd_lower`).
- For each `LP_NORMAL` line pointer, build the tuple's `ctid =
  (block_number, lp_index + 1)`.
- Fetch live tuples via `SELECT *, ctid FROM rel WHERE ctid = ANY(...)`. This
  delegates tuple decoding (varlena, TOAST, column types) to Postgres
  instead of reimplementing it.
- For deletes / dead tuples: parse the tuple header (`t_xmin`, `t_xmax`) to
  identify rows that existed at snapshot time but no longer do. Decoding
  dead tuple bodies requires parsing the data ourselves (harder); for v1 we
  may extract only the PK columns from dead tuples, which is enough to
  emit a `DELETE`.

### Phase 3 — compute row-level diff

For tables with primary keys:

- Snapshot side: list of (PK, row) currently in source.
- Diverged side: list of (PK, row) currently on changed pages + PKs of dead
  tuples on those pages.
- Set differences yield insert / update / delete sets.

For tables without primary keys: fall back to full table content hashing for
those tables only (small minority in typical workloads).

### Phase 4 — apply or report

Either generate a SQL diff (INSERT/UPDATE/DELETE) the user can review and
apply, or apply it directly inside a single transaction.

Verification pass: re-run `hash_rust` on both sides post-merge.

## Open questions

1. **The 320 ms snapshot cost.** Currently dominated by psycopg connection
   setup and one catalog query. For an "every clone takes a snapshot" model
   this should be sub-100 ms. Possible fixes: skip the catalog query for
   relations we don't need (snapshot only what `pg_relation_filepath` returns
   non-null for), use a connection pool, or merge into the `foldout branch`
   command so the connection is already open.

2. **Hint bits and visibility writes.** Postgres can write to pages just to
   set hint bits (visibility/freezing info), bumping `pd_lsn` without any
   logical change. The page-LSN filter will see these as candidates;
   Phase 2's tuple-level inspection will correctly classify them as
   "no logical change." Expected behavior — but it does mean Phase 1's
   "candidate" set can be a superset of the true logical diff set.

3. **TOAST.** Wide column values live in a separate TOAST relation. If a
   TOASTed value changes, the heap tuple may not move but the TOAST page
   will. Phase 1 will flag the TOAST pages; Phase 2 needs to reassemble
   TOAST chains during tuple decoding (deferred via Postgres `SELECT`,
   which handles TOAST transparently — so this is "free" as long as we
   fetch via SELECT for live tuples).

4. **WAL summarization (PG17+).** `summarize_wal` would let us skip Phase 1
   entirely and ask Postgres directly for the changed-block list. Defer
   until we have a PG17-only mode or the version floor moves up.

5. **Tables without primary keys.** Page-LSN finds the page; we have no
   stable row identity. Fall back to hashing the affected tables, or
   require PKs.

6. **Concurrent writes during diff.** A write that lands while we're
   walking page headers could be observed half-applied. For now we assume
   the diff target is quiescent (clones used by AI agents typically are).
   If we need consistency under writes, take a `pg_export_snapshot()` and
   run the SELECT phase inside that snapshot.

## Out of scope (for now)

- Triggers on the user's DB. We already have a trigger-based capture path
  in `change_capture.sql`; this task is the no-triggers alternative. The
  two can coexist later (triggers for online capture, page-LSN for
  retrospective diff).
- Merging non-table objects (functions, views, sequences). Schema-only;
  handled by `ddl_log` replay.
- Three-way merge with conflict resolution. First cut is fast-forward only.

## Acceptance criteria

- On `coinleverprod` (4.76 GB, 79 tables): with the user touching ~1% of
  the data, the full diff (including row extraction) completes in under
  1 second.
- Detects insert, update, delete on tables with primary keys.
- Detects DDL via existing `ddl_log` (no new code needed here).
- Verification re-hash confirms post-merge equality.

## Files

- `page_diff_v2.py` — Phase 0 (snapshot) + Phase 1 (find changed pages).
- `notes.md` — hashing benchmarks (5 GB DB in ~9 s, 8 workers, LPT).
- `src/foldout/sql/change_capture.sql` — trigger-based alternative.
- `hash_rust/` — full-DB hashing, used as the verification step.
- `vkapgx/pg_hashdb` — server-side per-table hasher; not on the hot path.
