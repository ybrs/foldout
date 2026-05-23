# NEXT-STEPS

Three follow-up tasks. Implement in the order listed.

---

## 1. CLI integration test (priority 1)

### Why
The existing test suites (`tests/test_page_diff.py`,
`tests/test_page_diff_3way.py`) call `cross_diff` / `cross_diff_3way`
directly as Python functions. They do not exercise the *user-facing*
path:

- `vka branch <main> <branch>` — does the COW copy, registers base in
  `vka_databases`, takes both snapshots
- `vka diff <branch>` — looks up branch + base, picks 3-way path,
  produces SQL preview
- `vka diff <branch> --apply` — applies SQL, drops the base snapshot,
  exits non-zero on conflict

Bugs in catalog tracking, snapshot path resolution, base lookup,
auto-cleanup, or exit codes would all slip past the existing tests.

### Scope
A new test module `tests/test_cli_diff.py` that drives the actual
`vkarious` CLI binary via `subprocess`, asserts on its output and exit
codes, and verifies post-state via SQL.

### Scenarios

| # | Branch does | Parent does | `vka diff` expected | `vka diff --apply` expected |
|---|---|---|---|---|
| 1 | nothing | nothing | "no changes" | exit 0, no-op |
| 2 | add table | nothing | DDL+INSERTs | exit 0; parent has new table |
| 3 | add table A | add table B independently | DDL+INSERTs for A, B listed as drift | exit 0; parent has both A and B |
| 4 | add col + update row | parent adds different col | ALTER+UPDATE, parent's col listed as drift | exit 0; parent has both cols, row updated |
| 5 | update row R to X | parent updates same row R to Y | conflicts=1, no SQL | **exit non-zero**, parent unchanged |
| 6 | normal merge | nothing | shows base name + 3-way preview | exit 0; base auto-dropped after apply |
| 7 | normal merge | nothing | (re-run after apply) | exit 0; base already dropped, falls back gracefully |

### Acceptance criteria
- Test file uses `subprocess.run(['.venv/bin/vkarious', 'diff', ...])`,
  parses stdout/stderr, asserts exit code.
- Each scenario sets up its own throwaway parent + branch DBs and
  cleans up at the end (regardless of test outcome).
- Test runs in CI alongside the existing suites.
- All 7 scenarios pass.

### Tricky bits
- The CLI requires `VKA_DATABASE` env var. Tests must set it.
- Branch creation calls `register_branch_database` which writes to the
  `vkarious` metadata DB. Tests must clean those rows up afterwards.
- The `__base__<branch>` snapshot DB also needs cleanup if `--apply`
  isn't reached or fails.

---

## 2. No-PK 3-way support

### Why
Currently `cross_diff_3way` *skips* tables without a primary key. The
2-way path has a multiset fallback (treats whole row as identity,
INSERTs and DELETEs without UPDATEs). 3-way needs the same idea,
extended with base as a reference.

### Design

For tables without a PK, identity is the whole row content. The 3-way
operation is on multisets:

- `BASE`   = multiset of rows at branch creation
- `BRANCH` = multiset of rows currently on branch
- `MAIN`   = multiset of rows currently on parent

Define per-row counts and apply:

```
branch_delta[row]  = BRANCH[row] - BASE[row]   # branch's intent (+/-)
main_delta[row]    = MAIN[row]   - BASE[row]   # parent's intent
```

For each distinct row R:

| branch_delta[R] | main_delta[R] | Action on parent |
|---|---|---|
| 0 | 0 | nothing |
| +N | 0 | INSERT R, N times |
| -N | 0 | DELETE R, N times (one ctid each via the `ctid = (SELECT … LIMIT 1)` trick) |
| 0 | ±N | parent drift; leave alone |
| +N | +M | INSERT R, N times (parent already has +M of its own) |
| -N | -M | DELETE R, min(N,M) times (both deleted; if branch deleted more than parent did, leftover branch-deletes apply) |
| **+N** | **-M** | conflict: branch wants more of R, parent removed some |
| **-N** | **+M** | conflict: branch wants fewer of R, parent added some |

