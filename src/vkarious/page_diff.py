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
from collections import Counter

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

EXCLUDED_SCHEMAS = ("pg_catalog", "information_schema", "vkarious")
EXCLUDED_SCHEMAS_SQL = "(" + ",".join(f"'{s}'" for s in EXCLUDED_SCHEMAS) + ")"


# ---------------- Schema (DDL) diff ----------------

def dump_schema(conn):
    """Snapshot the relevant catalog state of a database for DDL diffing.

    Captures: schemas, tables (with columns + PK), non-PK indexes,
    non-PK constraints (FK/UNIQUE/CHECK), views, materialized views,
    user-defined functions.
    """
    out = {
        "schemas": [],
        "tables": {},        # 'nsp.name' -> {columns, primary_key, ...}
        "indexes": {},       # 'nsp.idxname' -> {table, definition}
        "constraints": {},   # 'nsp.tab.conname' -> {type, definition}
        "views": {},         # 'nsp.name' -> definition (SELECT body)
        "matviews": {},      # 'nsp.name' -> definition
        "functions": {},     # 'nsp.name(args)' -> full CREATE FUNCTION text
        "sequences": {},     # 'nsp.name' -> seq params + last_value + owned_by
    }

    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT nspname FROM pg_namespace
            WHERE nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
            ORDER BY nspname
        """)
        out["schemas"] = [r[0] for r in cur.fetchall()]

        cur.execute(f"""
            SELECT n.nspname, c.relname
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','p')
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
            ORDER BY n.nspname, c.relname
        """)
        for nsp, rel in cur.fetchall():
            out["tables"][f"{nsp}.{rel}"] = {
                "schema": nsp, "name": rel,
                "columns": [], "primary_key": None,
            }

        cur.execute(f"""
            SELECT n.nspname, c.relname, a.attname, a.attnum,
                   format_type(a.atttypid, a.atttypmod) AS data_type,
                   a.attnotnull,
                   pg_get_expr(ad.adbin, ad.adrelid) AS default_expr,
                   a.attidentity
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attrdef ad ON ad.adrelid = c.oid AND ad.adnum = a.attnum
            WHERE a.attnum > 0 AND NOT a.attisdropped
              AND c.relkind IN ('r','p')
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
            ORDER BY n.nspname, c.relname, a.attnum
        """)
        for nsp, rel, attname, attnum, dtype, notnull, default_expr, identity in cur.fetchall():
            key = f"{nsp}.{rel}"
            if key in out["tables"]:
                out["tables"][key]["columns"].append({
                    "name": attname,
                    "type": dtype,
                    "not_null": bool(notnull),
                    "default": default_expr,
                    "identity": identity if identity else None,
                })

        cur.execute(f"""
            SELECT n.nspname, c.relname,
                   array_agg(a.attname ORDER BY array_position(i.indkey::int[], a.attnum::int))
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey)
            WHERE i.indisprimary
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
            GROUP BY n.nspname, c.relname
        """)
        for nsp, rel, pkcols in cur.fetchall():
            key = f"{nsp}.{rel}"
            if key in out["tables"]:
                out["tables"][key]["primary_key"] = list(pkcols)

        # Non-PK indexes; exclude those that back UNIQUE constraints (we'll emit
        # those via ADD CONSTRAINT instead).
        cur.execute(f"""
            SELECT n.nspname, ic.relname AS idx_name, tc.relname AS tab_name,
                   pg_get_indexdef(i.indexrelid)
            FROM pg_index i
            JOIN pg_class ic ON ic.oid = i.indexrelid
            JOIN pg_class tc ON tc.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = ic.relnamespace
            WHERE NOT i.indisprimary
              AND NOT EXISTS (
                SELECT 1 FROM pg_constraint con
                WHERE con.conindid = i.indexrelid AND con.contype IN ('u','x')
              )
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
        """)
        for nsp, idxname, tabname, ixdef in cur.fetchall():
            out["indexes"][f"{nsp}.{idxname}"] = {
                "schema": nsp, "name": idxname,
                "table": tabname, "definition": ixdef,
            }

        # FK, CHECK, UNIQUE, EXCLUDE constraints
        cur.execute(f"""
            SELECT n.nspname, c.relname, con.conname, con.contype,
                   pg_get_constraintdef(con.oid, true)
            FROM pg_constraint con
            JOIN pg_class c ON c.oid = con.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE con.contype IN ('f','c','u','x')
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
        """)
        for nsp, rel, conname, contype, condef in cur.fetchall():
            out["constraints"][f"{nsp}.{rel}.{conname}"] = {
                "schema": nsp, "table": rel, "name": conname,
                "type": contype, "definition": condef,
            }

        cur.execute(f"""
            SELECT n.nspname, c.relname, c.relkind, pg_get_viewdef(c.oid, true)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('v','m')
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
        """)
        for nsp, name, kind, vdef in cur.fetchall():
            bucket = "views" if kind == 'v' else "matviews"
            out[bucket][f"{nsp}.{name}"] = {
                "schema": nsp, "name": name, "definition": vdef.rstrip(";\n "),
            }

        # User functions/procedures (exclude extension-owned)
        cur.execute(f"""
            SELECT n.nspname, p.proname,
                   pg_get_function_identity_arguments(p.oid) AS args,
                   pg_get_functiondef(p.oid)
            FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND NOT EXISTS (
                SELECT 1 FROM pg_depend d
                WHERE d.objid = p.oid AND d.deptype = 'e'
              )
        """)
        for nsp, name, args, fdef in cur.fetchall():
            key = f"{nsp}.{name}({args})"
            out["functions"][key] = {
                "schema": nsp, "name": name, "args": args,
                "definition": fdef.rstrip(),
            }

        # Sequences (incl. those auto-created for SERIAL columns).
        # pg_sequence_last_value returns NULL if the sequence has never
        # been called yet. We record the OWNED BY target (schema.tbl.col)
        # so we can emit `ALTER SEQUENCE ... OWNED BY ...` after the table
        # is created.
        cur.execute(f"""
            SELECT n.nspname, c.relname,
                   format_type(s.seqtypid, NULL) AS data_type,
                   s.seqstart, s.seqincrement, s.seqmin, s.seqmax,
                   s.seqcache, s.seqcycle,
                   pg_sequence_last_value(c.oid::regclass) AS last_value,
                   (
                     SELECT n2.nspname || '.' || c2.relname || '.' || a.attname
                     FROM pg_depend d
                     JOIN pg_class c2 ON c2.oid = d.refobjid
                     JOIN pg_namespace n2 ON n2.oid = c2.relnamespace
                     JOIN pg_attribute a ON a.attrelid = d.refobjid
                                        AND a.attnum = d.refobjsubid
                     WHERE d.classid = 'pg_class'::regclass
                       AND d.refclassid = 'pg_class'::regclass
                       AND d.objid = c.oid
                       AND d.deptype IN ('a','i')
                     LIMIT 1
                   ) AS owned_by
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_sequence s ON s.seqrelid = c.oid
            WHERE c.relkind = 'S'
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS_SQL}
              AND n.nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
        """)
        for (nsp, name, dtype, start, inc, mn, mx,
             cache, cycle, last_value, owned_by) in cur.fetchall():
            out["sequences"][f"{nsp}.{name}"] = {
                "schema": nsp, "name": name,
                "data_type": dtype,
                "start": int(start), "increment": int(inc),
                "min": int(mn), "max": int(mx),
                "cache": int(cache), "cycle": bool(cycle),
                "last_value": (int(last_value) if last_value is not None else None),
                "owned_by": owned_by,
            }

    return out


def _q(s):
    return '"' + s.replace('"', '""') + '"'


def _render_column(c):
    parts = [f'{_q(c["name"])} {c["type"]}']
    if c.get("identity"):
        kind = "ALWAYS" if c["identity"] == "a" else "BY DEFAULT"
        parts.append(f"GENERATED {kind} AS IDENTITY")
    elif c.get("default") is not None:
        parts.append(f'DEFAULT {c["default"]}')
    if c.get("not_null"):
        parts.append("NOT NULL")
    return " ".join(parts)


def _render_create_table(t):
    cols = [_render_column(c) for c in t["columns"]]
    if t.get("primary_key"):
        pk = ", ".join(_q(c) for c in t["primary_key"])
        cols.append(f"PRIMARY KEY ({pk})")
    body = ",\n  ".join(cols)
    return f'CREATE TABLE {_q(t["schema"])}.{_q(t["name"])} (\n  {body}\n);'


def diff_schemas(src, tgt):
    """Return ordered DDL statements that make `src` look like `tgt`.

    Order (so dependencies resolve):
      1. CREATE SCHEMA
      2. CREATE TABLE (new)
      3. ALTER TABLE ADD/DROP/ALTER COLUMN; PK changes
      4. CREATE INDEX (new)
      5. ALTER TABLE ADD CONSTRAINT (FK/CHECK/UNIQUE) (new)
      6. CREATE OR REPLACE VIEW (new/changed views; DROP+CREATE on diff)
      7. CREATE OR REPLACE FUNCTION (new/changed)
      ... DML happens here ...
      8. DROP FUNCTION (removed)
      9. DROP VIEW (removed)
     10. ALTER TABLE DROP CONSTRAINT (removed)
     11. DROP INDEX (removed)
     12. DROP TABLE (removed)
     13. DROP SCHEMA (removed)

    Returns (pre_dml, post_dml) tuple. Caller emits pre_dml before DML and
    post_dml after.
    """
    pre = []   # CREATE/ALTER — must run before DML
    post = []  # DROP — runs after DML

    src_schemas = set(src["schemas"])
    tgt_schemas = set(tgt["schemas"])
    for s in sorted(tgt_schemas - src_schemas):
        pre.append(f'CREATE SCHEMA {_q(s)};')

    # Sequences (CREATE before tables so DEFAULT nextval('seq') resolves)
    src_seqs = src.get("sequences", {})
    tgt_seqs = tgt.get("sequences", {})
    new_seq_keys = sorted(set(tgt_seqs) - set(src_seqs))
    dropped_seq_keys = sorted(set(src_seqs) - set(tgt_seqs))
    for k in new_seq_keys:
        s = tgt_seqs[k]
        pre.append(
            f'CREATE SEQUENCE {_q(s["schema"])}.{_q(s["name"])} '
            f'AS {s["data_type"]} '
            f'START WITH {s["start"]} INCREMENT BY {s["increment"]} '
            f'MINVALUE {s["min"]} MAXVALUE {s["max"]} '
            f'CACHE {s["cache"]}'
            + (' CYCLE' if s["cycle"] else '')
            + ';'
        )

    # Tables
    src_tabs = src["tables"]
    tgt_tabs = tgt["tables"]
    new_tabs = sorted(set(tgt_tabs) - set(src_tabs))
    dropped_tabs = sorted(set(src_tabs) - set(tgt_tabs))
    common_tabs = sorted(set(src_tabs) & set(tgt_tabs))

    for k in new_tabs:
        pre.append(_render_create_table(tgt_tabs[k]))

    for k in common_tabs:
        s = src_tabs[k]
        t = tgt_tabs[k]
        nsp, name = s["schema"], s["name"]
        qn = f"{_q(nsp)}.{_q(name)}"
        s_cols = {c["name"]: c for c in s["columns"]}
        t_cols = {c["name"]: c for c in t["columns"]}

        # ADD COLUMNs (preserve target order)
        for c in t["columns"]:
            if c["name"] not in s_cols:
                pre.append(f'ALTER TABLE {qn} ADD COLUMN {_render_column(c)};')

        # ALTER COLUMNs (type / default / not null)
        for cname in [c["name"] for c in t["columns"]]:
            if cname not in s_cols:
                continue
            sc = s_cols[cname]
            tc = t_cols[cname]
            qc = _q(cname)
            if sc["type"] != tc["type"]:
                pre.append(
                    f'ALTER TABLE {qn} ALTER COLUMN {qc} TYPE {tc["type"]} '
                    f'USING {qc}::{tc["type"]};'
                )
            if (sc.get("default") or None) != (tc.get("default") or None):
                if tc.get("default") is None:
                    pre.append(f'ALTER TABLE {qn} ALTER COLUMN {qc} DROP DEFAULT;')
                else:
                    pre.append(
                        f'ALTER TABLE {qn} ALTER COLUMN {qc} '
                        f'SET DEFAULT {tc["default"]};'
                    )
            if bool(sc.get("not_null")) != bool(tc.get("not_null")):
                action = "SET" if tc.get("not_null") else "DROP"
                pre.append(f'ALTER TABLE {qn} ALTER COLUMN {qc} {action} NOT NULL;')

        # DROP COLUMNs that were removed
        for cname in s_cols:
            if cname not in t_cols:
                pre.append(f'ALTER TABLE {qn} DROP COLUMN {_q(cname)};')

        # Primary key changes
        if (s.get("primary_key") or None) != (t.get("primary_key") or None):
            if s.get("primary_key"):
                pre.append(f'ALTER TABLE {qn} DROP CONSTRAINT {_q(name + "_pkey")};')
            if t.get("primary_key"):
                pk = ", ".join(_q(c) for c in t["primary_key"])
                pre.append(f'ALTER TABLE {qn} ADD PRIMARY KEY ({pk});')

    # Indexes
    new_idx = sorted(set(tgt["indexes"]) - set(src["indexes"]))
    dropped_idx = sorted(set(src["indexes"]) - set(tgt["indexes"]))
    for k in new_idx:
        pre.append(f'{tgt["indexes"][k]["definition"]};')
    for k in dropped_idx:
        post.append(f'DROP INDEX {_q(src["indexes"][k]["schema"])}.{_q(src["indexes"][k]["name"])};')

    # Constraints (FK/CHECK/UNIQUE/EXCLUDE) — also handle definition changes
    src_con = src["constraints"]
    tgt_con = tgt["constraints"]
    for k in sorted(set(tgt_con) - set(src_con)):
        c = tgt_con[k]
        pre.append(
            f'ALTER TABLE {_q(c["schema"])}.{_q(c["table"])} '
            f'ADD CONSTRAINT {_q(c["name"])} {c["definition"]};'
        )
    for k in sorted(set(src_con) - set(tgt_con)):
        c = src_con[k]
        post.append(
            f'ALTER TABLE {_q(c["schema"])}.{_q(c["table"])} '
            f'DROP CONSTRAINT {_q(c["name"])};'
        )
    for k in sorted(set(src_con) & set(tgt_con)):
        if src_con[k]["definition"] != tgt_con[k]["definition"]:
            c = tgt_con[k]
            pre.append(
                f'ALTER TABLE {_q(c["schema"])}.{_q(c["table"])} '
                f'DROP CONSTRAINT {_q(c["name"])};'
            )
            pre.append(
                f'ALTER TABLE {_q(c["schema"])}.{_q(c["table"])} '
                f'ADD CONSTRAINT {_q(c["name"])} {c["definition"]};'
            )

    # Views (do DROP + CREATE on any diff for simplicity)
    for k in sorted(set(tgt["views"]) - set(src["views"])):
        v = tgt["views"][k]
        pre.append(f'CREATE VIEW {_q(v["schema"])}.{_q(v["name"])} AS\n{v["definition"]};')
    for k in sorted(set(src["views"]) - set(tgt["views"])):
        v = src["views"][k]
        post.append(f'DROP VIEW {_q(v["schema"])}.{_q(v["name"])};')
    for k in sorted(set(src["views"]) & set(tgt["views"])):
        if src["views"][k]["definition"] != tgt["views"][k]["definition"]:
            v = tgt["views"][k]
            pre.append(
                f'CREATE OR REPLACE VIEW {_q(v["schema"])}.{_q(v["name"])} AS\n'
                f'{v["definition"]};'
            )

    for k in sorted(set(tgt["matviews"]) - set(src["matviews"])):
        v = tgt["matviews"][k]
        pre.append(
            f'CREATE MATERIALIZED VIEW {_q(v["schema"])}.{_q(v["name"])} AS\n'
            f'{v["definition"]};'
        )
    for k in sorted(set(src["matviews"]) - set(tgt["matviews"])):
        v = src["matviews"][k]
        post.append(f'DROP MATERIALIZED VIEW {_q(v["schema"])}.{_q(v["name"])};')

    # Functions
    for k in sorted(set(tgt["functions"]) - set(src["functions"])):
        pre.append(tgt["functions"][k]["definition"] + ";")
    for k in sorted(set(src["functions"]) - set(tgt["functions"])):
        f = src["functions"][k]
        post.append(f'DROP FUNCTION {_q(f["schema"])}.{_q(f["name"])}({f["args"]});')
    for k in sorted(set(src["functions"]) & set(tgt["functions"])):
        if src["functions"][k]["definition"] != tgt["functions"][k]["definition"]:
            # CREATE OR REPLACE FUNCTION is included in pg_get_functiondef
            pre.append(tgt["functions"][k]["definition"] + ";")

    # Sequence ownership: attach new sequences to their column AFTER the
    # CREATE TABLE so the owning column exists.
    for k in new_seq_keys:
        s = tgt_seqs[k]
        if s.get("owned_by"):
            ob = s["owned_by"]  # 'nsp.tbl.col'
            try:
                nsp_o, rest = ob.split(".", 1)
                tbl_o, col_o = rest.split(".", 1)
            except ValueError:
                continue
            pre.append(
                f'ALTER SEQUENCE {_q(s["schema"])}.{_q(s["name"])} '
                f'OWNED BY {_q(nsp_o)}.{_q(tbl_o)}.{_q(col_o)};'
            )

    # Drop tables and schemas last
    for k in dropped_tabs:
        t = src_tabs[k]
        post.append(f'DROP TABLE {_q(t["schema"])}.{_q(t["name"])};')

    # Setval — align sequence position on source to match target. We do
    # this for every target sequence whose last_value differs from source's
    # (or which is new). DML INSERTs with explicit IDs do NOT advance the
    # sequence, so without this, source's seq would still be at its old
    # value and the next nextval() would collide with rows we just inserted.
    for k in sorted(tgt_seqs):
        s = tgt_seqs[k]
        prev = src_seqs.get(k)
        if (prev is not None
                and prev.get("last_value") == s.get("last_value")):
            continue
        if s.get("last_value") is None:
            # Sequence never called yet on target. setval(start_val, false)
            # so the next nextval() returns start.
            post.append(
                f"SELECT setval('{s['schema']}.{s['name']}', "
                f"{s['start']}, false);"
            )
        else:
            post.append(
                f"SELECT setval('{s['schema']}.{s['name']}', "
                f"{s['last_value']}, true);"
            )

    # Drop sequences that aren't owned by a dropped table (those auto-drop
    # via OWNED BY). For simplicity emit IF EXISTS so we tolerate the cascade.
    for k in dropped_seq_keys:
        s = src_seqs[k]
        post.append(
            f'DROP SEQUENCE IF EXISTS {_q(s["schema"])}.{_q(s["name"])};'
        )

    for s in sorted(src_schemas - tgt_schemas):
        post.append(f'DROP SCHEMA {_q(s)};')

    return pre, post


def _merge_table_3way(base_t, main_t, branch_t, table_key, conflicts, drifts=None):
    """3-way merge a single table dict (column-level + PK). Mutates `conflicts`
    when both sides changed the same thing differently; appends to `drifts`
    (if provided) for column-level changes parent made independently.
    Returns the merged table state, taking main as the starting point and
    applying branch's safe additions/modifications.
    """
    base_cols = {c["name"]: c for c in base_t["columns"]} if base_t else {}
    main_cols = {c["name"]: c for c in main_t["columns"]} if main_t else {}
    branch_cols = {c["name"]: c for c in branch_t["columns"]} if branch_t else {}
    all_col_names = set(base_cols) | set(main_cols) | set(branch_cols)

    merged_cols_by_name = dict(main_cols)
    for cn in all_col_names:
        bc = base_cols.get(cn)
        mc = main_cols.get(cn)
        rc = branch_cols.get(cn)
        branch_changed = (bc != rc)
        main_changed = (bc != mc)
        if branch_changed and main_changed:
            if rc == mc:
                continue  # same change applied both sides
            conflicts.append({
                "kind": "column", "key": f"{table_key}.{cn}",
                "base": bc, "main": mc, "branch": rc,
            })
        elif branch_changed and not main_changed:
            if rc is None:
                merged_cols_by_name.pop(cn, None)
            else:
                merged_cols_by_name[cn] = rc
        elif main_changed and not branch_changed:
            # Parent independently changed this column. Record as drift so
            # the user knows; merged keeps main's state (default behavior).
            if drifts is not None:
                drifts.append({
                    "kind": "column", "key": f"{table_key}.{cn}",
                    "base": bc, "main": mc,
                })

    # Order: keep main's original order, then append columns branch added that
    # weren't in base or main.
    merged_columns = [
        merged_cols_by_name[c["name"]]
        for c in (main_t["columns"] if main_t else [])
        if c["name"] in merged_cols_by_name
    ]
    seen = {c["name"] for c in merged_columns}
    for cn in branch_cols:
        if cn in seen:
            continue
        if cn not in main_cols and cn not in base_cols:
            merged_columns.append(branch_cols[cn])
            seen.add(cn)

    # PK 3-way
    base_pk = base_t.get("primary_key") if base_t else None
    main_pk = main_t.get("primary_key") if main_t else None
    branch_pk = branch_t.get("primary_key") if branch_t else None
    branch_pk_changed = base_pk != branch_pk
    main_pk_changed = base_pk != main_pk
    if branch_pk_changed and main_pk_changed and branch_pk != main_pk:
        conflicts.append({
            "kind": "primary_key", "key": table_key,
            "base": base_pk, "main": main_pk, "branch": branch_pk,
        })
        merged_pk = main_pk
    elif branch_pk_changed:
        merged_pk = branch_pk
    else:
        merged_pk = main_pk

    merged = dict(main_t) if main_t else dict(branch_t)
    merged["columns"] = merged_columns
    merged["primary_key"] = merged_pk
    return merged


def _three_way_dict_merge(base_d, main_d, branch_d, kind, conflicts):
    """3-way merge a `{key: state_dict}` mapping (indexes, constraints, views,
    matviews, functions, sequences). Object-level: if both sides changed the
    same key differently, record a conflict and keep main's state.
    """
    merged = dict(main_d)
    all_keys = set(base_d) | set(main_d) | set(branch_d)
    for k in all_keys:
        b = base_d.get(k)
        m = main_d.get(k)
        r = branch_d.get(k)
        branch_changed = (b != r)
        main_changed = (b != m)
        if branch_changed and main_changed:
            if r == m:
                continue
            conflicts.append({"kind": kind, "key": k, "base": b, "main": m, "branch": r})
        elif branch_changed and not main_changed:
            if r is None:
                merged.pop(k, None)
            else:
                merged[k] = r
    return merged


def merge_schemas_3way(base, main, branch):
    """Build merged_schema = main + branch's safe intent. Returns
    (merged_schema, conflicts, drifts) where:
      - merged_schema: target state to apply to main
      - conflicts: list of dicts describing what couldn't be merged
      - drifts: list of dicts describing what main changed independently
    """
    conflicts = []
    drifts = []

    # Schemas (namespaces)
    base_ns = set(base["schemas"])
    main_ns = set(main["schemas"])
    branch_ns = set(branch["schemas"])
    merged_ns = set(main_ns)
    for n in base_ns | main_ns | branch_ns:
        b = n in base_ns
        m = n in main_ns
        r = n in branch_ns
        if (b != r) and (b != m):
            if r == m:
                continue
            conflicts.append({"kind": "schema", "key": n, "base": b, "main": m, "branch": r})
        elif (b != r) and (b == m):
            if r:
                merged_ns.add(n)
            else:
                merged_ns.discard(n)
        elif (b != m) and (b == r):
            drifts.append({"kind": "schema", "key": n, "main": m, "base": b})

    # Tables — column-level 3-way for tables present in all three; object-level
    # for additions/deletions.
    base_t = base["tables"]; main_t = main["tables"]; branch_t = branch["tables"]
    merged_tables = dict(main_t)
    all_tab_keys = set(base_t) | set(main_t) | set(branch_t)
    for k in all_tab_keys:
        b = base_t.get(k); m = main_t.get(k); r = branch_t.get(k)
        branch_changed = (b != r)
        main_changed = (b != m)
        if not branch_changed and not main_changed:
            continue
        if branch_changed and not main_changed:
            if r is None:
                merged_tables.pop(k, None)
            elif b is None:
                merged_tables[k] = r
            else:
                # both have base AND branch; column-level merge against main
                merged_tables[k] = _merge_table_3way(b, m, r, k, conflicts, drifts)
        elif main_changed and not branch_changed:
            drifts.append({"kind": "table", "key": k, "main": m, "base": b})
        else:  # both changed
            if r == m:
                continue
            if b is not None and m is not None and r is not None:
                merged_tables[k] = _merge_table_3way(b, m, r, k, conflicts, drifts)
            else:
                conflicts.append({"kind": "table", "key": k, "base": b, "main": m, "branch": r})

    merged_indexes = _three_way_dict_merge(base["indexes"], main["indexes"], branch["indexes"], "index", conflicts)
    merged_constr  = _three_way_dict_merge(base["constraints"], main["constraints"], branch["constraints"], "constraint", conflicts)
    merged_views   = _three_way_dict_merge(base["views"], main["views"], branch["views"], "view", conflicts)
    merged_matv    = _three_way_dict_merge(base["matviews"], main["matviews"], branch["matviews"], "matview", conflicts)
    merged_funcs   = _three_way_dict_merge(base["functions"], main["functions"], branch["functions"], "function", conflicts)
    merged_seqs    = _three_way_dict_merge(base["sequences"], main["sequences"], branch["sequences"], "sequence", conflicts)

    # Track drifts in non-table object kinds too (informational)
    for kind, base_d, main_d, branch_d in [
        ("index", base["indexes"], main["indexes"], branch["indexes"]),
        ("constraint", base["constraints"], main["constraints"], branch["constraints"]),
        ("view", base["views"], main["views"], branch["views"]),
        ("matview", base["matviews"], main["matviews"], branch["matviews"]),
        ("function", base["functions"], main["functions"], branch["functions"]),
        ("sequence", base["sequences"], main["sequences"], branch["sequences"]),
    ]:
        for k in set(base_d) | set(main_d) | set(branch_d):
            if base_d.get(k) != main_d.get(k) and base_d.get(k) == branch_d.get(k):
                drifts.append({"kind": kind, "key": k, "main": main_d.get(k), "base": base_d.get(k)})

    merged = {
        "schemas": sorted(merged_ns),
        "tables": merged_tables,
        "indexes": merged_indexes,
        "constraints": merged_constr,
        "views": merged_views,
        "matviews": merged_matv,
        "functions": merged_funcs,
        "sequences": merged_seqs,
    }
    return merged, conflicts, drifts


def diff_schemas_3way(base, main, branch):
    """Return (pre_dml, post_dml, conflicts, drifts). The pre/post lists are
    SQL statements to make `main` look like the merged state (main + branch's
    intent, excluding conflicts).
    """
    merged, conflicts, drifts = merge_schemas_3way(base, main, branch)
    pre, post = diff_schemas(main, merged)
    return pre, post, conflicts, drifts


def list_relations(conn):
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT n.nspname, c.relname, c.oid, c.relfilenode,
                   pg_relation_filepath(c.oid)
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind IN ('r','m','t')
              AND n.nspname NOT IN {EXCLUDED_SCHEMAS!r}
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
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            # Force pending writes to disk so the file mtime/size we capture
            # reflect committed state, not just what's in shared buffers.
            cur.execute("CHECKPOINT")
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

def find_changed_blocks_lsn_only(pgdata, relpath, snap_lsn):
    """Like find_changed_blocks_per_relation but with no stat-skip layer:
    scan every segment for pages with pd_lsn > snap_lsn. Used for the
    "main" (parent) side of a 3-way diff, where we don't carry a per-file
    snapshot but still need to know which pages drifted after branch time.
    """
    out = []
    bytes_scanned = 0
    for seg_idx, seg_full in segment_paths(pgdata, relpath):
        rel_seg_path = os.path.relpath(seg_full, pgdata)
        try:
            bytes_scanned += os.path.getsize(seg_full)
        except OSError:
            continue
        out.extend(
            scan_segment_for_changed_blocks(
                pgdata, rel_seg_path, snap_lsn, seg_idx * SEGMENT_PAGES
            )
        )
    return out, bytes_scanned


def fetch_rows_for_ctids(conn, schema, table, ctids,
                         column_types, added_defaults=None):
    """Fetch live rows by ctid, casting every data column to text.

    `column_types` is the {colname: pg_typename} mapping for the table's
    visible data columns, in attnum order (from the catalog dump).

    `added_defaults` (optional) is a {colname: default_expr_text_or_None} dict
    for columns present on target but not yet on source. For each such column
    we ask Postgres whether the row's value differs from the column's
    DEFAULT expression, emitted as a boolean "__vka_diff_<col>".

    Returns (cols, rows). All data columns come back as text strings;
    diff-flag columns come back as Python bool.
    """
    if not ctids:
        return [], []
    select_parts = ["ctid::text AS ctid"]
    for col in column_types:
        q = '"' + col.replace('"', '""') + '"'
        select_parts.append(f'{q}::text AS {q}')
    if added_defaults:
        for col, expr in added_defaults.items():
            default_sql = expr if expr is not None else "NULL"
            qc = '"' + col.replace('"', '""') + '"'
            select_parts.append(
                f'({qc} IS DISTINCT FROM ({default_sql})) AS "__vka_diff_{col}"'
            )
    sql_text = (
        f'SELECT {", ".join(select_parts)} FROM "{schema}"."{table}" '
        f'WHERE ctid = ANY(%s::tid[])'
    )
    with conn.cursor() as cur:
        ctid_literals = [f"({b},{o})" for (b, o) in ctids]
        cur.execute(sql_text, (ctid_literals,))
        cols = [d.name for d in cur.description]
        return cols, cur.fetchall()


def _table_column_types(schema_dump, key):
    """Return {colname: pg_typename} for a table from a dump_schema() result."""
    t = schema_dump["tables"].get(key)
    if t is None:
        return {}
    return {c["name"]: c["type"] for c in t["columns"]}


def fetch_rows_by_pk(conn, schema, table, pk_names, pk_values, column_types,
                     added_defaults=None):
    """Fetch rows by primary key (as text). pk_values is an iterable of
    text-tuples matching pk_names in order. Returns (cols, rows).

    `added_defaults`: same semantics as `fetch_rows_for_ctids`.
    """
    pk_values = list(pk_values)
    if not pk_values or not pk_names:
        return [], []
    select_parts = ['ctid::text AS ctid']
    for col in column_types:
        q = '"' + col.replace('"', '""') + '"'
        select_parts.append(f'{q}::text AS {q}')
    if added_defaults:
        for col, expr in added_defaults.items():
            default_sql = expr if expr is not None else "NULL"
            qc = '"' + col.replace('"', '""') + '"'
            select_parts.append(
                f'({qc} IS DISTINCT FROM ({default_sql})) AS "__vka_diff_{col}"'
            )
    pk_select = "(" + ", ".join(f'"{c}"::text' for c in pk_names) + ")"
    placeholders = ", ".join(
        ["(" + ", ".join(["%s"] * len(pk_names)) + ")" for _ in pk_values]
    )
    flat = [v for pk in pk_values for v in pk]
    sql_text = (
        f'SELECT {", ".join(select_parts)} FROM "{schema}"."{table}" '
        f'WHERE {pk_select} IN ({placeholders})'
    )
    with conn.cursor() as cur:
        cur.execute(sql_text, flat)
        cols = [d.name for d in cur.description]
        return cols, cur.fetchall()


def _row_data_dict(cols, row):
    """Strip ctid and __vka_diff_* synthetic cols. Return {colname: text_value}."""
    out = {}
    for i, c in enumerate(cols):
        if c == "ctid" or c.startswith("__vka_diff_"):
            continue
        out[c] = row[i]
    return out


def _diff_flags(cols, row):
    """Return {colname: bool} for __vka_diff_<col> entries."""
    out = {}
    for i, c in enumerate(cols):
        if c.startswith("__vka_diff_"):
            out[c[len("__vka_diff_"):]] = row[i]
    return out


def _row_changed_vs_base(branch_dict, base_dict, branch_diff_flags):
    """A row on branch is changed relative to base if any common column
    differs OR if any column added by branch has a non-default value.
    base_dict has only common cols (it's from base's schema).
    """
    for c, bv in base_dict.items():
        if branch_dict.get(c) != bv:
            return True
    for col, differs in branch_diff_flags.items():
        if differs:
            return True
    return False


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
              "DDL_PRE": 0, "DDL_POST": 0,
              "scanned_files": 0, "scanned_bytes": 0, "skipped_files": 0,
              "rels_with_changes": 0, "rels_no_pk_skipped": 0}
    ddl_pre = []     # DDL to run before DML
    ddl_post = []    # DDL to run after DML (drops)
    sql_out = []     # DML

    with psycopg.connect(dsn(current_db), autocommit=True) as cur_conn, \
         psycopg.connect(dsn(source_db), autocommit=True) as src_conn:

        # Flush dirty buffers on both sides so the data files we'll mmap
        # contain the committed state of the database (otherwise recent
        # writes that only exist in WAL+shared buffers are invisible to us).
        with cur_conn.cursor() as cur:
            cur.execute("CHECKPOINT")
        with src_conn.cursor() as cur:
            cur.execute("CHECKPOINT")

        # ---- Schema (DDL) diff (read-only) ----
        src_schema = dump_schema(src_conn)
        tgt_schema = dump_schema(cur_conn)
        ddl_pre, ddl_post = diff_schemas(src_schema, tgt_schema)
        totals["DDL_PRE"] = len(ddl_pre)
        totals["DDL_POST"] = len(ddl_post)

        # Per-table column-set diff: which columns will ddl_pre ADD on source?
        # We store {colname: default_expr_text_or_None} (raw catalog text,
        # not interpreted). During row diffing we ask Postgres to evaluate
        # "current_value IS DISTINCT FROM (default_expr)" so we never need
        # to interpret a default expression or a column value in Python.
        added_cols_by_table = {}   # 'nsp.name' -> { colname: default_expr_or_None }
        for key, t in tgt_schema["tables"].items():
            s = src_schema["tables"].get(key)
            if s is None:
                continue
            s_cols = {c["name"] for c in s["columns"]}
            added = {}
            for c in t["columns"]:
                if c["name"] not in s_cols:
                    added[c["name"]] = c.get("default")
            if added:
                added_cols_by_table[key] = added

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
            pk_names = [c[0] for c in pk_cols]

            cur_ctids = []
            src_ctids = []
            for block, _lsn in blocks:
                cur_ctids.extend(live_ctids_for_block(pgdata, current_relpath, block))
                src_ctids.extend(live_ctids_for_block(pgdata, src_relpath, block))

            added_for_this_tbl = added_cols_by_table.get(f"{nsp}.{rel}", {})
            cur_types = _table_column_types(tgt_schema, f"{nsp}.{rel}")
            src_types = _table_column_types(src_schema, f"{nsp}.{rel}")
            cur_cols, cur_rows = fetch_rows_for_ctids(
                cur_conn, nsp, rel, cur_ctids, cur_types,
                added_defaults=added_for_this_tbl,
            )
            src_cols, src_rows = fetch_rows_for_ctids(
                src_conn, nsp, rel, src_ctids, src_types,
            )

            if not pk_names:
                # ---------- no-PK fallback: multiset diff on changed pages ----------
                # Use full row content as identity. UPDATEs appear as
                # INSERT + DELETE; that's correct for an unidentifiable row.
                # Build clean data-only rows (strip ctid + any __vka_diff_* flags).
                def _data_only(all_cols, rows):
                    if not all_cols:
                        return [], []
                    keep_idx = [i for i, c in enumerate(all_cols)
                                if c != "ctid" and not c.startswith("__vka_diff_")]
                    cols = [all_cols[i] for i in keep_idx]
                    out = [tuple(r[i] for i in keep_idx) for r in rows]
                    return cols, out
                cur_data_cols, cur_data_rows = _data_only(cur_cols, cur_rows)
                src_data_cols, src_data_rows = _data_only(src_cols, src_rows)
                data_cols = cur_data_cols or src_data_cols
                cur_counter = Counter(cur_data_rows)
                src_counter = Counter(src_data_rows)
                inserts_rows = []
                deletes_rows = []
                for row, cnt in cur_counter.items():
                    extra = cnt - src_counter.get(row, 0)
                    for _ in range(extra):
                        inserts_rows.append(row)
                for row, cnt in src_counter.items():
                    extra = cnt - cur_counter.get(row, 0)
                    for _ in range(extra):
                        deletes_rows.append(row)
                if not (inserts_rows or deletes_rows):
                    continue
                totals["rels_with_changes"] += 1
                totals["INSERT"] += len(inserts_rows)
                totals["DELETE"] += len(deletes_rows)
                print(f"  {nsp}.{rel}: [no PK] INSERT={len(inserts_rows)} "
                      f"DELETE={len(deletes_rows)}")
                qn = f'"{nsp}"."{rel}"'
                # Use target's types for INSERTs (it's where the new row lives).
                ins_types = cur_types or src_types
                del_types = src_types or cur_types
                for row in inserts_rows:
                    cols_s = ", ".join(f'"{c}"' for c in data_cols)
                    vals_s = ", ".join(
                        _sql_text_literal(v, ins_types[c])
                        for c, v in zip(data_cols, row)
                    )
                    sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")
                    if verbose:
                        print(f"    + {dict(zip(data_cols, row))}")
                for row in deletes_rows:
                    # Use ctid subquery with LIMIT 1 to delete exactly one
                    # matching row (handles duplicates safely).
                    where_inner = " AND ".join(
                        (f'"{c}" IS NULL' if v is None
                         else f'"{c}"={_sql_text_literal(v, del_types[c])}')
                        for c, v in zip(data_cols, row)
                    )
                    sql_out.append(
                        f"DELETE FROM {qn} WHERE ctid = "
                        f"(SELECT ctid FROM {qn} WHERE {where_inner} LIMIT 1);"
                    )
                    if verbose:
                        print(f"    - {dict(zip(data_cols, row))}")
                continue

            # column 0 is ctid; data columns start at 1
            # Returned columns: ctid (always position 0), then all data cols,
            # then optionally synthetic "__vka_diff_<col>" boolean flags for
            # columns we asked Postgres to compare to their DEFAULT.
            def split_columns(cols):
                data_cols = []          # ordered data col names (no ctid)
                col_idx = {}            # data colname -> position in row
                diff_flag_idx = {}      # data colname -> position of its diff flag
                for i, c in enumerate(cols or []):
                    if c == "ctid":
                        continue
                    if c.startswith("__vka_diff_"):
                        diff_flag_idx[c[len("__vka_diff_"):]] = i
                    else:
                        col_idx[c] = i
                        data_cols.append(c)
                return data_cols, col_idx, diff_flag_idx

            tgt_data_cols, tgt_col_idx, cur_diff_flag_idx = split_columns(cur_cols)
            src_data_cols, src_col_idx, _ = split_columns(src_cols)

            def build_pk_map(all_cols, col_idx, rows):
                if not all_cols or not rows:
                    return {}
                pk_idx_in_row = [all_cols.index(p) for p in pk_names]
                out = {}
                for r in rows:
                    pk = tuple(r[i] for i in pk_idx_in_row)
                    out[pk] = r   # keep full row so we can read any indexed col
                return out

            cur_map = build_pk_map(cur_cols, tgt_col_idx, cur_rows)
            src_map = build_pk_map(src_cols, src_col_idx, src_rows)

            cur_pks = set(cur_map)
            src_pks = set(src_map)
            inserts = sorted(cur_pks - src_pks)
            deletes = sorted(src_pks - cur_pks)
            common_pks = cur_pks & src_pks

            updates = []
            for pk in common_pks:
                new = cur_map[pk]
                old = src_map[pk]
                row_diffs = []  # list of (col, new_val)
                for c in tgt_data_cols:
                    if c in pk_names:
                        continue
                    n_val = new[tgt_col_idx[c]]
                    if c in src_col_idx:
                        o_val = old[src_col_idx[c]]
                        if n_val != o_val:
                            row_diffs.append((c, n_val))
                    elif c in cur_diff_flag_idx:
                        # New column on target — Postgres told us whether the
                        # value differs from the column's DEFAULT expression.
                        if new[cur_diff_flag_idx[c]]:
                            row_diffs.append((c, n_val))
                if row_diffs:
                    updates.append((pk, row_diffs))

            if not (inserts or deletes or updates):
                continue

            totals["rels_with_changes"] += 1
            totals["INSERT"] += len(inserts)
            totals["UPDATE"] += len(updates)
            totals["DELETE"] += len(deletes)
            print(f"  {nsp}.{rel}: INSERT={len(inserts)} "
                  f"UPDATE={len(updates)} DELETE={len(deletes)}")

            qn = f'"{nsp}"."{rel}"'

            for pk in inserts:
                row = cur_map[pk]
                vals = [row[tgt_col_idx[c]] for c in tgt_data_cols]
                cols_s = ", ".join(f'"{c}"' for c in tgt_data_cols)
                vals_s = ", ".join(
                    _sql_text_literal(v, cur_types[c])
                    for c, v in zip(tgt_data_cols, vals)
                )
                sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")
                if verbose:
                    print(f"    + {dict(zip(tgt_data_cols, vals))}")
            for pk, row_diffs in updates:
                sets = ", ".join(
                    f'"{c}"={_sql_text_literal(v, cur_types[c])}'
                    for c, v in row_diffs
                )
                where = " AND ".join(
                    f'"{c}"={_sql_text_literal(v, cur_types[c])}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(f"UPDATE {qn} SET {sets} WHERE {where};")
                if verbose:
                    print(f"    ~ pk={pk} sets={row_diffs}")
            for pk in deletes:
                where = " AND ".join(
                    f'"{c}"={_sql_text_literal(v, src_types[c])}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(f"DELETE FROM {qn} WHERE {where};")
                if verbose:
                    old = src_map[pk]
                    old_vals = [old[src_col_idx[c]] for c in src_data_cols]
                    print(f"    - pk={pk} row={dict(zip(src_data_cols, old_vals))}")

        # ---- Brand-new tables on target: dump all rows as INSERTs ----
        # These tables don't exist in source's schema at all (ddl_pre will
        # CREATE them). Their rows can't go through the page-LSN path
        # because there's nothing to diff against on source.
        for key, t in tgt_schema["tables"].items():
            if key in src_schema["tables"]:
                continue
            new_types = _table_column_types(tgt_schema, key)
            data_cols = list(new_types.keys())
            select_parts = ", ".join(
                f'"{c}"::text AS "{c}"' for c in data_cols
            ) or "1"
            with cur_conn.cursor() as cur:
                cur.execute(
                    f'SELECT {select_parts} FROM "{t["schema"]}"."{t["name"]}"'
                )
                rows = cur.fetchall()
            if not rows:
                continue
            qn = f'"{t["schema"]}"."{t["name"]}"'
            cols_s = ", ".join(f'"{c}"' for c in data_cols)
            totals["rels_with_changes"] += 1
            totals["INSERT"] += len(rows)
            print(f"  {t['schema']}.{t['name']}: [new table] INSERT={len(rows)}")
            for r in rows:
                vals_s = ", ".join(
                    _sql_text_literal(v, new_types[c])
                    for c, v in zip(data_cols, r)
                )
                sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")

    # Final assembled SQL: DDL pre, then DML, then DDL post (drops).
    final_sql = list(ddl_pre) + list(sql_out) + list(ddl_post)

    dt = (time.perf_counter() - t0) * 1000
    totals["elapsed_ms"] = dt
    totals["sql"] = final_sql
    totals["ddl_pre"] = list(ddl_pre)
    totals["ddl_post"] = list(ddl_post)
    totals["dml"] = list(sql_out)
    if verbose or __name__ == "__main__":
        print()
        print(f"cross-diff complete in {dt:.0f} ms")
        print(f"  files skipped via stat:  {totals['skipped_files']}")
        print(f"  files scanned:           {totals['scanned_files']}  "
              f"({totals['scanned_bytes']/1024/1024:.1f} MB)")
        print(f"  relations with changes:  {totals['rels_with_changes']}")
        print(f"  DDL_PRE={totals['DDL_PRE']}  DDL_POST={totals['DDL_POST']}")
        print(f"  INSERT={totals['INSERT']}  UPDATE={totals['UPDATE']}  DELETE={totals['DELETE']}")
        if totals["rels_no_pk_skipped"]:
            print(f"  relations skipped (no PK): {totals['rels_no_pk_skipped']}")
        if final_sql:
            print("\n-- SQL diff --")
            for s in final_sql:
                print(s)
    return totals


def _sql_text_literal(text_value, typename):
    """Render a Postgres value (already in its canonical text form) as a typed
    SQL literal.

    `text_value` is the string Postgres produced via `col::text`. We never
    interpret it — we just wrap it in quotes and cast back to its type, so
    Postgres parses it on the receiving side. Works for every type with a
    normal text I/O: jsonb, arrays, ranges, PostGIS geometry, custom enums,
    domain types, etc.
    """
    if text_value is None:
        return "NULL"
    escaped = text_value.replace("'", "''")
    return f"'{escaped}'::{typename}"


# ---------- main ----------

def cross_diff_3way(pgdata, source_db, current_db, base_db, snap_path,
                     *, parent_snap_path=None, verbose=False):
    """3-way diff. `base_db` is the COW snapshot of `source_db` at branch
    creation time; used as the merge base.

    Returns a dict with the same keys as `cross_diff` plus:
      - "conflicts": list of dicts. Non-empty means abort the apply.
      - "drifts":    list of dicts. Parent changed these independently.

    On conflict, no SQL is emitted for the conflicted parts; the caller
    should not apply.
    """
    with open(snap_path) as f:
        snap = json.load(f)
    snap_lsn = parse_lsn_str(snap["lsn"])
    snap_by_oid = {r["oid"]: r for r in snap["relations"]}

    # Parent (source) snapshot — used for stat-skip on main side so we
    # don't have to LSN-scan every page of every relation on main.
    parent_snap_by_oid = {}
    if parent_snap_path:
        with open(parent_snap_path) as f:
            parent_snap = json.load(f)
        parent_snap_by_oid = {r["oid"]: r for r in parent_snap["relations"]}

    def dsn(db):
        return f"host=127.0.0.1 dbname={db} user={os.environ.get('USER','aybarsb')}"

    t0 = time.perf_counter()
    totals = {
        "INSERT": 0, "UPDATE": 0, "DELETE": 0,
        "DDL_PRE": 0, "DDL_POST": 0,
        "scanned_files": 0, "scanned_bytes": 0, "skipped_files": 0,
        "rels_with_changes": 0,
    }
    ddl_pre = []
    ddl_post = []
    sql_out = []  # DML
    conflicts = []
    drifts = []

    with psycopg.connect(dsn(current_db), autocommit=True) as cur_conn, \
         psycopg.connect(dsn(source_db), autocommit=True) as src_conn, \
         psycopg.connect(dsn(base_db), autocommit=True) as base_conn:

        for c in (cur_conn, src_conn, base_conn):
            with c.cursor() as cur:
                cur.execute("CHECKPOINT")

        # ---- Schema 3-way ----
        base_schema = dump_schema(base_conn)
        main_schema = dump_schema(src_conn)
        branch_schema = dump_schema(cur_conn)
        s_pre, s_post, s_conflicts, s_drifts = diff_schemas_3way(
            base_schema, main_schema, branch_schema
        )
        ddl_pre.extend(s_pre)
        ddl_post.extend(s_post)
        conflicts.extend(s_conflicts)
        drifts.extend(s_drifts)
        totals["DDL_PRE"] = len(ddl_pre)
        totals["DDL_POST"] = len(ddl_post)

        # If schema-level conflicts, return early — don't even try DML.
        if conflicts:
            totals["elapsed_ms"] = (time.perf_counter() - t0) * 1000
            totals["sql"] = []
            totals["ddl_pre"] = ddl_pre
            totals["ddl_post"] = ddl_post
            totals["dml"] = []
            totals["conflicts"] = conflicts
            totals["drifts"] = drifts
            if verbose or __name__ == "__main__":
                print()
                print(f"cross-diff-3way ABORTED in {totals['elapsed_ms']:.0f} ms")
                print(f"  conflicts: {len(conflicts)}")
                for c in conflicts:
                    print(f"    !! {c['kind']} {c['key']}")
            return totals

        # Per-table column-set added on branch but not yet on main (will be
        # ADDed by ddl_pre). Same idea as 2-way: ask Postgres whether each
        # row's value matches the column's DEFAULT.
        added_cols_by_table = {}
        for key, t in branch_schema["tables"].items():
            mt = main_schema["tables"].get(key)
            if mt is None:
                continue
            m_cols = {c["name"] for c in mt["columns"]}
            added = {}
            for c in t["columns"]:
                if c["name"] not in m_cols:
                    added[c["name"]] = c.get("default")
            if added:
                added_cols_by_table[key] = added

        # ---- Brand-new tables on branch (not in base, not in main) ----
        for key, t in branch_schema["tables"].items():
            if key in main_schema["tables"] or key in base_schema["tables"]:
                continue
            new_types = _table_column_types(branch_schema, key)
            data_cols = list(new_types.keys())
            select_parts = ", ".join(f'"{c}"::text AS "{c}"' for c in data_cols) or "1"
            with cur_conn.cursor() as cur:
                cur.execute(
                    f'SELECT {select_parts} FROM "{t["schema"]}"."{t["name"]}"'
                )
                rows = cur.fetchall()
            if not rows:
                continue
            qn = f'"{t["schema"]}"."{t["name"]}"'
            cols_s = ", ".join(f'"{c}"' for c in data_cols)
            totals["rels_with_changes"] += 1
            totals["INSERT"] += len(rows)
            for r in rows:
                vals_s = ", ".join(
                    _sql_text_literal(v, new_types[c])
                    for c, v in zip(data_cols, r)
                )
                sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")

        # ---- Row-level 3-way for tables present on both branch and main ----
        cur_rels = list_relations(cur_conn)
        src_rels = list_relations(src_conn)
        base_rels = list_relations(base_conn)
        src_relpath_by_name = {(r[0], r[1]): r[4] for r in src_rels}
        base_relpath_by_name = {(r[0], r[1]): r[4] for r in base_rels}

        for nsp, rel, oid, current_relfilenode, current_relpath in cur_rels:
            key = f"{nsp}.{rel}"
            if key not in base_schema["tables"]:
                continue   # branch addition (handled above as new table)
            if key not in main_schema["tables"]:
                continue   # branch deleted; DDL_POST drops it on main

            prev = snap_by_oid.get(oid)
            if prev is None:
                continue
            if current_relfilenode != prev["relfilenode"]:
                continue  # rare: rewrite on branch (VACUUM FULL etc.)

            branch_blocks, b_bytes, b_files, b_skipped = (
                find_changed_blocks_per_relation(
                    pgdata, current_relpath, prev["segments"], snap_lsn
                )
            )
            totals["scanned_bytes"] += b_bytes
            totals["scanned_files"] += b_files
            totals["skipped_files"] += b_skipped

            main_relpath = src_relpath_by_name.get((nsp, rel))
            base_relpath = base_relpath_by_name.get((nsp, rel))
            if main_relpath is None or base_relpath is None:
                continue

            # Look up main's OID + parent-snapshot entry for stat-skip.
            main_oid = next(
                (r[2] for r in src_rels if r[0] == nsp and r[1] == rel),
                None,
            )
            main_prev = parent_snap_by_oid.get(main_oid) if main_oid else None
            if main_prev is not None and main_prev["relfilenode"] == \
                    next(r[3] for r in src_rels if r[0] == nsp and r[1] == rel):
                # Stat-skip path: only scan pages of segments that drifted.
                main_blocks, m_bytes, m_files, m_skipped = (
                    find_changed_blocks_per_relation(
                        pgdata, main_relpath, main_prev["segments"], snap_lsn
                    )
                )
                totals["scanned_bytes"] += m_bytes
                totals["scanned_files"] += m_files
                totals["skipped_files"] += m_skipped
            else:
                # Fall back to full LSN scan (older branches without a
                # saved parent snapshot, or rewritten relations).
                main_blocks, m_bytes = find_changed_blocks_lsn_only(
                    pgdata, main_relpath, snap_lsn
                )
                totals["scanned_bytes"] += m_bytes

            if not branch_blocks and not main_blocks:
                continue

            pk_cols = get_pk_columns(cur_conn, oid)
            pk_names = [c[0] for c in pk_cols]
            if not pk_names:
                continue  # no-PK 3-way deferred

            main_types   = _table_column_types(main_schema, key)
            branch_types = _table_column_types(branch_schema, key)
            base_types   = _table_column_types(base_schema, key)
            added_for_this_tbl = added_cols_by_table.get(key, {})

            # Candidate PK collection: read live ctids on changed pages from
            # BOTH the changed side AND base. This finds deleted PKs (still
            # alive on base, missing on current side) as well as inserted /
            # updated PKs (alive on current).
            def collect_pks_from_ctids(conn, schema, table, types, ctids):
                if not ctids:
                    return set()
                cols, rows = fetch_rows_for_ctids(conn, schema, table, ctids, types)
                if not cols or not rows:
                    return set()
                pk_idx = [cols.index(p) for p in pk_names]
                return {tuple(r[i] for i in pk_idx) for r in rows}

            candidate_pks = set()
            # From branch's changed pages: live on branch (current state) + live on base (pre-change state)
            br_ctids = []
            base_ctids_branch_side = []
            for block, _lsn in branch_blocks:
                br_ctids.extend(live_ctids_for_block(pgdata, current_relpath, block))
                base_ctids_branch_side.extend(live_ctids_for_block(pgdata, base_relpath, block))
            candidate_pks |= collect_pks_from_ctids(cur_conn, nsp, rel, branch_types, br_ctids)
            candidate_pks |= collect_pks_from_ctids(base_conn, nsp, rel, base_types, base_ctids_branch_side)
            # From main's changed pages: live on main (current) + live on base (pre-change)
            mn_ctids = []
            base_ctids_main_side = []
            for block, _lsn in main_blocks:
                mn_ctids.extend(live_ctids_for_block(pgdata, main_relpath, block))
                base_ctids_main_side.extend(live_ctids_for_block(pgdata, base_relpath, block))
            candidate_pks |= collect_pks_from_ctids(src_conn, nsp, rel, main_types, mn_ctids)
            candidate_pks |= collect_pks_from_ctids(base_conn, nsp, rel, base_types, base_ctids_main_side)

            if not candidate_pks:
                continue

            # AUTHORITATIVE PK-based fetches: ask each DB for the current state
            # of these PKs. A PK absent from a fetch result means that DB no
            # longer has the row (deleted), not "we just didn't scan its page".
            branch_cols, branch_rows = fetch_rows_by_pk(
                cur_conn, nsp, rel, pk_names, candidate_pks, branch_types,
                added_defaults=added_for_this_tbl,
            )
            main_cols_, main_rows = fetch_rows_by_pk(
                src_conn, nsp, rel, pk_names, candidate_pks, main_types,
            )
            base_cols, base_rows = fetch_rows_by_pk(
                base_conn, nsp, rel, pk_names, candidate_pks, base_types,
            )

            def pk_dict_map(cols, rows):
                if not cols or not rows:
                    return {}
                pk_idx = [cols.index(p) for p in pk_names]
                return {tuple(r[i] for i in pk_idx): (cols, r) for r in rows}

            branch_map = pk_dict_map(branch_cols, branch_rows)
            main_map = pk_dict_map(main_cols_, main_rows)
            base_map_raw = pk_dict_map(base_cols, base_rows)
            base_map = {
                pk: _row_data_dict(c, r) for pk, (c, r) in base_map_raw.items()
            }
            all_pks = candidate_pks

            tbl_inserts = []
            tbl_updates = []
            tbl_deletes = []

            for pk in all_pks:
                branch_pair = branch_map.get(pk)
                main_pair = main_map.get(pk)
                base_row = base_map.get(pk)

                branch_data = (
                    _row_data_dict(*branch_pair) if branch_pair else None
                )
                branch_flags = (
                    _diff_flags(*branch_pair) if branch_pair else {}
                )
                main_data = _row_data_dict(*main_pair) if main_pair else None

                # Classify what each side did to this row.
                # branch's op:
                if branch_data is None:
                    if base_row is None:
                        branch_op = "NOOP"
                    else:
                        branch_op = "DELETE"
                else:
                    if base_row is None:
                        branch_op = "INSERT"
                    else:
                        if _row_changed_vs_base(branch_data, base_row, branch_flags):
                            branch_op = "UPDATE"
                        else:
                            branch_op = "NOOP"

                # main's op (no diff flags needed — main has same schema as base):
                if main_data is None:
                    if base_row is None:
                        main_op = "NOOP"
                    else:
                        main_op = "DELETE"
                else:
                    if base_row is None:
                        main_op = "INSERT"
                    else:
                        if any(main_data.get(c) != base_row.get(c) for c in base_row):
                            main_op = "UPDATE"
                        else:
                            main_op = "NOOP"

                if branch_op == "NOOP":
                    if main_op != "NOOP":
                        drifts.append({
                            "kind": "row", "key": f"{key}#{pk}",
                            "main_op": main_op,
                        })
                    continue

                if main_op == "NOOP":
                    # safe: apply branch's intent
                    if branch_op == "INSERT":
                        tbl_inserts.append(pk)
                    elif branch_op == "DELETE":
                        tbl_deletes.append(pk)
                    elif branch_op == "UPDATE":
                        tbl_updates.append(pk)
                    continue

                # Both touched the row.
                if branch_op == main_op:
                    # Same operation kind — same final value?
                    if branch_op == "DELETE":
                        continue  # both deleted: agreed
                    if branch_op in ("INSERT", "UPDATE"):
                        # Compare the resulting row on common cols
                        common = set(main_data.keys()) & set(branch_data.keys())
                        if all(main_data[c] == branch_data[c] for c in common):
                            # Also check added cols don't differ from default
                            if not any(branch_flags.values()):
                                continue  # same change, both sides
                conflicts.append({
                    "kind": "row", "key": f"{key}#{pk}",
                    "branch_op": branch_op, "main_op": main_op,
                    "base": base_row,
                    "main": main_data,
                    "branch": branch_data,
                })

            if conflicts:
                # we'll surface conflicts but keep iterating tables for full report
                continue

            if not (tbl_inserts or tbl_updates or tbl_deletes):
                continue
            totals["rels_with_changes"] += 1
            totals["INSERT"] += len(tbl_inserts)
            totals["UPDATE"] += len(tbl_updates)
            totals["DELETE"] += len(tbl_deletes)
            qn = f'"{nsp}"."{rel}"'

            # Emit INSERTs
            for pk in tbl_inserts:
                cols, row = branch_map[pk]
                data_cols = [c for c in cols
                             if c != "ctid" and not c.startswith("__vka_diff_")]
                col_idx = {c: i for i, c in enumerate(cols)}
                vals = [row[col_idx[c]] for c in data_cols]
                cols_s = ", ".join(f'"{c}"' for c in data_cols)
                vals_s = ", ".join(
                    _sql_text_literal(v, branch_types[c])
                    for c, v in zip(data_cols, vals)
                )
                sql_out.append(f"INSERT INTO {qn} ({cols_s}) VALUES ({vals_s});")

            # Emit UPDATEs: only changed cols, plus added-col where flag is True
            for pk in tbl_updates:
                cols, row = branch_map[pk]
                col_idx = {c: i for i, c in enumerate(cols)}
                base_row = base_map[pk]
                set_clauses = []
                # Common cols (in both base and branch)
                for c in base_row:
                    if c in pk_names:
                        continue
                    if c not in col_idx:
                        continue
                    new_v = row[col_idx[c]]
                    old_v = base_row[c]
                    if new_v != old_v:
                        set_clauses.append(
                            f'"{c}"={_sql_text_literal(new_v, branch_types[c])}'
                        )
                # Added cols (on branch but not in base) — emit if non-default
                flags = _diff_flags(cols, row)
                for c, differs in flags.items():
                    if differs:
                        new_v = row[col_idx[c]]
                        set_clauses.append(
                            f'"{c}"={_sql_text_literal(new_v, branch_types[c])}'
                        )
                if not set_clauses:
                    continue
                where = " AND ".join(
                    f'"{c}"={_sql_text_literal(v, branch_types[c])}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(
                    f"UPDATE {qn} SET {', '.join(set_clauses)} WHERE {where};"
                )

            # Emit DELETEs
            for pk in tbl_deletes:
                where = " AND ".join(
                    f'"{c}"={_sql_text_literal(v, main_types[c])}'
                    for c, v in zip(pk_names, pk)
                )
                sql_out.append(f"DELETE FROM {qn} WHERE {where};")

    final_sql = [] if conflicts else (list(ddl_pre) + list(sql_out) + list(ddl_post))
    dt = (time.perf_counter() - t0) * 1000
    totals["elapsed_ms"] = dt
    totals["sql"] = final_sql
    totals["ddl_pre"] = list(ddl_pre)
    totals["ddl_post"] = list(ddl_post)
    totals["dml"] = list(sql_out)
    totals["conflicts"] = conflicts
    totals["drifts"] = drifts

    if verbose or __name__ == "__main__":
        print()
        print(f"cross-diff-3way complete in {dt:.0f} ms")
        print(f"  DDL_PRE={totals['DDL_PRE']}  DDL_POST={totals['DDL_POST']}")
        print(f"  INSERT={totals['INSERT']}  UPDATE={totals['UPDATE']}  DELETE={totals['DELETE']}")
        print(f"  conflicts={len(conflicts)}  drifts={len(drifts)}")
        if conflicts:
            print("\n!! CONFLICTS (no SQL emitted):")
            for c in conflicts:
                print(f"   {c['kind']} {c['key']}: branch={c.get('branch_op','?')} main={c.get('main_op','?')}")
        if drifts:
            print("\n-- parent drift (left alone):")
            for d in drifts[:10]:
                print(f"   {d['kind']} {d['key']}")
            if len(drifts) > 10:
                print(f"   ... and {len(drifts) - 10} more")
        if final_sql:
            print("\n-- SQL diff --")
            for s in final_sql:
                print(s)
    return totals


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
