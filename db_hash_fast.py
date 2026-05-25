import os
import argparse
from urllib.parse import urlparse, urlunparse
import psycopg
from psycopg import sql

DDL = """
create extension if not exists pgcrypto;
create schema if not exists foldout;

create table if not exists foldout.hash_config(
  schema_name text not null,
  table_name text not null,
  chunk_rows int not null default 200000,
  primary key(schema_name, table_name)
);

create unlogged table if not exists foldout.row_hashes(
  schema_name text not null,
  table_name text not null,
  pk_hash bytea not null,
  chunk_id bigint not null,
  row_hash bytea not null,
  primary key(schema_name, table_name, pk_hash)
);

create index if not exists row_hashes_chunk_idx on foldout.row_hashes(schema_name, table_name, chunk_id);

create unlogged table if not exists foldout.chunk_hashes(
  schema_name text not null,
  table_name text not null,
  chunk_id bigint not null,
  chunk_hash bytea not null,
  row_count int not null,
  primary key(schema_name, table_name, chunk_id)
);

create or replace function foldout.row_hashes_has_pk() returns boolean language sql stable as $$
select exists (select 1 from information_schema.columns where table_schema='foldout' and table_name='row_hashes' and column_name='pk')
$$;

create or replace function foldout.pk_tuple(_schema text, _table text) returns text[] language sql stable as $$
select array_agg(a.attname order by x.k)
from pg_constraint con join pg_class c on c.oid=con.conrelid join pg_namespace n on n.oid=c.relnamespace join unnest(con.conkey) with ordinality as x(attnum,k) on true join pg_attribute a on a.attrelid=c.oid and a.attnum=x.attnum
where con.contype='p' and n.nspname=_schema and c.relname=_table
$$;

create or replace function foldout.all_cols(_schema text, _table text) returns text[] language sql stable as $$
select array_agg(a.attname order by a.attnum)
from pg_attribute a join pg_class c on a.attrelid=c.oid join pg_namespace n on n.oid=c.relnamespace
where n.nspname=_schema and c.relname=_table and a.attnum>0 and not a.attisdropped
$$;

create or replace function foldout.row_digest(rec anyelement) returns bytea language sql immutable as $$
select digest(convert_to($1::text,'UTF8'),'sha256')
$$;

create or replace function foldout.compute_chunk_id(_schema text, _table text, pk_cols text[], chunk_rows int, rec record) returns bigint language plpgsql immutable as $$
declare i int; k text := ''; h bigint;
begin
  if pk_cols is null then return 0; end if;
  for i in 1..array_length(pk_cols,1) loop
    k := k || coalesce(row_to_json(rec)::json->>pk_cols[i],'∅') || '␟';
  end loop;
  h := abs(hashtextextended(k,0));
  return h / greatest(chunk_rows,1);
end
$$;

create or replace function foldout.fold_chunk_hash(_schema text, _table text, _chunk_id bigint, _pk_cols text[]) returns table(chunk_hash bytea, row_count int) language sql as $$
with rows as (
  select row_hash from foldout.row_hashes where schema_name=_schema and table_name=_table and chunk_id=_chunk_id order by pk_hash
)
select coalesce(digest((string_agg(encode(row_hash,'hex'),'')::bytea),'sha256'), digest('', 'sha256')), count(*) from rows
$$;

create or replace function foldout.upsert_chunk_hash(_schema text, _table text, _chunk_id bigint, _pk_cols text[]) returns void language plpgsql as $$
declare h bytea; cnt int;
begin
  select chunk_hash, row_count into h, cnt from foldout.fold_chunk_hash(_schema,_table,_chunk_id,_pk_cols);
  insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count) values(_schema,_table,_chunk_id,coalesce(h,digest('', 'sha256')),coalesce(cnt,0)) on conflict(schema_name,table_name,chunk_id) do update set chunk_hash=excluded.chunk_hash,row_count=excluded.row_count;
end
$$;

create or replace function foldout.table_root_hash(_schema text, _table text) returns bytea language sql as $$
select coalesce(digest((string_agg(encode(chunk_hash,'hex'), '' order by chunk_id))::bytea,'sha256'), digest('', 'sha256')) from foldout.chunk_hashes where schema_name=_schema and table_name=_table
$$;

create or replace function foldout.database_root_hash() returns bytea language sql as $$
with t as (
  select schema_name, table_name, foldout.table_root_hash(schema_name, table_name) as h from foldout.chunk_hashes group by schema_name, table_name
)
select coalesce(digest((string_agg(encode(h,'hex'), '' order by schema_name, table_name))::bytea,'sha256'), digest('', 'sha256')) from t
$$;
"""

