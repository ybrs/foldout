# 3-WAY-DIFF-TASK

## Goal

Make `vkarious diff <branch>` git-like: report and propagate only the
changes the branch made since it was created, not "everything that
differs between branch and parent right now". Specifically:

- A table that exists on the BRANCH but not the PARENT, and that didn't
  exist when the branch was created → `CREATE TABLE` (branch added it).
- A table that exists on the PARENT but not the BRANCH, and that existed
  when the branch was created → `DROP TABLE` (branch removed it).
- A table that exists on the PARENT but not the BRANCH, and that didn't
  exist when the branch was created → **don't emit `DROP TABLE`** — the
  parent added it independently. Report it as informational drift.

Same logic at row level: do not emit DELETEs for rows the parent added
independently; do not emit INSERTs for rows the branch never touched
that just happen to be missing from the parent's view.

## Why a third reference state is required

Today the diff is **two-way**: branch (current) vs. parent (current).
It cannot distinguish "branch added X" from "parent removed X" because
both look identical to a two-way comparison. We need the state at
**branch creation time** as the merge base.

```
        BASE  =  parent's state at the moment branch was created
        BRANCH = current state of the branch
        MAIN  =  current state of the parent
```

For any object O (table / column / index / row …):

| BASE | MAIN | BRANCH | Meaning                                    | Action                  |
|------|------|--------|--------------------------------------------|-------------------------|
| same | same | same   | nothing happened                           | no-op                   |
| same | same | diff   | branch changed O                           | apply branch's value    |
| same | diff | same   | main changed O independently               | report drift; leave it  |
| same | diff | diff   | both changed; check if **same change**     | no-op if equal          |
| same | diff | diff   | both changed differently                   | **CONFLICT → abort**    |

## How we get BASE — without writing anything new

vkarious already creates branches via COW file copies in ~milliseconds.
We use the same mechanism for the merge base:

**At `vkarious branch main feat1` time, additionally create a COW snapshot
of `main` and tag it as the branch's base.** It's a real Postgres
database we can query (schema + rows). Storage cost is ~0 because COW
shares pages with `main` until either side writes.

