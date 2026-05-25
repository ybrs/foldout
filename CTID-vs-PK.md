# Why `ctid` can't replace a primary key for 3-way row diff

`ctid` looks tempting. It's small, it's stable across `SELECT`s in
the same transaction, and after a COW branch is taken the same row has
the *same* `ctid` on `BASE`, `BRANCH`, and `MAIN`. So why doesn't
foldout just use `ctid` as the row identity for tables without a
primary key?

Short answer: **`ctid` is a physical location, not a row identity.**
The moment anyone updates a row, its `ctid` changes — and after that
the "same logical row" on BRANCH and MAIN no longer shares a
`ctid`. The diff loses the link.

This document explains exactly where it breaks, what the
half-measures look like, and why detecting a parallel-UPDATE
**CONFLICT** in a no-PK table is much harder than it first appears.

---

## 1. The COW honeymoon: ctids align at branch time

Right after `foldout branch main feat1`, every page on `BRANCH` and
`__base__feat1` is a copy-on-write clone of the corresponding page on
`MAIN`. Same heap layout, same line pointers. So a row that lived at
`ctid (0,5)` on `MAIN` is also at `(0,5)` on `BRANCH` and `(0,5)` on
`BASE`.

We do exploit this — it's how we detect DELETEs today. If `BASE` has a
live ctid `(0,5)` and `BRANCH` has the same ctid marked dead, the
branch deleted that row. (See `live_ctids_for_block(pgdata,
base_relpath, block)` in `cross_diff_3way`.) That works because a
DELETE doesn't relocate the row; it just flips the line-pointer flag
to `LP_DEAD`.

The honeymoon ends as soon as someone runs `UPDATE`.

---

## 2. UPDATE breaks the alignment

Postgres MVCC never overwrites a tuple in place. An UPDATE:

1. Writes a brand-new tuple at a fresh heap slot (new `ctid`).
2. Marks the old tuple dead.
3. Sets the old tuple's `t_ctid` field to point at the new ctid
   (a "HOT chain" if no indexed column changed, otherwise a regular
   update chain).

Concretely, with `BASE`, `BRANCH`, `MAIN` all starting from the
COW-cloned layout:

```
At branch time (t=0):
  BASE   (0,5) = ('a',)   live
  BRANCH (0,5) = ('a',)   live
  MAIN   (0,5) = ('a',)   live

After BRANCH updates: UPDATE log SET msg='b' WHERE msg='a'
  BRANCH (0,5)  = ('a',)  DEAD, t_ctid → (0,12)
  BRANCH (0,12) = ('b',)  live

After MAIN independently updates: UPDATE log SET msg='c' WHERE msg='a'
  MAIN   (0,5)  = ('a',)  DEAD, t_ctid → (0,11)
  MAIN   (0,11) = ('c',)  live
```

Now ask: *"Which ctid identifies the row that was at `(0,5)`?"*

- On `BRANCH`, that row is now at `(0,12)`.
- On `MAIN`, the same row is at `(0,11)`.

The ctids don't match each other. They don't match `BASE`. A naive
diff comparing live ctids would say: branch deleted `(0,5)` and
inserted `(0,12)`; main deleted `(0,5)` and inserted `(0,11)`. Both
sides "deleted the same thing and inserted unrelated things" — which
is exactly the wrong story. The right story is **both sides updated
the same row in parallel → CONFLICT**.

---

## 3. The half-measure: chase HOT chains

We *could* recover the lost link by walking `t_ctid` pointers:

> *"`BRANCH` page 0 has a dead tuple at `(0,5)` whose `t_ctid` points
> at `(0,12)`, where the live tuple is `('b',)`. Therefore the row
> originally at `(0,5)` is now `('b',)` on branch."*

Apply that to both sides and you'd correctly conclude: branch ended
up with `('b',)`; main ended up with `('c',)`; both started from
`('a',)` at `(0,5)`. That's a CONFLICT.

This works in principle. In practice three things make it fragile:

### 3.1 VACUUM erases the chain

Once Postgres decides the dead tuple at `(0,5)` is no longer needed
for any active snapshot, autovacuum prunes it. The line pointer
becomes `LP_UNUSED` or, in the HOT-prune case, `LP_REDIRECT` (which
points *somewhere*, but the original line-pointer slot still exists
only as a redirect — its data is gone).