Conflicts are rare in practice (require both sides to touch the same
exact row identity). Treat them like row-level conflicts for PKs:
exit non-zero from `--apply`.

### Scope
- Add no-PK fallback to `cross_diff_3way` (mirror existing no-PK code
  in 2-way `cross_diff`).
- Reuse text-cast row fetching and the SQL emit conventions (multiset
  DELETE via `WHERE ctid = (SELECT … LIMIT 1)`).
- Find candidate rows on changed pages on both sides plus base
  (because a deletion only shows up as "row in base but not in current
  side", same trick as PK path).

### Tests (`tests/test_page_diff_3way.py`)

Add scenarios:
- `no-pk-branch-inserts-parent-untouched` → INSERT emitted, no conflict
- `no-pk-branch-deletes-parent-untouched` → DELETE emitted
- `no-pk-branch-inserts-parent-inserts-different` → INSERT for branch's row, parent's row listed as drift
- `no-pk-branch-deletes-row-parent-also-deletes-same-row` → no-op (both agree)
- `no-pk-branch-inserts-row-parent-deletes-same-row` → conflict (rare but well-defined)
- `no-pk-duplicate-row-handling` — branch inserts a 4th copy of a row that already has 3 copies; emit one INSERT.

### Acceptance criteria
- All 6 no-PK scenarios pass end-to-end including the apply+hash
  convergence check (where applicable).
- 2-way no-PK tests still pass (regression safety).

---

## 3. 2-way fallback with visible warning

### Why
Branches created before 3-way landed (or with the base DB manually
dropped) have no merge base. Today `vka diff` already falls back to
2-way for these — but the warning is buried in normal diff output
("base: (none — falling back to 2-way diff)"). Users skimming the
output can easily miss it. Worse, in `--apply` mode, the warning is
the only hint that "this might propose dropping things the parent
added independently" — and `--apply` proceeds without an extra
confirmation step.

### Design

1. **Make the fallback warning prominent.** Emit it as a single
   highlighted block on stderr (not interleaved with normal output):

   ```
   ⚠ No merge base — running 2-way diff against parent's current state.
     This may propose DROP statements for objects the parent added
     independently. Recreate this branch with `vka branch` (after this
     feature's release) to enable 3-way merge.
   ```

   Use ANSI yellow/bold if stderr is a TTY.

2. **In `--apply` mode without a base**, require an explicit opt-in
   flag: `--allow-2way-apply` (or similar). Without it, refuse:

   ```
   ⚠ Refusing to apply a 2-way diff (branch has no base).
     If you understand the risks, re-run with --allow-2way-apply.
   ```

   This is the "you might lose parent's independent changes" guard rail.

3. **Suggest a re-branch path.** Output should tell the user how to
   recover: `vka rebranch <branch>` (future) or
   `vka delete <branch> && vka branch <parent> <branch>` (today).

### Scope
- `src/vkarious/cli.py`:
  - Detect missing base → print the prominent warning
  - Add `--allow-2way-apply` flag to `vka diff`
  - Refuse `--apply` without that flag when base is missing
- No engine changes (`cross_diff` already exists).

### Tests
- CLI integration tests (item 1) include scenarios:
  - Diff a branch with no base → fallback warning visible
  - `--apply` on a no-base branch → exit non-zero, parent untouched
  - `--apply --allow-2way-apply` on a no-base branch → exit 0

### Acceptance criteria
- Warning appears on stderr in red/yellow when running on a TTY.
- `--apply` without `--allow-2way-apply` on a baseless branch exits
  non-zero with a clear refusal message.
- `--allow-2way-apply` bypasses the refusal.

---

## Suggested order
1. **CLI integration test scaffold + scenarios 1–6.** This catches
   regressions for everything we already shipped and is the path most
   users hit.
2. **No-PK 3-way support.** Self-contained engine change; tests are an
   extension of the existing 3-way test file.
3. **2-way fallback warning.** Smallest change; benefits from the CLI
   test scaffold being in place.
