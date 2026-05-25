-- foldout change capture and DDL audit installation
-- Creates schema, tables, helpers, triggers, and event triggers.

CREATE SCHEMA IF NOT EXISTS foldout;

CREATE TABLE IF NOT EXISTS foldout.change_log (
  id bigserial PRIMARY KEY,
  rel regclass NOT NULL,
  op char(1) NOT NULL,
  key jsonb NOT NULL,
  cols jsonb,
  tx xid8 NOT NULL DEFAULT pg_current_xact_id(),
  ts timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS foldout.ddl_log (
  id bigserial PRIMARY KEY,
  ts timestamptz NOT NULL DEFAULT clock_timestamp(),
  username text NOT NULL DEFAULT current_user,
  dbname name NOT NULL DEFAULT current_database(),
  tx xid8 NOT NULL DEFAULT pg_current_xact_id(),
  command_tag text NOT NULL,
  object_type text,
  schema_name text,
  object_identity text,
  phase text NOT NULL,
  sql_text text,
  pre_def text,
  post_def text
);

-- Helpers for PK discovery and key serialization
CREATE OR REPLACE FUNCTION foldout.pk_names(rel regclass) RETURNS text[]
LANGUAGE sql STABLE AS $$
SELECT array_agg(a.attname ORDER BY a.attnum)
FROM pg_index i
JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
WHERE i.indrelid=$1 AND i.indisprimary
$$;

CREATE OR REPLACE FUNCTION foldout.pk_json(rel regclass, rec anyelement) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
  j jsonb := '{}'::jsonb;
  att smallint;
  name name;
  v jsonb;
BEGIN
  FOR att IN SELECT unnest(i.indkey) FROM pg_index i WHERE i.indrelid=rel AND i.indisprimary LOOP
    SELECT a.attname INTO name FROM pg_attribute a WHERE a.attrelid=rel AND a.attnum=att AND NOT a.attisdropped;
    EXECUTE format('SELECT to_jsonb(($1).%I)', name) INTO v USING rec;
    j := j || jsonb_build_object(name, v);
  END LOOP;
  IF j = '{}'::jsonb THEN RAISE EXCEPTION 'no primary key on %', rel; END IF;
  RETURN j;
END$$;

-- DML capture trigger (type-aware)
CREATE OR REPLACE FUNCTION foldout.capture() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,foldout AS $$
DECLARE
  keys jsonb;
  delta jsonb := '{}'::jsonb;
  col record;
  pkcols text[] := foldout.pk_names(TG_RELID::regclass);
  v_new text;
  v_old text;
  entry jsonb;
BEGIN
  IF TG_OP='INSERT' THEN
    keys := foldout.pk_json(TG_RELID::regclass, NEW);
    FOR col IN
      SELECT a.attname AS name, a.atttypid AS toid, a.atttypmod AS m, a.attnum
      FROM pg_attribute a
      WHERE a.attrelid=TG_RELID AND a.attnum>0 AND NOT a.attisdropped
    LOOP
      EXECUTE format('SELECT ($1).%I::text', col.name) INTO v_new USING NEW;
      entry := jsonb_build_object('toid', col.toid, 'm', col.m, 'v', v_new);
      delta := delta || jsonb_build_object(col.name, entry);
    END LOOP;
    INSERT INTO foldout.change_log(rel,op,key,cols) VALUES (TG_RELID::regclass,'I',keys,delta);
    RETURN NEW;
  ELSIF TG_OP='UPDATE' THEN
    keys := foldout.pk_json(TG_RELID::regclass, NEW);
    FOR col IN
      SELECT a.attname AS name, a.atttypid AS toid, a.atttypmod AS m, a.attnum
      FROM pg_attribute a
      WHERE a.attrelid=TG_RELID AND a.attnum>0 AND NOT a.attisdropped
    LOOP
      IF col.name = ANY(pkcols) THEN
        EXECUTE format('SELECT ($1).%I::text', col.name) INTO v_new USING NEW;
        EXECUTE format('SELECT ($1).%I::text', col.name) INTO v_old USING OLD;
        IF v_new IS DISTINCT FROM v_old THEN
          entry := jsonb_build_object('toid', col.toid, 'm', col.m, 'v', v_new);
          delta := delta || jsonb_build_object(col.name, entry);
        END IF;
      ELSE
        EXECUTE format('SELECT ($1).%I::text', col.name) INTO v_new USING NEW;
        EXECUTE format('SELECT ($1).%I::text', col.name) INTO v_old USING OLD;
        IF v_new IS DISTINCT FROM v_old THEN
          entry := jsonb_build_object('toid', col.toid, 'm', col.m, 'v', v_new);
          delta := delta || jsonb_build_object(col.name, entry);
        END IF;
      END IF;
    END LOOP;
    IF delta <> '{}'::jsonb THEN
      INSERT INTO foldout.change_log(rel,op,key,cols) VALUES (TG_RELID::regclass,'U',keys,delta);
    END IF;
    RETURN NEW;
  ELSIF TG_OP='DELETE' THEN
    keys := foldout.pk_json(TG_RELID::regclass, OLD);
    INSERT INTO foldout.change_log(rel,op,key,cols) VALUES (TG_RELID::regclass,'D',keys,NULL);
    RETURN OLD;
  END IF;
END$$;

-- Apply a single log row to the base table
CREATE OR REPLACE FUNCTION foldout.apply_row(log_id bigint) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  r record;
  k text;
  setlist text := '';
  coltype text;
  q text;
  first boolean := true;
BEGIN
  SELECT * INTO r FROM foldout.change_log WHERE id=log_id;
  IF r.op='I' THEN
    q := 'INSERT INTO '||r.rel||'('||
         (SELECT string_agg(quote_ident(k), ',') FROM jsonb_object_keys(r.cols) t(k))||
         ') VALUES ('||
         (
           SELECT string_agg(
                    CASE
                      WHEN (r.cols->k->>'v') IS NULL THEN 'NULL'
                      ELSE format('(%L)::%s', r.cols->k->>'v', format_type((r.cols->k->>'toid')::oid, (r.cols->k->>'m')::int))
                    END, ',')
           FROM jsonb_object_keys(r.cols) t(k)
         )||')';
    EXECUTE q;
  ELSIF r.op='U' THEN
    IF r.cols IS NULL THEN RETURN; END IF;
    FOR k IN SELECT key FROM jsonb_object_keys(r.cols) LOOP
      coltype := format_type((r.cols->k->>'toid')::oid, (r.cols->k->>'m')::int);
      IF NOT first THEN setlist := setlist||', '; END IF;
      setlist := setlist||quote_ident(k)||' = '||
                 CASE WHEN (r.cols->k->>'v') IS NULL THEN 'NULL'
                      ELSE format('(%L)::%s', r.cols->k->>'v', coltype) END;
      first := false;
    END LOOP;
    q := 'UPDATE '||r.rel||' SET '||setlist||' WHERE ('||
         (SELECT string_agg(quote_ident(k), ',') FROM jsonb_object_keys(r.key) t(k))||
         ') = ('||
         (SELECT string_agg(
                   CASE WHEN r.key->>k IS NULL THEN 'NULL'
                        ELSE format('%L', r.key->>k) END, ',')
          FROM jsonb_object_keys(r.key) t(k))||
         ')';
    EXECUTE q;
  ELSIF r.op='D' THEN
    q := 'DELETE FROM '||r.rel||' WHERE ('||
         (SELECT string_agg(quote_ident(k), ',') FROM jsonb_object_keys(r.key) t(k))||
         ') = ('||
         (SELECT string_agg(
                   CASE WHEN r.key->>k IS NULL THEN 'NULL'
                        ELSE format('%L', r.key->>k) END, ',')
          FROM jsonb_object_keys(r.key) t(k))||
         ')';
    EXECUTE q;
  END IF;
END$$;

-- Install row triggers on existing user tables that have a PK
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT c.oid::regclass AS rel
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE c.relkind='r'
      AND n.nspname NOT IN ('pg_catalog','information_schema','foldout')
      AND EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid AND i.indisprimary)
  LOOP
    EXECUTE format('DROP TRIGGER IF EXISTS foldout_row ON %s', r.rel);
    EXECUTE format('CREATE TRIGGER foldout_row AFTER INSERT OR UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION foldout.capture()', r.rel);
  END LOOP;
