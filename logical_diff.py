import os
import argparse
from collections import defaultdict
from urllib.parse import urlparse, urlunparse
import psycopg
from psycopg import sql, adapt

def conn_for(dbname):
    base = os.environ.get("FLD_DATABASE")
    if not base:
        raise RuntimeError("FLD_DATABASE is not set")
    u = urlparse(base)
    if u.scheme not in ("postgresql", "postgres"):
        raise RuntimeError("FLD_DATABASE must be a postgresql:// or postgres:// URL")
    new_path = "/" + dbname
    dsn = urlunparse((u.scheme, u.netloc, new_path, u.params, u.query, u.fragment))
    return psycopg.connect(dsn)

def qident(name):
    return '"' + name.replace('"', '""') + '"'

def qvalue(v, conn):
    if v is None:
        return "NULL"
    return sql.Literal(v).as_string(conn)

def get_columns(cur):
    cur.execute("""
        select n.nspname, c.relname, a.attname, t.typname, a.atttypmod, a.attnotnull,
               pg_get_expr(def.adbin, def.adrelid) as default_expr
        from pg_class c
        join pg_namespace n on n.oid = c.relnamespace
        join pg_attribute a on a.attrelid=c.oid and a.attnum>0 and not a.attisdropped
        join pg_type t on t.oid=a.atttypid
        left join pg_attrdef def on def.adrelid=c.oid and def.adnum=a.attnum
        where c.relkind='r' and n.nspname not in ('pg_catalog','information_schema')
        order by n.nspname, c.relname, a.attnum
    """)
    cols = defaultdict(list)
    for r in cur.fetchall():
        nsp, rel, att, typ, typmod, notnull, default_expr = r
        cols[(nsp, rel)].append((att, typ, typmod, notnull, default_expr))
    return cols

def get_primary_keys(cur):
    cur.execute("""
        select n.nspname, c.relname, array_agg(a.attname order by x.k) as cols
        from pg_constraint con
        join pg_class c on c.oid = con.conrelid
        join pg_namespace n on n.oid = c.relnamespace
        join unnest(con.conkey) with ordinality as x(attnum,k) on true
        join pg_attribute a on a.attrelid=c.oid and a.attnum=x.attnum
        where con.contype='p' and n.nspname not in ('pg_catalog','information_schema')
        group by 1,2
    """)
    pks = {}
    for r in cur.fetchall():
        pks[(r[0], r[1])] = tuple(r[2])
    return pks

def format_coltype(typ, typmod):
    if typ in ("varchar","bpchar"):
        if typmod > 4:
            return f"{'character varying' if typ=='varchar' else 'character'}({typmod-4})"
        return "text" if typ=="varchar" else "char"
    if typ == "numeric":
        if typmod >= 0:
            precision = ((typmod - 4) >> 16) & 65535
            scale = (typmod - 4) & 65535
            return f"numeric({precision},{scale})"
        return "numeric"
    return typ

def ddl_diff(left_cur, right_cur):
    left_cols = get_columns(left_cur)
    right_cols = get_columns(right_cur)
    left_pks = get_primary_keys(left_cur)
    right_pks = get_primary_keys(right_cur)

    left_tables = set(left_cols.keys())
    right_tables = set(right_cols.keys())

    stmts = []

    for tbl in sorted(right_tables - left_tables):
        nsp, rel = tbl
        cols = right_cols[tbl]
        defs = []
        for att, typ, typmod, notnull, default_expr in cols:
            tstr = format_coltype(typ, typmod)
            cdef = f"{qident(att)} {tstr}"
            if default_expr:
                cdef += f" DEFAULT {default_expr}"
            if notnotnull := notnull:
                cdef += " NOT NULL"
            defs.append(cdef)
        pk = right_pks.get(tbl)
        if pk:
            defs.append(f"PRIMARY KEY ({', '.join(qident(x) for x in pk)})")
        stmts.append(f"CREATE TABLE {qident(nsp)}.{qident(rel)} (\n  " + ",\n  ".join(defs) + "\n);")

    for tbl in sorted(left_tables & right_tables):
        nsp, rel = tbl
        lcols = {c[0]: c for c in left_cols[tbl]}
        rcols = {c[0]: c for c in right_cols[tbl]}

        for cname in rcols.keys() - lcols.keys():
            att, typ, typmod, notnull, default_expr = rcols[cname]
            tstr = format_coltype(typ, typmod)
            stmt = f"ALTER TABLE {qident(nsp)}.{qident(rel)} ADD COLUMN {qident(att)} {tstr}"
            if default_expr:
                stmt += f" DEFAULT {default_expr}"
            if notnotnull := notnull:
                stmt += " NOT NULL"
            stmts.append(stmt + ";")

        for cname in lcols.keys() - rcols.keys():
            stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} DROP COLUMN {qident(cname)};")

        for cname in lcols.keys() & rcols.keys():
            la = lcols[cname]
            ra = rcols[cname]
            ltyp = format_coltype(la[1], la[2])
            rtyp = format_coltype(ra[1], ra[2])
            if ltyp != rtyp:
                stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ALTER COLUMN {qident(cname)} TYPE {rtyp};")
            ldef = la[4] or None
            rdef = ra[4] or None
            if ldef != rdef:
                if rdef is None:
                    stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ALTER COLUMN {qident(cname)} DROP DEFAULT;")
                else:
                    stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ALTER COLUMN {qident(cname)} SET DEFAULT {rdef};")
            lnn = la[3]
            rnn = ra[3]
            if lnn != rnn:
                if rnn:
                    stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ALTER COLUMN {qident(cname)} SET NOT NULL;")
                else:
                    stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ALTER COLUMN {qident(cname)} DROP NOT NULL;")

        lpk = left_pks.get(tbl)
        rpk = right_pks.get(tbl)
        if lpk != rpk:
            if lpk:
                stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} DROP CONSTRAINT {qident(rel + '_pkey')};")
            if rpk:
                stmts.append(f"ALTER TABLE {qident(nsp)}.{qident(rel)} ADD PRIMARY KEY ({', '.join(qident(x) for x in rpk)});")

    for tbl in sorted(left_tables - right_tables):
        nsp, rel = tbl
        stmts.append(f"DROP TABLE {qident(nsp)}.{qident(rel)};")

    return stmts

