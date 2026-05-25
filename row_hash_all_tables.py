import os, sys, time, threading, psycopg, subprocess, shlex, math
from concurrent.futures import ThreadPoolExecutor, as_completed
from psycopg import sql

dsn = os.environ["FLD_DATABASE"]

def list_user_tables_with_stats(con):
    with con.cursor() as cur:
        cur.execute("""
            select n.nspname, c.relname, c.oid,
                   greatest(c.reltuples,0)::bigint as est_rows,
                   pg_total_relation_size(c.oid) as total_bytes
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where c.relkind = 'r'
              and n.nspname not in ('pg_catalog','information_schema')
            order by n.nspname, c.relname
        """)
        return cur.fetchall()

def list_columns(con, schema, table):
    with con.cursor() as cur:
        cur.execute("""
            select column_name
            from information_schema.columns
            where table_schema = %s and table_name = %s
            order by ordinal_position
        """, (schema, table))
        return [r[0] for r in cur.fetchall()]

def list_pk_columns(con, schema, table):
    with con.cursor() as cur:
        cur.execute("""
            select a.attname
            from pg_index i
            join pg_class c on c.oid = i.indrelid
            join pg_namespace n on n.oid = c.relnamespace
            join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)
            where i.indisprimary
              and n.nspname = %s and c.relname = %s
            order by array_position(i.indkey, a.attnum)
        """, (schema, table))
        return [r[0] for r in cur.fetchall()]

def pretty_bytes(b):
    g = 1024**3
    m = 1024**2
    if b >= g:
        return f"{b/g:.2f} GB"
    if b >= m:
        return f"{b/m:.2f} MB"
    return f"{b/1024:.2f} KB"

def pretty_time(s):
    if s < 1:
        return f"{s*1000:.0f}ms"
    if s < 60:
        return f"{s:.2f}s"
    m = int(s // 60)
    sec = s - m*60
    return f"{m}m{sec:.0f}s"

def digest_table(con, schema, table, use_blake3=True):
    if use_blake3:
        import blake3
        h = blake3.blake3()
    else:
        cmd = shlex.split("openssl dgst -sha256 -binary")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    cols = list_columns(con, schema, table)
    pk = list_pk_columns(con, schema, table)
    if not cols:
        return "", 0
    select_list = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    if pk:
        order_by = sql.SQL(", ").join(sql.Identifier(c) for c in pk)
    else:
        order_by = sql.SQL(", ").join(sql.Identifier(c) for c in cols)
    q = sql.SQL("COPY (SELECT {select_list} FROM {tbl} ORDER BY {order_by}) TO STDOUT (FORMAT binary)").format(
        select_list=select_list,
        tbl=sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table)),
        order_by=order_by,
    )
    streamed = 0
    with con.cursor() as cur:
        with cur.copy(q) as cp:
            for chunk in cp:
                streamed += len(chunk)
                if use_blake3:
                    h.update(chunk)
                else:
                    p.stdin.write(chunk)
    if use_blake3:
        return h.hexdigest(), streamed
    p.stdin.close()
    return p.stdout.read().hex(), streamed

def db_total_bytes(con):
    with con.cursor() as cur:
        cur.execute("select pg_database_size(current_database())")
        return int(cur.fetchone()[0])

def hash_worker(schema, table, use_blake3, total_rows, total_bytes, start_all, counter):
    with psycopg.connect(dsn, autocommit=True) as con:
        with con.cursor() as cur:
            cur.execute("""
                select greatest(c.reltuples,0)::bigint, pg_total_relation_size(c.oid)
                from pg_class c join pg_namespace n on n.oid=c.relnamespace
                where n.nspname=%s and c.relname=%s
            """, (schema, table))
            r = cur.fetchone() or (0, 0)
            est_rows, total_b = int(r[0]), int(r[1])
        print(f"starting: {schema}.{table}"
                f"size {pretty_bytes(total_b)}  "
                f"rows~{est_rows}"
                )
        t0 = time.perf_counter()
        digest, streamed = digest_table(con, schema, table, use_blake3=use_blake3)
        dt = time.perf_counter() - t0
    with counter["lock"]:
        counter["bytes_done"] += total_b
        spent = time.perf_counter() - start_all
        done = counter["bytes_done"]
        left = (total_bytes - done) / (done/spent) if done and total_bytes else 0
    rate = (streamed/dt) if dt > 0 else 0
    bytes_pct = (total_b/total_bytes)*100 if total_bytes else 0
    rows_pct = (est_rows/total_rows)*100 if total_rows else 0
    line = (
        f"done: {schema}.{table} {digest} "
        f"size {pretty_bytes(total_b)} ({bytes_pct:.2f}% of set) "
        f"rows~{est_rows} ({rows_pct:.2f}% of set) "
        f"took {pretty_time(dt)} "
        f"rate {pretty_bytes(rate)}/s "
        f"spent {pretty_time(spent)} left~{pretty_time(left)}"
    )
    return total_b, est_rows, line