def conn_for(dbname):
    base = os.environ.get("FLD_DATABASE")
    if not base:
        raise RuntimeError("FLD_DATABASE is not set")
    u = urlparse(base)
    if u.scheme not in ("postgresql", "postgres"):
        raise RuntimeError("FLD_DATABASE must be a postgresql:// or postgres:// URL")
    dsn = urlunparse((u.scheme, u.netloc, "/" + dbname, u.params, u.query, u.fragment))
    return psycopg.connect(dsn)

def bootstrap(conn, chunk_rows):
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("set local max_parallel_workers_per_gather=4")
        cur.execute("select n.nspname, c.relname from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='r' and n.nspname not in ('pg_catalog','information_schema','foldout') and exists(select 1 from pg_constraint con where con.conrelid=c.oid and con.contype='p') order by 1,2")
        tables = cur.fetchall()
    for nsp, rel in tables:
        with conn.cursor() as cur:
            cur.execute("insert into foldout.hash_config(schema_name,table_name,chunk_rows) values(%s,%s,%s) on conflict(schema_name,table_name) do nothing", (nsp, rel, chunk_rows))
            cur.execute("select foldout.pk_tuple(%s,%s)", (nsp, rel))
            pk = cur.fetchone()[0]
            if not pk:
                continue
            cur.execute("set local synchronous_commit=off")
            cur.execute("select foldout.row_hashes_has_pk()")
            has_pk = cur.fetchone()[0]
            if has_pk:
                q = sql.SQL("""
                    insert into foldout.row_hashes(schema_name,table_name,pk,pk_hash,chunk_id,row_hash)
                    select %s,%s,(select jsonb_object_agg(k, row_to_json(t)::json->k) from unnest(foldout.pk_tuple(%s,%s)) k),
                           (select digest(string_agg(coalesce(row_to_json(t)::json->>k,'∅'), '' order by ord), 'sha256') from unnest(foldout.pk_tuple(%s,%s)) with ordinality as u(k,ord)),
                           foldout.compute_chunk_id(%s,%s, foldout.pk_tuple(%s,%s), conf.chunk_rows, t),
                           foldout.row_digest(t)
                    from {}.{} t cross join (select chunk_rows from foldout.hash_config where schema_name=%s and table_name=%s) conf on conflict do nothing
                """).format(sql.Identifier(nsp), sql.Identifier(rel))
                cur.execute(q, (nsp, rel, nsp, rel, nsp, rel, nsp, rel, nsp, rel, nsp, rel))
            else:
                q = sql.SQL("""
                    insert into foldout.row_hashes(schema_name,table_name,pk_hash,chunk_id,row_hash)
                    select %s,%s,(select digest(string_agg(coalesce(row_to_json(t)::json->>k,'∅'), '' order by ord), 'sha256') from unnest(foldout.pk_tuple(%s,%s)) with ordinality as u(k,ord)),
                           foldout.compute_chunk_id(%s,%s, foldout.pk_tuple(%s,%s), conf.chunk_rows, t),
                           foldout.row_digest(t)
                    from {}.{} t cross join (select chunk_rows from foldout.hash_config where schema_name=%s and table_name=%s) conf on conflict do nothing
                """).format(sql.Identifier(nsp), sql.Identifier(rel))
                cur.execute(q, (nsp, rel, nsp, rel, nsp, rel, nsp, rel, nsp, rel))
            q2 = sql.SQL("""
                with grp as (
                  select distinct chunk_id from foldout.row_hashes where schema_name=%s and table_name=%s
                )
                insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count)
                select %s,%s,g.chunk_id,f.chunk_hash,f.row_count from grp g cross join lateral foldout.fold_chunk_hash(%s,%s,g.chunk_id,foldout.pk_tuple(%s,%s)) f on conflict(schema_name,table_name,chunk_id) do update set chunk_hash=excluded.chunk_hash,row_count=excluded.row_count
            """)
            cur.execute(q2, (nsp, rel, nsp, rel, nsp, rel, nsp, rel))
        conn.commit()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("dbname")
    p.add_argument("--chunk-rows", type=int, default=int(os.environ.get("FLD_CHUNK_ROWS", "200000")))
    args = p.parse_args()
    with conn_for(args.dbname) as conn:
        bootstrap(conn, args.chunk_rows)
        with conn.cursor() as cur:
            cur.execute("select encode(foldout.database_root_hash(),'hex')")
            root = cur.fetchone()[0]
            print(root)

if __name__ == "__main__":
    main()