After vacuum runs on `MAIN`, there is no more `(0,5) → (0,11)`
breadcrumb to follow. We can still see `('c',)` at `(0,11)`; we can
still see `BASE`'s `('a',)` at `(0,5)`; but the link saying "these
are the same logical row" is gone.

The user has no idea when autovacuum ran. Their diff could go from
"correctly flagged conflict" to "silently merged as compatible
inserts" between two consecutive `foldout diff` invocations, just
because vacuum happened in between. That's the worst kind of
correctness bug — non-deterministic and invisible.

### 3.2 Non-HOT updates are messier

If the update changed an indexed column, the new tuple isn't HOT.
The heap-side update chain still exists, but there's a separate
index entry pointing at the new tuple. For a no-PK table this isn't
catastrophic (no PK index to manage), but for tables with secondary
indexes the diff would need to be careful not to double-count
versions.

### 3.3 We'd be parsing dead tuple headers

Today, foldout only reads **live** line pointers (`LP_NORMAL`) on
changed pages, then asks Postgres via `SELECT … WHERE ctid = ANY(…)`
to materialize them. The Python side never decodes a tuple — Postgres
does it.

To walk a HOT chain, foldout would have to parse the *dead* tuple
header bytes directly to find `t_ctid`, because dead tuples aren't
returned by `SELECT`. That means:

- Decoding `HeapTupleHeaderData` on disk.
- Handling `LP_DEAD`, `LP_REDIRECT`, `LP_UNUSED` line-pointer flags.
- Following multi-hop chains (a row can be updated repeatedly,
  producing `(0,5) → (0,12) → (0,17) → …`).
- Reading dead tuples on `BASE` too (not just branch/main), to find
  the chain root.

It's all doable. It's also a lot of code that depends on
implementation details of Postgres' heap format, which is more
stable than most things but isn't guaranteed across major versions.

---

## 4. Why "just mark a CONFLICT" isn't a free escape hatch

You might think: *we don't need exact row identity — just emit a
CONFLICT whenever both sides wrote to the same no-PK table.* That's
strictly safer, but it has its own costs:

1. **It conflicts on every parallel write, even truly independent
   inserts.** Two teammates appending to the same `audit_log` no-PK
   table would never be able to merge. For tables that are
   append-only (which is most of the legitimate no-PK use case),
   that's a worse experience than the current best-effort merge.
2. **You'd need a way to say "yes I know, apply it anyway".** Which
   is `--allow-2way-apply` reinvented for one specific failure mode.
3. **The detection signal we have is "both `branch_blocks` and
   `main_blocks` are non-empty for this table"** — *not* "both sides
   touched the same row". Without row identity we can't distinguish
   them. So a strict CONFLICT here would actually be a coarse
   approximation in the other direction.

What foldout does instead (today): emits a **warning** when both
sides wrote to the pages of a no-PK table, applies the multiset diff
as a best effort, and tells the user to add a PK if they want exact
semantics. The warning makes the trade-off visible without forcing a
conflict that's almost always a false positive.

---

## 5. Summary

| What we want                                  | Can `ctid` do it? | Notes                                                                                |
|-----------------------------------------------|:-----------------:|--------------------------------------------------------------------------------------|
| Detect a row that branch DELETEd              | ✅                | DELETE doesn't move the row; base's live ctid → branch's dead ctid is a clean signal |
| Detect a row that branch INSERTed             | ✅                | New ctid not present in base — clear add                                             |
| Detect a row that branch UPDATEd in isolation | ⚠️ partial        | Need to walk `t_ctid` chains; broken by VACUUM                                       |
| Detect parallel UPDATE on the same row (CONFLICT) | ❌            | Both sides' new ctids diverge; chains may be vacuumed; reliable detection requires PK |
| Detect a row whose shape changed (ADD COLUMN) | ❌                | Orthogonal — ctid identity wouldn't help because content keys still don't match either |

**Add a primary key.** It's a one-line change in your schema and it
turns every one of these from "best effort with caveats" into "exact
3-way merge semantics".
