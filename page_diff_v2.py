"""
Page-LSN database diff.

Workflow:
  page_diff_v2.py snapshot <pgdata> <dbname> <out.json>
      Records (relfilenode, segments[(path, size, mtime_ns)]) per relation,
      plus pg_current_wal_lsn() at snapshot time.

  page_diff_v2.py diff <pgdata> <dbname> <snap.json>
      Phase 1: stat-skip relations whose files are byte-identical; for the
               rest, mmap segments and find pages with pd_lsn > snap_lsn.
      Phase 2: parse line pointers on changed pages, classify each tuple
               slot (live / dead-since-snapshot / unchanged), fetch live
               tuple data via SELECT ... WHERE ctid = ANY (Postgres handles
               type and TOAST decoding for us).

Output is per-relation:
  - inserted / updated  : live tuples on changed pages with xmin > snap_xid
                           (we emit (ctid, row_data))
  - deleted             : line pointers that became dead since snapshot
                           (we emit (ctid, parsed_pk_columns))
"""

import json
import mmap
import os
import struct
import sys
import time

import psycopg

PAGE_SIZE = 8192
SEGMENT_PAGES = (1024 * 1024 * 1024) // PAGE_SIZE  # 131072

# Heap tuple header offsets (HeapTupleHeaderData)
HEAP_HASNULL = 0x0001

# Line pointer flags (ItemIdData lp_flags)
LP_UNUSED = 0
LP_NORMAL = 1
LP_REDIRECT = 2
LP_DEAD = 3


# ---------- LSN / segment helpers ----------

def parse_lsn_str(s):
    hi, lo = s.split("/")
    return (int(hi, 16) << 32) | int(lo, 16)


def format_lsn(n):
    return f"{n >> 32:X}/{n & 0xFFFFFFFF:08X}"


def segment_paths(pgdata, relpath):
    base = os.path.join(pgdata, relpath)
    if not os.path.exists(base):
        return
    yield 0, base
    seg = 1
    while True:
        p = f"{base}.{seg}"
        if not os.path.exists(p):
            return
        yield seg, p
        seg += 1


# ---------- catalog ----------

def list_relations(conn):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT n.nspname, c.relname, c.oid, c.relfilenode,
                   pg_relation_filepath(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','m','t')
              AND n.nspname NOT IN ('pg_catalog','information_schema')
              AND pg_relation_filepath(c.oid) IS NOT NULL
            ORDER BY n.nspname, c.relname
        """)
        return cur.fetchall()


def get_pk_columns(conn, oid):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT a.attname, a.attnum
            FROM pg_index i
            JOIN pg_attribute a ON a.attrelid = i.indrelid
                              AND a.attnum = ANY(i.indkey)
            WHERE i.indisprimary AND i.indrelid = %s
            ORDER BY array_position(i.indkey, a.attnum)
        """, (oid,))
        return cur.fetchall()


# ---------- snapshot ----------

def stat_segments(pgdata, relpath):
    out = []
    for _seg_idx, path in segment_paths(pgdata, relpath):
        st = os.stat(path)
        out.append({
            "path": os.path.relpath(path, pgdata),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
        })
    return out


def snapshot(pgdata, dbname, out_path):
    dsn = f"host=127.0.0.1 dbname={dbname} user={os.environ.get('USER','aybarsb')}"
    t0 = time.perf_counter()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_current_wal_lsn(), txid_current_snapshot();")
            lsn, xid_snap = cur.fetchone()
        rels = list_relations(conn)
        snap = {
            "dbname": dbname,
            "lsn": lsn,
            "xid_snapshot": str(xid_snap),
            "relations": [],
        }
        for nsp, rel, oid, relfilenode, relpath in rels:
            snap["relations"].append({
                "schema": nsp, "name": rel, "oid": oid,
                "relfilenode": relfilenode, "relpath": relpath,
                "segments": stat_segments(pgdata, relpath),
            })
    with open(out_path, "w") as f:
        json.dump(snap, f, indent=2)
    dt = (time.perf_counter() - t0) * 1000
    print(f"snapshot: lsn={lsn}  relations={len(snap['relations'])}  {dt:.0f} ms  -> {out_path}")


# ---------- Phase 1 ----------