Tagging: extend `vka_databases` with a `base_for` column (or a new
`type='base'` row pointing at the branch). Naming: `__base__feat1`
(prefix chosen so users don't accidentally connect to it for editing).

When the branch is dropped (`vka delete branch …`), drop the base too.
When the branch is "merged" (`vka diff --apply` succeeds without
conflicts), the base can be safely dropped too (the branch's changes
are now in main; the base is no longer the reference point).

## What changes in cross_diff

We open **three** connections instead of two:

```python
src_conn   = parent (MAIN — current state)
cur_conn   = branch (BRANCH — current state)
base_conn  = parent's snapshot (BASE — branch creation time)
```

### Schema diff becomes three-way

Replace the current `diff_schemas(src, tgt)` with
`diff_schemas_3way(base, main, branch)`:

```python
for each object in (base.objects ∪ main.objects ∪ branch.objects):
    in_base   = object in base
    in_main   = object in main
    in_branch = object in branch

    # Pure branch additions / removals
    if (not in_base) and in_branch and (not in_main):  emit CREATE on main
    if in_base and (not in_branch) and in_main:        emit DROP   on main
    if (not in_base) and (not in_branch) and in_main:  # main drift, leave it
        report("parent independently added: …")

    # Modifications
    if in_base and in_branch and in_main:
        if base == branch:           # branch didn't touch it
            if main != base: report("parent drift")  # main drift, leave it
            continue
        if base == main:             # main didn't touch it
            emit ALTER on main to match branch
        else:
            if branch == main: continue          # both made same change
            CONFLICT(object)         # abort merge
```

### Row-level diff becomes three-way (per table)

For each table present on both BRANCH and MAIN:

1. Page-LSN scan on **branch** vs `snapshot_lsn` → set of changed pages on branch.
2. Page-LSN scan on **main**   vs `snapshot_lsn` → set of changed pages on main.
3. For each changed page, fetch its current rows + the corresponding rows
   from **base** (the snapshot DB is queryable just like any other DB —
   `SELECT * FROM "tbl" WHERE ctid = …` works).

For each PK appearing in any of the three sides:

```
base_row    = SELECT ... FROM base.tbl WHERE pk = ?     -- or NULL if not in base
branch_row  = SELECT ... FROM branch.tbl WHERE pk = ?
main_row    = SELECT ... FROM main.tbl WHERE pk = ?
```

Then the 3-way logic exactly mirrors the schema case:

| base | branch | main | action                                |
|------|--------|------|---------------------------------------|
| -    | row    | -    | INSERT (branch new row)               |
| -    | -      | row  | drift on main, leave                  |
| -    | row    | row  | both added; equal? no-op : CONFLICT   |
| row  | -      | row  | branch DELETEd                        |
| row  | row    | -    | drift on main (it deleted)            |
| row  | -      | -    | both deleted                          |
| row  | row    | row  | compare; branch-only change → UPDATE; both changed → no-op if equal else CONFLICT |

### Conflict handling (v1)

**Abort with a structured conflict report.** No partial apply. List each
conflicted object/row with all three sides so the user can see what
diverged. Exit non-zero from `vka diff --apply`. Plain `vka diff`
(preview) still prints the planned SQL above the conflict report so the
user sees what *would* run if conflicts were resolved.

Future: `--ours` / `--theirs` flags for global override (out of scope
for v1).

## API and CLI changes

- `vka_databases`: add a relation from a branch to its base snapshot.
  Either a new column on the branch row (`base_oid`) or a new row with
  `type='base', parent=branch_oid`. Lean toward the column for
  simplicity.
- `vkarious branch <main> <name>`: after the existing COW copy, take a
  **second** COW copy of `<main>` named `__base__<name>`, register the
  link, and snapshot its page-diff state into
  `~/.vkarious/snapshots/<branch_oid>.json` (same file we already write).
- `vkarious diff <branch>`:
  - If branch has a base → 3-way diff.
  - If no base (older branch from before this feature) → warn and fall
    back to 2-way with a hint to recreate the branch.
- `vkarious diff <branch> --apply`: abort on conflict, exit non-zero.

## Out of scope (deferred)

- Three-way diff for functions / views (we emit DROP+CREATE on any
  difference; conflicts would just look like "both rewrote the same
  function"). Treat the *body text* equality at object level for v1.
- Auto-rebase ("pull main's drift into branch first, then diff").
- Conflict resolution flags (`--ours` / `--theirs`).
- Base-snapshot lifecycle automation (auto-delete on merge). Manual
  delete for v1.

## Acceptance criteria

- Test: BRANCH adds `t1`, MAIN adds `t2`, base has neither. Diff emits
  `CREATE TABLE t1`, does **not** emit `DROP TABLE t2`, reports drift.
- Test: BRANCH alters column type, MAIN does not. Diff emits ALTER.
- Test: BRANCH and MAIN both alter the same column to different types.
  Diff aborts with CONFLICT, no SQL emitted.
- Test: BRANCH inserts row id=1, MAIN inserts row id=2. Diff emits
  one INSERT for id=1, does not touch id=2.
- Test: BRANCH updates row id=1 to "x", MAIN updates same row to "y".
  Diff aborts with CONFLICT.
- The existing 24 two-way scenarios still pass (3-way behaves like
  two-way when MAIN equals BASE, which is true when MAIN didn't drift).

## Files this will touch

- `src/vkarious/cli.py` — `branch` (create base), `diff` (3-way path).
- `src/vkarious/db.py` — base-snapshot tracking in `vka_databases`.
- `src/vkarious/page_diff.py` — `dump_schema_3way`, `cross_diff_3way`.
- `src/vkarious/migration/vkarious_3.sql` — schema migration to add
  base linkage.
- `tests/test_page_diff.py` — new conflict + drift scenarios.

## Status (as of 2026-05-23)

### Done

- ✅ `merge_schemas_3way()` — full 3-way schema merge:
  table-level for adds/drops, column-level inside common tables
  (so "branch adds X, parent adds Y" is compatible, not a conflict),
  primary keys, indexes, constraints, views, materialized views,
  functions, sequences, schemas.
- ✅ `diff_schemas_3way()` returns `(pre, post, conflicts, drifts)`.
- ✅ `cross_diff_3way()` — full row-level 3-way:
  page-LSN scan on both sides; candidate PKs collected from changed
  pages of branch and main AND from base's same-block-number pages
  (so DELETEs are detected); authoritative PK-based fetches on all
  three sides; per-row 3-way classification.
- ✅ `vka branch` creates `__base__<branch>` (COW snapshot of parent)
  and saves a stats snapshot of the parent inside the write lock.
- ✅ `vka diff` uses 3-way when base exists, drops base + parent snap
  on successful `--apply`.
- ✅ Conflict abort: `vka diff --apply` exits non-zero on any conflict.
- ✅ Drift report: parent's independent changes are listed but not
  touched (column, table, row, sequence, function, view, etc.).
- ✅ Stat-skip on main side via the parent snapshot — restored real-DB
  diff time from ~22 s to ~170 ms on coinleverprod.
- ✅ Type-agnostic value handling: every column fetched as `col::text`,
  emitted as `'<text>'::<typename>`. Works for jsonb, arrays, custom
  enums, PostGIS, etc.
- ✅ Sequences (incl. `SERIAL`): `CREATE SEQUENCE`, `ALTER ... OWNED BY`,
  `setval(...)` emitted in the right order.

### Tests

- ✅ `tests/test_schema_3way.py` — 9 unit tests on the dict-level merge
  matrix (no Postgres needed).
- ✅ `tests/test_page_diff_3way.py` — 12 e2e scenarios against a real
  Postgres, exercising `cross_diff_3way()` directly with a parent
  snapshot.
- ✅ `tests/test_page_diff.py` — 24 e2e scenarios for the 2-way path
  (regression coverage; still all passing).
- ❌ **CLI integration test** — see `NEXT-STEPS.md`.

### Remaining (see `NEXT-STEPS.md`)

- ❌ CLI integration test for `vka branch` + `vka diff` (the actual
  user-facing path; right now we only test the Python function).
- ❌ No-PK 3-way fallback (currently skips no-PK tables in 3-way).
- ❌ 2-way fallback with visible warning when a branch has no base.
