# Changelog

All notable changes to foldout will be listed here. Pre-1.0; expect
breaking changes between minor releases.

## [Unreleased]

### Breaking

- **`foldout diff` no longer applies SQL.** It's now read-only:
  SQL goes to stdout, progress and summary go to stderr. Use the new
  `foldout apply <file>` to apply the diff. Workflow:

  ```
  foldout diff feature_x > diff.sql
  $EDITOR diff.sql                    # review / tweak before applying
  foldout apply diff.sql              # runs the SQL against the parent
  foldout delete-branch feature_x     # clean up after merge
  ```

  Removed flags from `foldout diff`: `--apply`, `--sql-only`,
  `--allow-2way-apply`. The 2-way fallback (when no merge base exists)
  is now automatic, with a yellow stderr warning.

- **Page-index moved from JSON files to the foldout metadata DB.**
  The per-branch "what did the branch look like at branch time" data
  used to live at `~/.foldout/snapshots/<branch_oid>.json` (and
  `..._parent.json`). It now lives in the new `fld_page_index` table
  in the foldout DB (migration `foldout_4.sql`). Reasons:

  - Cleanup is automatic on `delete-branch` instead of leaking files.
  - No `$HOME` dependency (worked badly in containers / CI).
  - Shared across machines that point at the same cluster.
  - One source of truth, queryable via SQL.

  **Existing branches must be re-created** to get a usable page-index in
  the new table. `foldout diff <existing_branch>` against a v0.1 branch
  will fail with "No page-index found for branch '...'". Drop and
  re-branch:

  ```
  DROP DATABASE feature_x;
  foldout branch appdb feature_x
  ```

  The old `~/.foldout/snapshots/` dir is no longer read or written by
  foldout. It's safe to delete by hand.

- **`get_snapshot_dir` / `get_branch_snapshot_path` /
  `get_branch_parent_snapshot_path` removed from `foldout.db`.** Anyone
  who was importing these helpers should migrate to
  `save_page_index(branch_oid, kind, index)` /
  `load_page_index(branch_oid, kind)` /
  `delete_page_index_for_branch(branch_oid)`.

- **`page_diff.snapshot(pgdata, dbname, out_path)` removed.** Use
  `page_diff.build_page_index(pgdata, dbname)` which returns a
  `PageIndex` object directly (no JSON file involved).

- **`page_diff.cross_diff` and `page_diff.cross_diff_3way` signatures
  changed.** The `snap_path: str` / `parent_snap_path: str` parameters
  are now `index: PageIndex | None` / `parent_index: PageIndex | None`.
  Passing `None` disables the page-LSN filter — every page becomes a
  candidate. Used by the new two-arg `foldout diff <A> <B>` form for
  ad-hoc diffs of unrelated databases.

### Added

- **`foldout apply <file>`** — applies a previously-saved diff to its
  target database. Reads the `-- parent: <db>` line from the
  `-- foldout-diff vN` header that `foldout diff` writes. `--target <db>`
  overrides the target. No cleanup side effects.

- **`foldout delete-branch <name>`** — drops the branch DB, its merge
  base DB (`__base__<name>`), the `fld_databases` rows for both, and
  all `fld_page_index` rows for the branch. Explicit destructive
  command; no confirmation prompt (matches `git branch -D` UX).

- **`foldout diff <left> <right>`** — two-arg form: diffs two arbitrary
  databases without needing a registered foldout branch relationship.
  Walks every page of `<left>` (no page-index filter), so cost is
  O(`<left>` size). A yellow stderr warning is printed for databases
  >100 MB. Useful for ad-hoc "what's different between prod and
  staging?" investigations.

- **`--force` on `foldout snapshot` and `foldout branch`** — see the
  v0.2 lock-database changes below (separately noted).

- **`foldout diff` now writes a parseable header** in the SQL output:

  ```sql
  -- foldout-diff v1
  -- parent: appdb
  -- branch: feature_x
  -- mode: 3-way
  -- base: __base__feature_x
  --
  INSERT INTO items ...
  ```

  `foldout apply` reads this header to know where to send the SQL.
  Header fields can be retargeted with `apply --target`.

### Changed

- **Page-LSN diff now skips TOAST tables** (`pg_toast.pg_toast_*`)
  in the row-level scan loop. TOAST is PG-internal storage for
  oversized values — when an owning row's INSERT/UPDATE/DELETE is
  applied, PG manages the corresponding TOAST chunks itself. Emitting
  diff SQL against the TOAST tables directly was a latent bug (the
  diff code doesn't understand TOAST's `(chunk_id, chunk_seq)` shape);
  it only surfaced in the new full-scan path.

- **Diff/3-way diff now accept partial / missing page-indexes**: a
  relation not present in the index is treated as "fully changed,
  scan every page" instead of being silently skipped. This is what
  makes the full-scan two-arg form work.

- **Lock-database (snapshot/branch).** Replaces the no-op advisory
  lock in `database_write_lock`. New flow:

  1. `ALTER DATABASE <src> ALLOW_CONNECTIONS false` (blocks new connections).
  2. Check `pg_stat_activity` for non-self backends on `<src>`.
  3. Empty → CHECKPOINT, do work, restore `ALLOW_CONNECTIONS true`.
  4. Non-empty + no `--force` → fail with the connection list, restore.
  5. Non-empty + `--force` → `pg_terminate_backend` each, wait until
     empty (10s timeout with loud failure), do work, restore.

  See `TASKS.md` for the crash-recovery note (if foldout is `kill -9`'d
  mid-snapshot, the source DB stays locked — manual `ALTER DATABASE …
  ALLOW_CONNECTIONS true` recovers).

### Removed

- `--apply` from `foldout diff`. Use `foldout apply` instead.
- `--sql-only` from `foldout diff`. Default behavior already puts only
  SQL on stdout — just redirect with `>`.
- `--allow-2way-apply` from `foldout diff`. Apply is now decoupled;
  the user is expected to review SQL before running `apply`.
- `page_diff.snapshot()`, `page_diff.load_page_index_from_file()`,
  `db.get_snapshot_dir()`, `db.get_branch_snapshot_path()`,
  `db.get_branch_parent_snapshot_path()`.

### Internal

- New table `fld_page_index` (migration `foldout_4.sql`):
  PK `(branch_oid, kind)`, kind ∈ {`'branch'`, `'parent'`}, JSONB
  column for the relations list.
- `tests/test_page_diff.py` and `tests/test_page_diff_3way.py` removed
  and ported to `tests/integration/test_cross_diff.py` and
  `tests/integration/test_cross_diff_3way.py`. They now run against
  the harness clusters (pg16, pg17, pg18-default, pg18-clone) like
  every other integration test, instead of requiring a hardcoded
  developer-laptop PG.