def scan_segment_for_changed_blocks(pgdata, segpath, snap_lsn, block_offset):
    full = os.path.join(pgdata, segpath)
    size = os.path.getsize(full)
    if size == 0:
        return []
    blocks = []
    with open(full, "rb") as f:
        mm = mmap.mmap(f.fileno(), size, prot=mmap.PROT_READ)
        try:
            npages = size // PAGE_SIZE
            for i in range(npages):
                xlogid, xrecoff = struct.unpack_from("<II", mm, i * PAGE_SIZE)
                lsn = (xlogid << 32) | xrecoff
                if lsn > snap_lsn:
                    blocks.append((block_offset + i, lsn))
        finally:
            mm.close()
    return blocks


def find_changed_blocks_per_relation(pgdata, current_relpath, prev_segments, snap_lsn):
    prev_by_path = {s["path"]: s for s in prev_segments}
    changed_blocks = []
    scanned_bytes = 0
    skipped_files = 0
    scanned_files = 0
    for seg_idx, seg_full in segment_paths(pgdata, current_relpath):
        rel_seg_path = os.path.relpath(seg_full, pgdata)
        try:
            st = os.stat(seg_full)
        except FileNotFoundError:
            continue
        prev = prev_by_path.get(rel_seg_path)
        if (prev is not None
                and st.st_size == prev["size"]
                and st.st_mtime_ns == prev["mtime_ns"]):
            skipped_files += 1
            continue
        scanned_files += 1
        scanned_bytes += st.st_size
        changed_blocks.extend(
            scan_segment_for_changed_blocks(pgdata, rel_seg_path, snap_lsn,
                                            seg_idx * SEGMENT_PAGES)
        )
    return changed_blocks, scanned_bytes, scanned_files, skipped_files


# ---------- Phase 2: parse page tuple slots ----------

def parse_page_slots(page_bytes, snap_xmin):
    """
    Parse line pointers and tuple headers on a single 8 KB page.

    Returns:
      live_offsets   : list of line-pointer-index (1-based) for LP_NORMAL
                       tuples currently visible
      dead_or_old    : list of (lp_index_1based, lp_off, lp_len, t_xmin,
                                t_xmax, t_infomask, t_hoff, lp_flags)
                       for line pointers that EXISTED at snapshot time but
                       were modified afterwards (lp marked dead, or tuple
                       has xmax > snap_xmin, etc.)

    snap_xmin is the highest committed xid at snapshot time (oldest unused
    xid - 1, conservatively). Used to decide if an xmax/xmin happened
    "after" the snapshot.
    """
    pd_lower = struct.unpack_from("<H", page_bytes, 12)[0]
    n_lp = (pd_lower - 24) // 4
    live = []
    dead = []
    for i in range(n_lp):
        lp_off = 24 + i * 4
        word = struct.unpack_from("<I", page_bytes, lp_off)[0]
        off = word & 0x7FFF
        flags = (word >> 15) & 0x3
        ln = (word >> 17) & 0x7FFF
        if flags == LP_NORMAL and ln > 0:
            # tuple header: t_xmin (4), t_xmax (4), t_field3 (4),
            # t_ctid (6), t_infomask2 (2), t_infomask (2), t_hoff (1)
            if off + 23 > len(page_bytes):
                continue
            t_xmin = struct.unpack_from("<I", page_bytes, off)[0]
            t_xmax = struct.unpack_from("<I", page_bytes, off + 4)[0]
            t_infomask2 = struct.unpack_from("<H", page_bytes, off + 18)[0]
            t_infomask = struct.unpack_from("<H", page_bytes, off + 20)[0]
            t_hoff = page_bytes[off + 22]
            live.append((i + 1, off, ln, t_xmin, t_xmax, t_infomask, t_hoff))
            if t_xmax != 0 and t_xmax > snap_xmin:
                # was deleted/updated after the snapshot — record it as a
                # post-snapshot removal of the row that lived here
                dead.append((i + 1, off, ln, t_xmin, t_xmax, t_infomask, t_hoff, flags))
        elif flags == LP_DEAD:
            dead.append((i + 1, 0, 0, 0, 0, 0, 0, flags))
        # LP_REDIRECT (HOT chain) and LP_UNUSED: skip for v1
    return live, dead


def read_page(pgdata, relpath, block_number):
    seg_idx = block_number // SEGMENT_PAGES
    seg_block = block_number % SEGMENT_PAGES
    seg_path = os.path.join(pgdata, relpath if seg_idx == 0 else f"{relpath}.{seg_idx}")
    with open(seg_path, "rb") as f:
        f.seek(seg_block * PAGE_SIZE)
        return f.read(PAGE_SIZE)


# ---------- diff driver ----------