END$$;

-- Auto-install triggers for new tables with a PK
CREATE OR REPLACE FUNCTION foldout.install_trigger_for(rel regclass) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_index i WHERE i.indrelid=rel AND i.indisprimary) THEN
    EXECUTE format('DROP TRIGGER IF EXISTS foldout_row ON %s', rel);
    EXECUTE format('CREATE TRIGGER foldout_row AFTER INSERT OR UPDATE OR DELETE ON %s FOR EACH ROW EXECUTE FUNCTION foldout.capture()', rel);
  END IF;
END$$;

CREATE OR REPLACE FUNCTION foldout.on_ddl_end_install() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    IF r.schema_name NOT IN ('pg_catalog','information_schema','foldout') THEN
      IF r.object_type IN ('table','partitioned table','table partition') THEN
        PERFORM foldout.install_trigger_for(r.objid::regclass);
      END IF;
    END IF;
  END LOOP;
END$$;

DROP EVENT TRIGGER IF EXISTS foldout_ddl_end_install;
CREATE EVENT TRIGGER foldout_ddl_end_install ON ddl_command_end EXECUTE FUNCTION foldout.on_ddl_end_install();

-- DDL auditing with timestamps
CREATE OR REPLACE FUNCTION foldout.ddl_start() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    INSERT INTO foldout.ddl_log(command_tag,object_type,schema_name,object_identity,phase,pre_def)
    VALUES (
      r.command_tag,r.object_type,r.schema_name,r.object_identity,'start',
      CASE r.object_type
        WHEN 'view' THEN pg_get_viewdef(r.objid,true)
        WHEN 'materialized view' THEN pg_get_viewdef(r.objid,true)
        WHEN 'function' THEN pg_get_functiondef(r.objid)
        WHEN 'index' THEN pg_get_indexdef(r.objid)
        ELSE NULL
      END
    );
  END LOOP;
