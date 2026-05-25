import os
import argparse
from urllib.parse import urlparse, urlunparse
import psycopg
from psycopg import sql

def conn_for(dbname):
    base = os.environ.get("FLD_DATABASE")
    if not base:
        raise RuntimeError("FLD_DATABASE is not set")
    u = urlparse(base)
    if u.scheme not in ("postgresql", "postgres"):
        raise RuntimeError("FLD_DATABASE must be a postgresql:// or postgres:// URL")
    dsn = urlunparse((u.scheme, u.netloc, "/" + dbname, u.params, u.query, u.fragment))
    return psycopg.connect(dsn)

DDL = """
create extension if not exists pgcrypto;
create schema if not exists foldout;

create table if not exists foldout.hash_config(
  schema_name text not null,
  table_name text not null,
  chunk_rows int not null default 200000,
  primary key(schema_name, table_name)
);

-- Use UNLOGGED to reduce write amplification; data is derivable
create unlogged table if not exists foldout.row_hashes(
  schema_name text not null,
  table_name text not null,
  pk_hash bytea not null,
  chunk_id bigint not null,
  row_hash bytea not null,
  primary key(schema_name, table_name, pk_hash)
);

create index if not exists row_hashes_chunk_idx
  on foldout.row_hashes(schema_name, table_name, chunk_id);

-- Use UNLOGGED as it can be recomputed
create unlogged table if not exists foldout.chunk_hashes(
  schema_name text not null,
  table_name text not null,
  chunk_id bigint not null,
  chunk_hash bytea not null,
  row_count int not null,
  dirty boolean not null default false,
  primary key(schema_name, table_name, chunk_id)
);

-- Helper: does row_hashes have legacy pk column?
create or replace function foldout.row_hashes_has_pk() returns boolean
language sql stable as $$
  select exists (
    select 1 from information_schema.columns
    where table_schema='foldout' and table_name='row_hashes' and column_name='pk'
  )
$$;

create or replace function foldout.pk_tuple(_schema text, _table text) returns text[] language sql stable as $$
select array_agg(a.attname order by x.k)
from pg_constraint con
join pg_class c on c.oid=con.conrelid
join pg_namespace n on n.oid=c.relnamespace
join unnest(con.conkey) with ordinality as x(attnum,k) on true
join pg_attribute a on a.attrelid=c.oid and a.attnum=x.attnum
where con.contype='p' and n.nspname=_schema and c.relname=_table
$$;

create or replace function foldout.all_cols(_schema text, _table text) returns text[] language sql stable as $$
select array_agg(a.attname order by a.attnum)
from pg_attribute a
join pg_class c on a.attrelid=c.oid
join pg_namespace n on n.oid=c.relnamespace
where n.nspname=_schema and c.relname=_table and a.attnum>0 and not a.attisdropped
$$;

create or replace function foldout.row_digest(rec anyelement) returns bytea language sql immutable as $$
  select decode(md5(to_jsonb(rec)::text), 'hex')
$$;

-- XOR helpers and per-chunk state
create or replace function foldout.bxor(a bigint, b bigint) returns bigint language sql immutable as $$
  select a # b
$$;

do $$ begin
  perform 1 from pg_aggregate where aggfnoid::regprocedure::text like 'foldout.xor_agg%';
  if not found then
    create aggregate foldout.xor_agg(bigint) (sfunc = foldout.bxor, stype = bigint, initcond = '0');
  end if;
end $$;

create unlogged table if not exists foldout.chunk_state(
  schema_name text not null,
  table_name text not null,
  chunk_id bigint not null,
  xor64 bigint not null,
  row_count int not null,
  primary key(schema_name, table_name, chunk_id)
);

create or replace function foldout.compute_chunk_id(_schema text, _table text, pk_cols text[], chunk_rows int, rec record) returns bigint language plpgsql immutable as $$
declare i int; k text := ''; h bigint;
begin
  if pk_cols is null then return 0; end if;
  for i in 1..array_length(pk_cols,1) loop
    k := k || coalesce(to_jsonb(rec)->>pk_cols[i],'∅') || '␟';
  end loop;
  h := abs(hashtextextended(k, 0));
  return h / greatest(chunk_rows,1);
end
$$;

create or replace function foldout.fold_chunk_hash(_schema text, _table text, _chunk_id bigint, _pk_cols text[]) returns table(chunk_hash bytea, row_count int) language sql as $$
with rows as (
  select row_hash
  from foldout.row_hashes
  where schema_name=_schema and table_name=_table and chunk_id=_chunk_id
  order by pk_hash
)
select coalesce(digest((string_agg(encode(row_hash,'hex'),'')::bytea),'sha256'), digest('', 'sha256')), count(*) from rows
$$;

create or replace function foldout.upsert_chunk_hash(_schema text, _table text, _chunk_id bigint, _pk_cols text[]) returns void language plpgsql as $$
declare h bytea; cnt int;
begin
  select chunk_hash, row_count into h, cnt from foldout.fold_chunk_hash(_schema,_table,_chunk_id,_pk_cols);
  insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
  values(_schema,_table,_chunk_id,coalesce(h,digest('', 'sha256')),coalesce(cnt,0),false)
  on conflict(schema_name,table_name,chunk_id)
  do update set chunk_hash=excluded.chunk_hash,row_count=excluded.row_count,dirty=false;
end
$$;

create or replace function foldout.tg_row_hash() returns trigger language plpgsql as $$
declare pk_cols text[] := foldout.pk_tuple(TG_TABLE_SCHEMA, TG_TABLE_NAME);
declare conf int; old_pk_hash bytea; new_pk_hash bytea; old_cid bigint; new_cid bigint;
begin
  select chunk_rows into conf from foldout.hash_config where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME;
  if conf is null then return null; end if;
  if TG_OP='INSERT' then
    select digest(string_agg(coalesce(to_jsonb(NEW)::jsonb->>k,'∅'), '' order by ord), 'sha256') into new_pk_hash
    from unnest(pk_cols) with ordinality as u(k,ord);
    new_cid := foldout.compute_chunk_id(TG_TABLE_SCHEMA,TG_TABLE_NAME,pk_cols,conf,NEW);
    if foldout.row_hashes_has_pk() then
      insert into foldout.row_hashes(schema_name,table_name,pk,pk_hash,chunk_id,row_hash)
      values(
        TG_TABLE_SCHEMA,TG_TABLE_NAME,
        (select jsonb_object_agg(k, to_jsonb(NEW)::jsonb->k) from unnest(pk_cols) k),
        new_pk_hash,new_cid,foldout.row_digest(NEW))
      on conflict(schema_name,table_name,pk_hash) do update set pk=excluded.pk,chunk_id=excluded.chunk_id,row_hash=excluded.row_hash;
    else
      insert into foldout.row_hashes(schema_name,table_name,pk_hash,chunk_id,row_hash)
      values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_pk_hash,new_cid,foldout.row_digest(NEW))
      on conflict(schema_name,table_name,pk_hash) do update set chunk_id=excluded.chunk_id,row_hash=excluded.row_hash;
    end if;
    update foldout.chunk_hashes set dirty=true where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id=new_cid;
    return NEW;
  elsif TG_OP='UPDATE' then
    select digest(string_agg(coalesce(to_jsonb(OLD)::jsonb->>k,'∅'), '' order by ord), 'sha256') into old_pk_hash
    from unnest(pk_cols) with ordinality as u(k,ord);
    select digest(string_agg(coalesce(to_jsonb(NEW)::jsonb->>k,'∅'), '' order by ord), 'sha256') into new_pk_hash
    from unnest(pk_cols) with ordinality as u(k,ord);
    old_cid := foldout.compute_chunk_id(TG_TABLE_SCHEMA,TG_TABLE_NAME,pk_cols,conf,OLD);
    new_cid := foldout.compute_chunk_id(TG_TABLE_SCHEMA,TG_TABLE_NAME,pk_cols,conf,NEW);
    if old_pk_hash=new_pk_hash then
      update foldout.row_hashes set chunk_id=new_cid,row_hash=foldout.row_digest(NEW)
      where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and pk_hash=old_pk_hash;
    else
      delete from foldout.row_hashes where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and pk_hash=old_pk_hash;
      if foldout.row_hashes_has_pk() then
        insert into foldout.row_hashes(schema_name,table_name,pk,pk_hash,chunk_id,row_hash)
        values(
          TG_TABLE_SCHEMA,TG_TABLE_NAME,
          (select jsonb_object_agg(k, to_jsonb(NEW)::jsonb->k) from unnest(pk_cols) k),
          new_pk_hash,new_cid,foldout.row_digest(NEW));
      else
        insert into foldout.row_hashes(schema_name,table_name,pk_hash,chunk_id,row_hash)
        values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_pk_hash,new_cid,foldout.row_digest(NEW));
      end if;
    end if;
    update foldout.chunk_hashes set dirty=true where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id in (old_cid,new_cid);
    return NEW;
  else
    select digest(string_agg(coalesce(to_jsonb(OLD)::jsonb->>k,'∅'), '' order by ord), 'sha256') into old_pk_hash
    from unnest(pk_cols) with ordinality as u(k,ord);
    old_cid := foldout.compute_chunk_id(TG_TABLE_SCHEMA,TG_TABLE_NAME,pk_cols,conf,OLD);
    delete from foldout.row_hashes where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and pk_hash=old_pk_hash;
    update foldout.chunk_hashes set dirty=true where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id=old_cid;
    return OLD;
  end if;
end;
$$;

create or replace function foldout.rehash_dirty() returns table(schema_name text, table_name text, chunk_id bigint, chunk_hash bytea) language plpgsql as $$
declare r record; pk_cols text[];
begin
  for r in select * from foldout.chunk_hashes where dirty loop
    pk_cols := foldout.pk_tuple(r.schema_name, r.table_name);
    perform foldout.upsert_chunk_hash(r.schema_name, r.table_name, r.chunk_id, pk_cols);
    return query select r.schema_name, r.table_name, r.chunk_id, (select chunk_hash from foldout.chunk_hashes where schema_name=r.schema_name and table_name=r.table_name and chunk_id=r.chunk_id);
  end loop;
end
$$;

create or replace function foldout.table_root_hash(_schema text, _table text) returns bytea language sql as $$
select coalesce(
  digest((string_agg(encode(chunk_hash,'hex'), '' order by chunk_id))::bytea,'sha256'),
  digest('', 'sha256')
)
from foldout.chunk_hashes
where schema_name=_schema and table_name=_table
$$;

create or replace function foldout.database_root_hash() returns bytea language sql as $$
with t as (
  select schema_name, table_name, foldout.table_root_hash(schema_name, table_name) as h
  from foldout.chunk_hashes
  group by schema_name, table_name
)
select coalesce(
  digest((string_agg(encode(h,'hex'), '' order by schema_name, table_name))::bytea,'sha256'),
  digest('', 'sha256')
)
from t
$$;
"""

