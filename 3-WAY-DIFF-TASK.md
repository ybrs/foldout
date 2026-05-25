# 3-WAY-DIFF-TASK

## Goal

Make `foldout diff <branch>` git-like: report and propagate only the
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

foldout already creates branches via COW file copies in ~milliseconds.
We use the same mechanism for the merge base:

**At `foldout branch main feat1` time, additionally create a COW snapshot
of `main` and tag it as the branch's base.** It's a real Postgres
database we can query (schema + rows). Storage cost is ~0 because COW
shares pages with `main` until either side writes.

Tagging: extend `fld_databases` with a `base_for` column (or a new
`type='base'` row pointing at the branch). Naming: `__base__feat1`
(prefix chosen so users don't accidentally connect to it for editing).

When the branch is dropped (`vka delete branch …`), drop the base too.
When the branch is "merged" (`fld diff --apply` succeeds without
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
diverged. Exit non-zero from `fld diff --apply`. Plain `fld diff`
(preview) still prints the planned SQL above the conflict report so the
user sees what *would* run if conflicts were resolved.

Future: `--ours` / `--theirs` flags for global override (out of scope
for v1).

## API and CLI changes

- `fld_databases`: add a relation from a branch to its base snapshot.
  Either a new column on the branch row (`base_oid`) or a new row with
  `type='base', parent=branch_oid`. Lean toward the column for
  simplicity.
- `foldout branch <main> <name>`: after the existing COW copy, take a
  **second** COW copy of `<main>` named `__base__<name>`, register the
  link, and snapshot its page-diff state into
  `~/.foldout/snapshots/<branch_oid>.json` (same file we already write).
- `foldout diff <branch>`:
  - If branch has a base → 3-way diff.
  - If no base (older branch from before this feature) → warn and fall
    back to 2-way with a hint to recreate the branch.
- `foldout diff <branch> --apply`: abort on conflict, exit non-zero.

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

- `src/foldout/cli.py` — `branch` (create base), `diff` (3-way path).
- `src/foldout/db.py` — base-snapshot tracking in `fld_databases`.
- `src/foldout/page_diff.py` — `dump_schema_3way`, `cross_diff_3way`.
- `src/foldout/migration/foldout_3.sql` — schema migration to add
  base linkage.
- `tests/test_page_diff.py` — new conflict + drift scenarios.

## Status (as of 2026-05-25)

### Done — engine

- ✅ `merge_schemas_3way()` — full 3-way schema merge: table-level for
  adds/drops, column-level inside common tables (so "branch adds X,
  parent adds Y" is compatible, not a conflict), primary keys,
  indexes, constraints, views, materialized views, functions,
  sequences, schemas.
- ✅ `diff_schemas_3way()` returns `(pre, post, conflicts, drifts)`.
- ✅ `cross_diff_3way()` — full row-level 3-way:
  page-LSN scan on both sides; candidate PKs collected from changed
  pages of branch and main AND from base's same-block-number pages
  (so DELETEs are detected); authoritative PK-based fetches on all
  three sides; per-row 3-way classification.
- ✅ Type-agnostic value handling: every column fetched as `col::text`,
  emitted as `'<text>'::<typename>`. Works for jsonb, arrays, custom
  enums, PostGIS, etc. — no Python-side type interpretation.
- ✅ Sequences (incl. `SERIAL`): `CREATE SEQUENCE`,
  `ALTER ... OWNED BY`, `setval(...)` emitted in dependency order.
- ✅ Drift report: parent's independent changes are listed but not
  touched (column, table, row, sequence, function, view, etc.).

### Done — branching / snapshot lifecycle

- ✅ `vka branch` creates `__base__<branch>` (COW snapshot of parent)
  alongside the branch DB, inside the same write lock.
- ✅ `vka branch` also saves a parent stats snapshot inside the lock
  (`<branch_oid>_parent.json`) — used for stat-skip on the parent side
  during diff (restored 3-way diff time from ~22 s to ~170 ms on a
  4.76 GB DB).
- ✅ `fld_databases` schema migration `foldout_3.sql` adds `base_oid`
  column linking a branch to its base snapshot.
- ✅ Successful `fld diff --apply` auto-drops the base DB + parent
  snapshot file.

### Done — CLI

- ✅ `fld diff <branch>` uses 3-way when a base exists.
- ✅ Preview mode (`fld diff` without `--apply`) always exits 0 — even
  with conflicts. Conflicts and drifts are reported for the user to see.
- ✅ `fld diff --apply` exits non-zero on any conflict; nothing is
  applied to the parent.
- ✅ When a branch has no base (legacy branch, or post-apply re-run):
  - Prominent yellow/bold warning on stderr explaining the risk.
  - `--apply` is refused unless `--allow-2way-apply` is passed.
  - Recovery hint: drop and recreate the branch.

### Done — tests (54 / 54 passing)

| File | Tests | Coverage |
|---|---:|---|
| `tests/test_schema_3way.py` | 9 | unit-level 3-way merge matrix (no Postgres) |
| `tests/test_page_diff.py`   | 24 | e2e for `cross_diff()` (2-way path; regression safety) |
| `tests/test_page_diff_3way.py` | 18 | e2e for `cross_diff_3way()` — parent-drift, both-sides changes, conflicts, deletes, **no-PK multiset 3-way** |
| `tests/test_cli_diff.py`    | 9 | full CLI path: `vka branch` + `fld diff` + `--apply` + exit codes + base lifecycle + 2-way fallback hardening |

CLI test scenarios:

- `no-changes` — empty diff, exit 0
- `branch-adds-table-parent-untouched` — DDL + INSERTs, apply succeeds, base auto-dropped
- `parent-drifts-branch-adds-table` — branch's table created, parent's reported as drift, NOT dropped
- `column-changes-on-both-sides` — both `ADD COLUMN` survive; branch's UPDATE applied
- `conflict-aborts-apply` — preview exits 0; `--apply` exits 1; parent untouched; base preserved
- `no-base-warning-on-diff` — prominent stderr warning when base is missing
- `no-base-refuses-apply` — `--apply` exits 1, parent untouched
- `no-base-allow-2way-apply` — `--allow-2way-apply` flag bypasses refusal
- `apply-then-rerun-falls-back` — base auto-dropped after apply; re-run shows fallback warning

### Remaining

- ✅ **No-PK 3-way support.** Implemented in `cross_diff_3way()` using
  multiset deltas: for each distinct row R on changed pages,
  `bD = count(R in branch's changed pages) - count(R in base's same
  pages)` and `mD = count(R in main's changed pages) - count(R in
  base's same pages)`. Matrix:
  - `bD=0, mD=0` → skip
  - `bD=0, mD≠0` → drift (kind `row_no_pk`)
  - `bD≠0, mD=0` → apply branch's intent
  - same sign → apply branch's "extra" (`max(0, |bD|−|mD|)`)
  - opposite signs → conflict (kind `row_no_pk`)

  No-PK tables that also have a column ADDed on branch are recorded as
  drift (`no_pk_with_added_columns`) and skipped — that combo is
  deferred.

  Tests in `tests/test_page_diff_3way.py`:
  - `no-pk-branch-inserts-parent-untouched`
  - `no-pk-branch-deletes-parent-untouched`
  - `no-pk-both-insert-different-rows`
  - `no-pk-both-delete-same-row`
  - `no-pk-branch-inserts-parent-deletes-conflict`
  - `no-pk-duplicate-row-handling`

### Deferred (not needed for v1)

- Three-way diff for views/functions at body-text level. Currently we
  emit DROP+CREATE on any body difference; "both rewrote the same
  function" appears as a conflict only if both versions differ.
- No-PK tables that also have `ADD COLUMN` on branch — currently
  surfaced as a `no_pk_with_added_columns` drift and skipped.
- Auto-rebase ("pull main's drift into the branch first, then diff").
- Conflict resolution flags (`--ours` / `--theirs`).
- A `vka rebranch <branch>` convenience command (today: drop + branch).