END$$;


-- TODO: this does not understand alter table 
CREATE OR REPLACE FUNCTION foldout.render_create_table_full(rel regclass) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
  rel_oid oid := rel::oid;
  relname text := rel::regclass::text;
  is_partitioned boolean;
  parent_list text := '';
  partdef text := NULL;
  tablespace_name text := NULL;
  withopts text := NULL;
  cols text := '';
  first boolean := true;
  r record;
  constr text := '';
  conrec record;
BEGIN
  SELECT (cls.relkind='p') AS is_partitioned,
         pg_get_partkeydef(cls.oid),
         CASE WHEN cls.reltablespace <> 0 THEN (SELECT spcname FROM pg_tablespace t WHERE t.oid=cls.reltablespace) END,
         CASE WHEN cls.reloptions IS NOT NULL THEN 'WITH ('||array_to_string(cls.reloptions, ', ')||')' END
  INTO is_partitioned, partdef, tablespace_name, withopts
  FROM pg_class AS cls
  WHERE cls.oid=rel_oid;

  SELECT string_agg(i.inhparent::regclass::text, ', ')
  INTO parent_list
  FROM pg_inherits i
  WHERE i.inhrelid=rel_oid;

  FOR r IN
    SELECT
      a.attname AS name,
      format_type(a.atttypid, a.atttypmod) AS typ,
      a.attnotnull AS notnull,
      a.attidentity AS ident,
      a.attgenerated AS attgen,
      CASE WHEN a.attcollation <> t.typcollation THEN quote_ident(co.collname) ELSE NULL END AS coll,
      (SELECT pg_get_expr(ad.adbin, ad.adrelid) FROM pg_attrdef ad WHERE ad.adrelid=a.attrelid AND ad.adnum=a.attnum) AS adexpr
    FROM pg_attribute a
    JOIN pg_type t ON t.oid=a.atttypid
    LEFT JOIN pg_collation co ON co.oid=a.attcollation
    WHERE a.attrelid=rel_oid AND a.attnum>0 AND NOT a.attisdropped
    ORDER BY a.attnum
  LOOP
    IF NOT first THEN cols := cols||', '; END IF;
    cols := cols||quote_ident(r.name)||' '||r.typ||
            CASE WHEN r.coll IS NOT NULL THEN ' COLLATE '||r.coll ELSE '' END||
            CASE
              WHEN r.attgen='s' THEN ' GENERATED ALWAYS AS ('||r.adexpr||') STORED'
              WHEN r.ident IN ('a','d') THEN
                ' GENERATED '||CASE WHEN r.ident='a' THEN 'ALWAYS' ELSE 'BY DEFAULT' END||' AS IDENTITY'
              WHEN r.adexpr IS NOT NULL THEN ' DEFAULT '||r.adexpr
              ELSE '' END||
            CASE WHEN r.notnull THEN ' NOT NULL' ELSE '' END;
    first := false;
  END LOOP;

  FOR conrec IN
    SELECT conname, contype, condeferrable, condeferred, pg_get_constraintdef(oid, true) AS def
    FROM pg_constraint
    WHERE conrelid=rel_oid
    ORDER BY conindid::text, conname
  LOOP
    IF constr <> '' THEN constr := constr||', '; END IF;
    constr := constr||'CONSTRAINT '||quote_ident(conrec.conname)||' '||conrec.def;
  END LOOP;

  RETURN
    'CREATE TABLE '||
    relname||' ('||
    cols||
    CASE WHEN constr<>'' THEN ', '||constr ELSE '' END||
    ')'||
    CASE WHEN parent_list IS NOT NULL THEN ' INHERITS ('||parent_list||')' ELSE '' END||
    CASE WHEN partdef IS NOT NULL THEN ' PARTITION BY '||partdef ELSE '' END||
    CASE WHEN withopts IS NOT NULL THEN ' '||withopts ELSE '' END||
    CASE WHEN tablespace_name IS NOT NULL THEN ' TABLESPACE '||quote_ident(tablespace_name) ELSE '' END;
END$$;