def table_rows(cur, nsp, rel, cols, pk):
    cur.execute(sql.SQL("select {} from {}.{}").format(
        sql.SQL(", ").join(sql.Identifier(c) for c in cols),
        sql.Identifier(nsp), sql.Identifier(rel)
    ))
    rows = {}
    idx = {c: i for i, c in enumerate(cols)}
    for r in cur.fetchall():
        key = tuple(r[idx[c]] for c in pk)
        rows[key] = tuple(r)
    return rows

def dml_diff(left_cur, right_cur):
    left_cols = get_columns(left_cur)
    right_cols = get_columns(right_cur)
    left_pks = get_primary_keys(left_cur)
    right_pks = get_primary_keys(right_cur)

    tables_left = set(left_cols.keys())
    tables_right = set(right_cols.keys())

    stmts = []

    for tbl in sorted(tables_right - tables_left):
        nsp, rel = tbl
        cols = [c[0] for c in right_cols[tbl]]
        right_cur.execute(sql.SQL("select {} from {}.{}").format(
            sql.SQL(", ").join(sql.Identifier(c) for c in cols),
            sql.Identifier(nsp), sql.Identifier(rel)
        ))
        for row in right_cur.fetchall():
            values = ", ".join(qvalue(row[i], right_cur.connection) for i in range(len(cols)))
            stmts.append(f"INSERT INTO {qident(nsp)}.{qident(rel)} (" + ", ".join(qident(c) for c in cols) + f") VALUES ({values});")

    for tbl in sorted(tables_left & tables_right):
        nsp, rel = tbl
        lpk = left_pks.get(tbl)
        rpk = right_pks.get(tbl)
        if not lpk or not rpk or lpk != rpk:
            continue
        lcols = [c[0] for c in left_cols[tbl]]
        rcols = [c[0] for c in right_cols[tbl]]
        common = [c for c in rcols if c in set(lcols)]
        if not set(lpk).issubset(set(common)):
            continue

        lrows = table_rows(left_cur, nsp, rel, common, lpk)
        rrows = table_rows(right_cur, nsp, rel, common, lpk)

        lkeys = set(lrows.keys())
        rkeys = set(rrows.keys())

        ins_keys = rkeys - lkeys
        del_keys = lkeys - rkeys
        cmp_keys = lkeys & rkeys

        conn = left_cur.connection

        for k in sorted(del_keys):
            where = []
            for i, c in enumerate(lpk):
                where.append(f"{qident(c)}={qvalue(k[i], conn)}")
            stmts.append(f"DELETE FROM {qident(nsp)}.{qident(rel)} WHERE " + " AND ".join(where) + ";")

        for k in sorted(ins_keys):
            row = rrows[k]
            values = ", ".join(qvalue(row[i], right_cur.connection) for i in range(len(common)))
            stmts.append(f"INSERT INTO {qident(nsp)}.{qident(rel)} (" + ", ".join(qident(c) for c in common) + f") VALUES ({values});")

        for k in sorted(cmp_keys):
            lrow = lrows[k]
            rrow = rrows[k]
            sets = []
            for i, c in enumerate(common):
                if c in lpk:
                    continue
                if lrow[i] != rrow[i]:
                    sets.append(f"{qident(c)}={qvalue(rrow[i], right_cur.connection)}")
            if sets:
                where = []
                for i, c in enumerate(lpk):
                    where.append(f"{qident(c)}={qvalue(k[i], conn)}")
                stmts.append(f"UPDATE {qident(nsp)}.{qident(rel)} SET " + ", ".join(sets) + " WHERE " + " AND ".join(where) + ";")

    return stmts

def main():
    p = argparse.ArgumentParser()
    p.add_argument("left_db")
    p.add_argument("right_db")
    args = p.parse_args()

    with conn_for(args.left_db) as lconn, conn_for(args.right_db) as rconn:
        with lconn.cursor() as lcur, rconn.cursor() as rcur:
            ddls = ddl_diff(lcur, rcur)
            dmls = dml_diff(lcur, rcur)

            print("-- DDL")
            for s in ddls:
                print(s)
            print("-- DML")
            for s in dmls:
                print(s)

if __name__ == "__main__":
    main()
