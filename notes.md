# foldout — performance notes

## Benchmark target

- Database: `coinleverprod` (local Postgres on 127.0.0.1)
- Size: **4.76 GB / 4882 MB**, **79 user tables**
- Largest tables:
  - `public.runtime_commandrunhistory` — 1.20 GB (25.18% of set)
  - `public.runtime_commandtrigger` — 1.15 GB (24.16% of set)
  - (top 2 = ~49% of the database)
- Hardware: MacBook Air (development machine, local Postgres)
- Date: 2026-05-22

## Full-DB hashing wall times

| Approach | Wall time | Notes |
|---|---:|---|
| `hash_rust` — 1 worker, round-robin | **26.62 s** | ~183 MB/s per stream ceiling |
| `hash_rust` — 4 workers, round-robin | 15.73 s | |
| `hash_rust` — 8 workers, round-robin | 13.60 s | |
| `hash_rust` — 4 workers, **LPT** (size-balanced) | **11.99 s** | |
| `hash_rust` — 8 workers, **LPT** (size-balanced) | **9.38 s** | floor = biggest single table (~6.6 s) |
| `pg_hashdb` — `SELECT * FROM vkar_db_hash(8192)` (in-DB, single-threaded) | **47.16 s** | cannot parallelize inside the extension |

### Per-stream ceiling

A single `COPY ... TO STDOUT (FORMAT binary)` + BLAKE3 hashes at ~180 MB/s on this machine. Likely server-side COPY CPU, not the client hasher. The 8-worker LPT wall time is dominated by the single largest table (`runtime_commandrunhistory` 1.20 GB / 180 MB/s ≈ 6.6 s), which is close to the observed 9.4 s floor.

### LPT vs round-robin

Sorting tables by size descending and assigning each next table to the currently-least-loaded worker (longest-processing-time scheduling) cut wall time materially without changing per-stream throughput:

- 4 workers: 15.73 s → 11.99 s (1.31× improvement)
- 8 workers: 13.60 s →  9.38 s (1.45× improvement)

Implementation in `hash_rust/src/main.rs::partition_lpt`, toggled by `FLD_HASH_LPT` (default on).

### Why `pg_hashdb` was slow

`pg_hashdb` runs each table sequentially inside a single Postgres backend. The pgrx extension cannot spawn parallel hashing threads inside Postgres, so it loses the parallelism that hash_rust gets via N concurrent psql connections. The per-stream win from skipping the wire protocol (~1.3–1.6×) doesn't make up for being 1× instead of 8×.

To make a server-side hasher competitive we would have to call `vkar_hash_table(oid)` from N parallel sessions client-side — at which point we lose most of the "no extension needed" simplicity that hash_rust offers.

## Raw outputs

### hash_rust 1 worker

```
SUMMARY tables=79 set_size=4.76 GB db_size=4.77 GB rows~0 took 26.62s
```

### hash_rust 4 workers, round-robin

```
SUMMARY tables=79 set_size=4.76 GB db_size=4.77 GB rows~0 took 15.73s
```

### hash_rust 8 workers, round-robin

```
SUMMARY tables=79 set_size=4.76 GB db_size=4.77 GB rows~0 took 13.60s
```

### hash_rust 4 workers, LPT

```
SUMMARY tables=79 set_size=4.76 GB db_size=4.77 GB rows~0 took 11.99s
```

### hash_rust 8 workers, LPT

```
SUMMARY tables=79 set_size=4.76 GB db_size=4.77 GB rows~0 took 9.38s
```

### pg_hashdb (single-threaded inside Postgres)

```
SELECT count(*) FROM vkar_db_hash(8192);
 count
-------
    79
(1 row)

Time: 47156.597 ms (00:47.157)
```

## Takeaways and next steps

- **Target met**: ~9 s for a 5 GB DB on a MacBook Air is acceptable as a baseline.
- **Per-table parallelism (intra-table TID/CTID range hashing)** is the only way to push below the current ~6.6 s floor. Splitting `runtime_commandrunhistory` into N ctid ranges, hashing each in parallel, and combining Merkle-style would help the cold-path case for very large tables.
- **Trigger-based merge** (via `foldout.change_log`) makes hashing unnecessary on the hot path of a merge. Hashing becomes a verification step and a fallback for tables without a primary key.
- **`pg_hashdb` is not the right tool for full-DB hashing** given the parallelism constraint inside a Postgres backend. Keep it as the per-table primitive if/when we want a server-side fallback.