def bootstrap(conn, chunk_rows):
    with conn.cursor() as cur:
        cur.execute(DDL)
        cur.execute("""
            select n.nspname, c.relname
            from pg_class c join pg_namespace n on n.oid=c.relnamespace
            where c.relkind='r' and n.nspname not in ('pg_catalog','information_schema','foldout')
              and exists(select 1 from pg_constraint con where con.conrelid=c.oid and con.contype='p')
            order by 1,2
        """)
        tables = cur.fetchall()
    for nsp, rel in tables:
        with conn.cursor() as cur:
            cur.execute("insert into foldout.hash_config(schema_name,table_name,chunk_rows) values(%s,%s,%s) on conflict(schema_name,table_name) do nothing", (nsp, rel, chunk_rows))
            cur.execute("select foldout.pk_tuple(%s,%s)", (nsp, rel))
            pk = cur.fetchone()[0]
            if not pk:
                continue
            # Gather all column names for fast, JSON-free hashing
            cur.execute("select foldout.all_cols(%s,%s)", (nsp, rel))
            all_cols = cur.fetchone()[0]
            # Build concatenation expressions
            def join_cols(prefix, names):
                s = ""
                i = 0
                while i < len(names):
                    col = names[i]
                    part = f"coalesce({prefix}.{sql.Identifier(col).as_string(cur)}::text, '∅')"
                    if i > 0:
                        s = s + "||'␟'||" + part
                    else:
                        s = part
                    i = i + 1
                return s
            pk_concat_new = join_cols("NEW", pk)
            pk_concat_old = join_cols("OLD", pk)
            pk_concat_t = join_cols("t", pk)
            row_concat_new = join_cols("NEW", all_cols)
            row_concat_t = join_cols("t", all_cols)
            # Build per-table trigger function using 64-bit XOR state, no row_hashes
            fn_name = f"tg_chunk_state_{nsp}_{rel}".replace('"','').replace('.', '_').replace('-', '_')
            fn_sql = sql.SQL("""
                create or replace function foldout.{fn}() returns trigger language plpgsql as $$
                declare conf int; old_cid bigint; new_cid bigint; old_row64 bigint; new_row64 bigint; s record;
                begin
                  select chunk_rows into conf from foldout.hash_config where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME;
                  if conf is null then return null; end if;
                  if TG_OP='INSERT' then
                    new_row64 := hashtextextended(({row_new}), 0);
                    new_cid := abs(hashtextextended(({pk_new}), 0)) / greatest(conf,1);
                    insert into foldout.chunk_state(schema_name,table_name,chunk_id,xor64,row_count)
                    values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_cid,new_row64,1)
                    on conflict(schema_name,table_name,chunk_id)
                    do update set xor64=foldout.bxor(foldout.chunk_state.xor64, excluded.xor64), row_count=foldout.chunk_state.row_count+1
                    returning xor64, row_count into s;
                    insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                    values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_cid, decode(md5(s.xor64::text), 'hex'), s.row_count, false)
                    on conflict(schema_name,table_name,chunk_id)
                    do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false;
                    return NEW;
                  elsif TG_OP='UPDATE' then
                    old_row64 := hashtextextended(({row_old}), 0);
                    new_row64 := hashtextextended(({row_new}), 0);
                    old_cid := abs(hashtextextended(({pk_old}), 0)) / greatest(conf,1);
                    new_cid := abs(hashtextextended(({pk_new}), 0)) / greatest(conf,1);
                    if old_cid = new_cid then
                      update foldout.chunk_state set xor64=foldout.bxor(foldout.bxor(xor64, old_row64), new_row64)
                      where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id=new_cid
                      returning xor64, row_count into s;
                      insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                      values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_cid, decode(md5(s.xor64::text), 'hex'), s.row_count, false)
                      on conflict(schema_name,table_name,chunk_id)
                      do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false;
                    else
                      update foldout.chunk_state set xor64=foldout.bxor(xor64, old_row64), row_count=row_count-1
                      where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id=old_cid
                      returning xor64, row_count into s;
                      insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                      values(TG_TABLE_SCHEMA,TG_TABLE_NAME,old_cid, decode(md5(s.xor64::text), 'hex'), s.row_count, false)
                      on conflict(schema_name,table_name,chunk_id)
                      do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false;
                      insert into foldout.chunk_state(schema_name,table_name,chunk_id,xor64,row_count)
                      values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_cid,new_row64,1)
                      on conflict(schema_name,table_name,chunk_id)
                      do update set xor64=foldout.bxor(foldout.chunk_state.xor64, excluded.xor64), row_count=foldout.chunk_state.row_count+1
                      returning xor64, row_count into s;
                      insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                      values(TG_TABLE_SCHEMA,TG_TABLE_NAME,new_cid, decode(md5(s.xor64::text), 'hex'), s.row_count, false)
                      on conflict(schema_name,table_name,chunk_id)
                      do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false;
                    end if;
                    return NEW;
                  else
                    old_row64 := hashtextextended(({row_old}), 0);
                    old_cid := abs(hashtextextended(({pk_old}), 0)) / greatest(conf,1);
                    update foldout.chunk_state set xor64=foldout.bxor(xor64, old_row64), row_count=row_count-1
                    where schema_name=TG_TABLE_SCHEMA and table_name=TG_TABLE_NAME and chunk_id=old_cid
                    returning xor64, row_count into s;
                    insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                    values(TG_TABLE_SCHEMA,TG_TABLE_NAME,old_cid, decode(md5(s.xor64::text), 'hex'), s.row_count, false)
                    on conflict(schema_name,table_name,chunk_id)
                    do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false;
                    return OLD;
                  end if;
                end;
                $$;
            """).format(
                fn=sql.Identifier(fn_name),
                pk_new=sql.SQL(pk_concat_new),
                pk_old=sql.SQL(pk_concat_old),
                row_new=sql.SQL(row_concat_new),
                row_old=sql.SQL(row_concat_new.replace('NEW.','OLD.')),
            )
            cur.execute(fn_sql)
            # Install trigger using per-table function
            cur.execute(sql.SQL("drop trigger if exists foldout_row_hash_tg on {}.{};").format(sql.Identifier(nsp), sql.Identifier(rel)))
            cur.execute(sql.SQL("create trigger foldout_row_hash_tg after insert or update or delete on {}.{} for each row execute function foldout.{}()").format(sql.Identifier(nsp), sql.Identifier(rel), sql.Identifier(fn_name)))
            # Speed up bootstrap aggregation and compute chunk_state then chunk_hashes
            cur.execute("set local synchronous_commit=off")
            agg = sql.SQL("""
                with conf as (
                  select chunk_rows from foldout.hash_config where schema_name=%s and table_name=%s
                )
                insert into foldout.chunk_state(schema_name,table_name,chunk_id,xor64,row_count)
                select %s,%s,
                       abs(hashtextextended(({pk_t}), 0)) / greatest(conf.chunk_rows,1) as chunk_id,
                       foldout.xor_agg(hashtextextended(({row_t}), 0)) as xor64,
                       count(*) as row_count
                from {}.{} t, conf
                group by chunk_id
                on conflict(schema_name,table_name,chunk_id)
                do update set xor64=excluded.xor64, row_count=excluded.row_count
            """).format(
                sql.Identifier(nsp), sql.Identifier(rel),
                pk_t=sql.SQL(pk_concat_t),
                row_t=sql.SQL(row_concat_t),
            )
            cur.execute(agg, (nsp, rel, nsp, rel))
            fill = sql.SQL("""
                insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                select %s,%s, s.chunk_id, decode(md5(s.xor64::text), 'hex'), s.row_count, false
                from foldout.chunk_state s
                where schema_name=%s and table_name=%s
                on conflict(schema_name,table_name,chunk_id)
                do update set chunk_hash=excluded.chunk_hash, row_count=excluded.row_count, dirty=false
            """)
            cur.execute(fill, (nsp, rel, nsp, rel))
            q2 = sql.SQL("""
                with grp as (
                  select distinct chunk_id from foldout.row_hashes where schema_name=%s and table_name=%s
                )
                insert into foldout.chunk_hashes(schema_name,table_name,chunk_id,chunk_hash,row_count,dirty)
                select %s,%s, g.chunk_id, f.chunk_hash, f.row_count, false
                from grp g
                cross join lateral foldout.fold_chunk_hash(%s,%s, g.chunk_id, foldout.pk_tuple(%s,%s)) f
                on conflict(schema_name,table_name,chunk_id)
                do update set chunk_hash=excluded.chunk_hash,row_count=excluded.row_count,dirty=false
            """)
            cur.execute(q2, (nsp, rel, nsp, rel, nsp, rel, nsp, rel))
            cur.execute(sql.SQL("drop trigger if exists foldout_row_hash_tg on {}.{}").format(sql.Identifier(nsp), sql.Identifier(rel)))
            cur.execute(sql.SQL("create trigger foldout_row_hash_tg after insert or update or delete on {}.{} for each row execute function foldout.tg_row_hash()").format(sql.Identifier(nsp), sql.Identifier(rel)))
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