def fetch_live_rows(conn, schema, table, ctids):
    if not ctids:
        return []
    with conn.cursor() as cur:
        ctid_literals = [f"({b},{o})" for (b, o) in ctids]
        cur.execute(
            f'SELECT ctid::text, * FROM "{schema}"."{table}" '
            f'WHERE ctid = ANY(%s::tid[])',
            (ctid_literals,),
        )
        cols = [d.name for d in cur.description]
        return cols, cur.fetchall()


def diff(pgdata, dbname, snap_path, *, verbose=False):
    with open(snap_path) as f:
        snap = json.load(f)
    snap_lsn = parse_lsn_str(snap["lsn"])

    # Extract a conservative "snapshot xmin": parse txid_current_snapshot()
    # output "xmin:xmax:xips". xmin = lowest still-running xid at snapshot.
    # Any tuple with xmin >= snap_xmin is post-snapshot.
    snap_xid_str = snap["xid_snapshot"]
    snap_xmin = int(snap_xid_str.split(":")[0])

    snap_by_oid = {r["oid"]: r for r in snap["relations"]}

    dsn = f"host=127.0.0.1 dbname={dbname} user={os.environ.get('USER','aybarsb')}"
    t0 = time.perf_counter()
    with psycopg.connect(dsn) as conn:
        current = list_relations(conn)

        total_inserted = 0
        total_updated_or_seen = 0
        total_deleted = 0
        total_skipped_files = 0
        total_scanned_files = 0
        total_scanned_bytes = 0
        relations_with_changes = 0

        for nsp, rel, oid, current_relfilenode, current_relpath in current:
            prev = snap_by_oid.get(oid)
            if prev is None:
                if verbose:
                    print(f"  [new relation] {nsp}.{rel}")
                continue
            if current_relfilenode != prev["relfilenode"]:
                print(f"  {nsp}.{rel}: REWRITTEN "
                      f"(relfilenode {prev['relfilenode']} -> {current_relfilenode})")
                relations_with_changes += 1
                continue

            blocks, scanned_bytes, scanned_files, skipped_files = (
                find_changed_blocks_per_relation(
                    pgdata, current_relpath, prev["segments"], snap_lsn))
            total_skipped_files += skipped_files
            total_scanned_files += scanned_files
            total_scanned_bytes += scanned_bytes
            if not blocks:
                continue

            # Phase 2: parse line pointers on each changed page
            inserts = []     # ctid tuples for live rows with xmin > snap
            seen = []        # ctid tuples for any other live row on changed page
            deletes = []     # (ctid, t_xmin, t_xmax) for tuples that died
            for block, page_lsn in blocks:
                page = read_page(pgdata, current_relpath, block)
                live, dead = parse_page_slots(page, snap_xmin)
                for (lp_idx, off, ln, t_xmin, t_xmax, infomask, hoff) in live:
                    ct = (block, lp_idx)
                    if t_xmin >= snap_xmin:
                        inserts.append(ct)
                    else:
                        seen.append(ct)
                for entry in dead:
                    lp_idx = entry[0]
                    flags = entry[7] if len(entry) > 7 else 0
                    deletes.append(((block, lp_idx), flags))

            # fetch live tuple data via Postgres
            cols = None
            insert_rows = []
            if inserts:
                cols, insert_rows = fetch_live_rows(conn, nsp, rel, inserts)
            seen_rows = []
            if seen:
                _cols, seen_rows = fetch_live_rows(conn, nsp, rel, seen)

            relations_with_changes += 1
            total_inserted += len(insert_rows)
            total_updated_or_seen += len(seen_rows)
            total_deleted += len(deletes)
            print(f"  {nsp}.{rel}: inserts/updated={len(insert_rows)}  "
                  f"seen-on-changed-page={len(seen_rows)}  "
                  f"dead-slots={len(deletes)}")
            if verbose:
                for r in insert_rows[:5]:
                    print(f"    + {r}")
                for r in seen_rows[:5]:
                    print(f"    . {r}")
                for ct, flags in deletes[:5]:
                    print(f"    - ctid={ct} lp_flags={flags}")

    dt = (time.perf_counter() - t0) * 1000
    print()
    print(f"diff complete in {dt:.0f} ms")
    print(f"  files skipped via stat:   {total_skipped_files}")
    print(f"  files scanned:            {total_scanned_files}  "
          f"({total_scanned_bytes/1024/1024:.1f} MB)")
    print(f"  relations with changes:   {relations_with_changes}")
    print(f"  live tuples (xmin>=snap): {total_inserted}")
    print(f"  live tuples (xmin<snap):  {total_updated_or_seen}")
    print(f"  dead slots (post-snap):   {total_deleted}")