CREATE OR REPLACE FUNCTION foldout.ddl_end() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE
  r record;
  v_sql_text text;
  rel_oid oid;
  relname_txt text;
  unquoted_name text;
  is_quoted boolean;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
    v_sql_text := NULL;

    IF r.object_type IN ('table','partitioned table','table partition') THEN
      rel_oid := NULL;

      IF r.objid <> 0 THEN
        rel_oid := r.objid;
      END IF;

      IF rel_oid IS NULL THEN
        relname_txt := r.object_identity;
        IF position('.' IN relname_txt) > 0 THEN
          relname_txt := split_part(relname_txt, '.', 2);
        END IF;
        is_quoted := left(relname_txt,1)='"' AND right(relname_txt,1)='"';
        IF is_quoted THEN
          unquoted_name := substring(relname_txt FROM 2 FOR char_length(relname_txt)-2);
          SELECT c.oid INTO rel_oid
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname = r.schema_name
            AND c.relname = unquoted_name
          LIMIT 1;
        ELSE
          SELECT c.oid INTO rel_oid
          FROM pg_class c
          JOIN pg_namespace n ON n.oid=c.relnamespace
          WHERE n.nspname = r.schema_name
            AND c.relname = lower(relname_txt)
          LIMIT 1;
        END IF;
      END IF;

      -- For CREATE TABLE commands, generate the full table definition
      -- For ALTER TABLE commands, use current_query() to get the actual ALTER statement
      IF rel_oid IS NOT NULL THEN
        IF r.command_tag = 'CREATE TABLE' THEN
          v_sql_text := foldout.render_create_table_full(rel_oid::regclass);
        ELSE
          -- For ALTER TABLE and other table commands, capture the actual SQL
          v_sql_text := current_query();
        END IF;
      END IF;
    ELSIF r.object_type IN ('view','materialized view') THEN
      v_sql_text := pg_get_viewdef(r.objid,true);
    ELSIF r.object_type='function' THEN
      v_sql_text := pg_get_functiondef(r.objid);
    ELSIF r.object_type='index' THEN
      v_sql_text := pg_get_indexdef(r.objid);
    END IF;

    INSERT INTO foldout.ddl_log(
      command_tag, object_type, schema_name, object_identity, phase, post_def, sql_text
    )
    VALUES (
      r.command_tag,
      r.object_type,
      r.schema_name,
      r.object_identity,
      'end',
      CASE r.object_type
        WHEN 'view' THEN pg_get_viewdef(r.objid,true)
        WHEN 'materialized view' THEN pg_get_viewdef(r.objid,true)
        WHEN 'function' THEN pg_get_functiondef(r.objid)
        WHEN 'index' THEN pg_get_indexdef(r.objid)
        ELSE NULL
      END,
      v_sql_text
    );
  END LOOP;
END$$;





CREATE OR REPLACE FUNCTION foldout.on_table_rewrite() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE r record;
BEGIN
  FOR r IN SELECT * FROM pg_event_trigger_table_rewrite() LOOP
    INSERT INTO foldout.ddl_log(command_tag,object_type,object_identity,phase)
    VALUES ('TABLE REWRITE','table', r.relid::regclass::text, 'end');
  END LOOP;
END$$;

-- Handle DROP commands via sql_drop event
CREATE OR REPLACE FUNCTION foldout.on_sql_drop() RETURNS event_trigger
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE 
  r record;
  v_sql_text text;
BEGIN
  -- Get the current query that triggered the drop
  v_sql_text := current_query();
  
  FOR r IN SELECT * FROM pg_event_trigger_dropped_objects() LOOP
    -- Only log table drops (and related types that get dropped with tables)
    IF r.object_type IN ('table', 'partitioned table', 'table partition') THEN
      INSERT INTO foldout.ddl_log(
        command_tag, 
        object_type, 
        schema_name, 
        object_identity, 
        phase, 
        sql_text
      ) VALUES (
        'DROP TABLE',
        r.object_type,
        r.schema_name,
        r.object_identity,
        'end',
        v_sql_text
      );
    END IF;
  END LOOP;
END$$;

DROP EVENT TRIGGER IF EXISTS foldout_ddl_start;
CREATE EVENT TRIGGER foldout_ddl_start ON ddl_command_start EXECUTE FUNCTION foldout.ddl_start();

DROP EVENT TRIGGER IF EXISTS foldout_ddl_end;
CREATE EVENT TRIGGER foldout_ddl_end ON ddl_command_end EXECUTE FUNCTION foldout.ddl_end();

DROP EVENT TRIGGER IF EXISTS foldout_table_rewrite;
CREATE EVENT TRIGGER foldout_table_rewrite ON table_rewrite EXECUTE FUNCTION foldout.on_table_rewrite();

DROP EVENT TRIGGER IF EXISTS foldout_sql_drop;
CREATE EVENT TRIGGER foldout_sql_drop ON sql_drop EXECUTE FUNCTION foldout.on_sql_drop();