def main():
    use_blake3 = True
    if len(sys.argv) > 1 and sys.argv[1].lower() == "--sha256":
        use_blake3 = False
        sys.argv.pop(1)
    table_arg = sys.argv[1] if len(sys.argv) > 1 else None
    workers = int(os.environ.get("FLD_HASH_WORKERS", "1"))
    with psycopg.connect(dsn, autocommit=True) as con:
        if table_arg:
            if "." in table_arg:
                schema, table = table_arg.split(".", 1)
            else:
                schema, table = "public", table_arg
            tables = [(schema, table, None, None, None)]
            total_rows = 0
            total_bytes = 0
        else:
            tables = list_user_tables_with_stats(con)
            total_rows = sum(int(t[3]) for t in tables)
            total_bytes = sum(int(t[4]) for t in tables)
        database_bytes = db_total_bytes(con)
    start_all = time.perf_counter()
    processed_bytes = 0
    processed_rows = 0
    if workers <= 1 or len(tables) <= 1:
        for t in tables:
            schema, table = t[0], t[1]
            print(f"starting: {schema}.{table}")
            with psycopg.connect(dsn, autocommit=True) as con:
                if table_arg:
                    with con.cursor() as cur:
                        cur.execute("""
                            select greatest(c.reltuples,0)::bigint, pg_total_relation_size(c.oid)
                            from pg_class c join pg_namespace n on n.oid=c.relnamespace
                            where n.nspname=%s and c.relname=%s
                        """, (schema, table))
                        r = cur.fetchone() or (0, 0)
                        est_rows, total_b = int(r[0]), int(r[1])
                else:
                    est_rows, total_b = int(t[3]), int(t[4])
                t0 = time.perf_counter()
                digest, streamed = digest_table(con, schema, table, use_blake3=use_blake3)
                dt = time.perf_counter() - t0
            processed_bytes += total_b
            processed_rows += est_rows
            spent = time.perf_counter() - start_all
            left = (total_bytes - processed_bytes) / (processed_bytes/spent) if processed_bytes and total_bytes else 0
            rate = (streamed/dt) if dt > 0 else 0
            bytes_pct = (total_b/total_bytes)*100 if total_bytes else 0
            rows_pct = (est_rows/total_rows)*100 if total_rows else 0
            print(
                f"{schema}.{table} {digest} "
                f"size {pretty_bytes(total_b)} ({bytes_pct:.2f}% of set) "
                f"rows~{est_rows} ({rows_pct:.2f}% of set) "
                f"took {pretty_time(dt)} "
                f"rate {pretty_bytes(rate)}/s "
                f"spent {pretty_time(spent)} left~{pretty_time(left)}"
            )
    else:
        counter = {"bytes_done": 0, "lock": threading.Lock()}
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = []
            for t in tables:
                futs.append(ex.submit(hash_worker, t[0], t[1], use_blake3, total_rows, total_bytes, start_all, counter))
            for f in as_completed(futs):
                tb, tr, line = f.result()
                processed_bytes += tb
                processed_rows += tr
                print(line)
    total_spent = time.perf_counter() - start_all
    set_bytes = total_bytes if total_bytes else processed_bytes
    print(
        f"SUMMARY tables={len(tables)} "
        f"set_size={pretty_bytes(set_bytes)} "
        f"db_size={pretty_bytes(database_bytes)} "
        f"rows~{processed_rows} "
        f"took {pretty_time(total_spent)}"
    )

if __name__ == "__main__":
    main()