# ---------- cross-DB diff (source vs current) ----------

def fetch_rows_for_ctids(conn, schema, table, ctids):
    if not ctids:
        return [], []
    with conn.cursor() as cur:
        ctid_literals = [f"({b},{o})" for (b, o) in ctids]
        cur.execute(
            f'SELECT ctid::text, * FROM "{schema}"."{table}" '
            f'WHERE ctid = ANY(%s::tid[])',
            (ctid_literals,),
        )
        cols = [d.name for d in cur.description]
        return cols, cur.fetchall()


def live_ctids_for_block(pgdata, relpath, block):
    """Read one page and return ctids (block, lp_index_1based) for LP_NORMAL slots."""
    seg_idx = block // SEGMENT_PAGES
    seg_block = block % SEGMENT_PAGES
    seg_path = os.path.join(pgdata, relpath if seg_idx == 0 else f"{relpath}.{seg_idx}")
    if not os.path.exists(seg_path):
        return []
    try:
        with open(seg_path, "rb") as f:
            f.seek(seg_block * PAGE_SIZE)
            page = f.read(PAGE_SIZE)
    except OSError:
        return []
    if len(page) < 24:
        return []
    pd_lower = struct.unpack_from("<H", page, 12)[0]
    n_lp = (pd_lower - 24) // 4 if pd_lower >= 24 else 0
    out = []
    for i in range(n_lp):
        word = struct.unpack_from("<I", page, 24 + i * 4)[0]
        flags = (word >> 15) & 0x3
        ln = (word >> 17) & 0x7FFF
        if flags == LP_NORMAL and ln > 0:
            out.append((block, i + 1))
    return out


def cross_diff(pgdata, source_db, current_db, snap_path, *, verbose=False):
    with open(snap_path) as f:
        snap = json.load(f)
    snap_lsn = parse_lsn_str(snap["lsn"])
    snap_by_oid = {r["oid"]: r for r in snap["relations"]}

    def dsn(db):
        return f"host=127.0.0.1 dbname={db} user={os.environ.get('USER','aybarsb')}"

    t0 = time.perf_counter()
    totals = {"INSERT": 0, "UPDATE": 0, "DELETE": 0,
              "scanned_files": 0, "scanned_bytes": 0, "skipped_files": 0,
              "rels_with_changes": 0, "rels_no_pk_skipped": 0}
    sql_out = []

    with psycopg.connect(dsn(current_db)) as cur_conn, \
         psycopg.connect(dsn(source_db)) as src_conn:

        cur_rels = list_relations(cur_conn)
        src_rels = list_relations(src_conn)
        src_relpath_by_name = {(r[0], r[1]): r[4] for r in src_rels}

        for nsp, rel, oid, current_relfilenode, current_relpath in cur_rels:
            prev = snap_by_oid.get(oid)
            if prev is None:
                continue
            if current_relfilenode != prev["relfilenode"]:
                print(f"  {nsp}.{rel}: REWRITTEN")
                totals["rels_with_changes"] += 1
                continue

            blocks, scanned_bytes, scanned_files, skipped_files = (
                find_changed_blocks_per_relation(
                    pgdata, current_relpath, prev["segments"], snap_lsn))
            totals["scanned_bytes"] += scanned_bytes
            totals["scanned_files"] += scanned_files
            totals["skipped_files"] += skipped_files
            if not blocks:
                continue

            src_relpath = src_relpath_by_name.get((nsp, rel))
            if src_relpath is None:
                print(f"  {nsp}.{rel}: not present in source")
                continue

            pk_cols = get_pk_columns(cur_conn, oid)
            if not pk_cols:
                print(f"  {nsp}.{rel}: no primary key, skipping (need PK for v1)")
                totals["rels_no_pk_skipped"] += 1
                continue
            pk_names = [c[0] for c in pk_cols]

            cur_ctids = []
            src_ctids = []
            for block, _lsn in blocks:
                cur_ctids.extend(live_ctids_for_block(pgdata, current_relpath, block))
                src_ctids.extend(live_ctids_for_block(pgdata, src_relpath, block))

            cur_cols, cur_rows = fetch_rows_for_ctids(cur_conn, nsp, rel, cur_ctids)
            src_cols, src_rows = fetch_rows_for_ctids(src_conn, nsp, rel, src_ctids)

            # column 0 is ctid; data columns start at 1
            def build_pk_map(cols, rows):
                if not cols:
                    return {}
                pk_idx = [cols.index(p) for p in pk_names]
                out = {}
                for r in rows:
                    pk = tuple(r[i] for i in pk_idx)
                    out[pk] = r[1:]   # row tuple without ctid
                return out, [c for c in cols[1:]]

            cur_map_pair = build_pk_map(cur_cols, cur_rows) if cur_cols else ({}, [])
            src_map_pair = build_pk_map(src_cols, src_rows) if src_cols else ({}, [])
            cur_map, data_cols_cur = cur_map_pair
            src_map, data_cols_src = src_map_pair

            cur_pks = set(cur_map)
            src_pks = set(src_map)
            inserts = sorted(cur_pks - src_pks)
            deletes = sorted(src_pks - cur_pks)
            common = cur_pks & src_pks
            updates = sorted(pk for pk in common if cur_map[pk] != src_map[pk])

            if not (inserts or deletes or updates):
                continue

            totals["rels_with_changes"] += 1
            totals["INSERT"] += len(inserts)
            totals["UPDATE"] += len(updates)
            totals["DELETE"] += len(deletes)
            print(f"  {nsp}.{rel}: INSERT={len(inserts)} "
                  f"UPDATE={len(updates)} DELETE={len(deletes)}")

            data_cols = data_cols_cur or data_cols_src
            qn = f'"{nsp}"."{rel}"'

            for pk in inserts:
                row = cur_map[pk]
                cols_s = ", ".join(f'"{c}"' for c in data_cols)
                vals_s = ", ".join(_sql_literal(v) for v in row)
                sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")
                if verbose:
                    print(f"    + {dict(zip(data_cols, row))}")
            for pk in updates:
                new = cur_map[pk]
                old = src_map[pk]
                sets = []
                for c, n_val, o_val in zip(data_cols, new, old):
                    if c in pk_names:
                        continue
                    if n_val != o_val:
                        sets.append(f'"{c}"={_sql_literal(n_val)}')
                if not sets:
                    continue
                where = " AND ".join(
                    f'"{c}"={_sql_literal(v)}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(f"UPDATE {qn} SET {', '.join(sets)} WHERE {where};")
                if verbose:
                    print(f"    ~ pk={pk} old={dict(zip(data_cols, old))} -> new={dict(zip(data_cols, new))}")
            for pk in deletes:
                where = " AND ".join(
                    f'"{c}"={_sql_literal(v)}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(f"DELETE FROM {qn} WHERE {where};")
                if verbose:
                    old = src_map[pk]
                    print(f"    - pk={pk} row={dict(zip(data_cols, old))}")

    dt = (time.perf_counter() - t0) * 1000
    totals["elapsed_ms"] = dt
    totals["sql"] = sql_out
    if verbose or __name__ == "__main__":
        print()
        print(f"cross-diff complete in {dt:.0f} ms")
        print(f"  files skipped via stat:  {totals['skipped_files']}")
        print(f"  files scanned:           {totals['scanned_files']}  "
              f"({totals['scanned_bytes']/1024/1024:.1f} MB)")
        print(f"  relations with changes:  {totals['rels_with_changes']}")
        print(f"  INSERT={totals['INSERT']}  UPDATE={totals['UPDATE']}  DELETE={totals['DELETE']}")
        if totals["rels_no_pk_skipped"]:
            print(f"  relations skipped (no PK): {totals['rels_no_pk_skipped']}")
        if sql_out:
            print("\n-- SQL diff --")
            for s in sql_out:
                print(s)
    return totals


def _sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v).replace("'", "''")
    return f"'{s}'"


# ---------- main ----------

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("snapshot", "diff", "cross-diff"):
        print("usage:")
        print("  page_diff_v2.py snapshot   <pgdata> <dbname> <out.json>")
        print("  page_diff_v2.py diff       <pgdata> <dbname> <snap.json> [-v]")
        print("  page_diff_v2.py cross-diff <pgdata> <source_db> <current_db> <snap.json> [-v]")
        sys.exit(2)
    cmd = sys.argv[1]
    verbose = "-v" in sys.argv
    if cmd == "snapshot":
        snapshot(sys.argv[2], sys.argv[3], sys.argv[4])
    elif cmd == "diff":
        diff(sys.argv[2], sys.argv[3], sys.argv[4], verbose=verbose)
    else:
        cross_diff(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5], verbose=verbose)


if __name__ == "__main__":
    main()
