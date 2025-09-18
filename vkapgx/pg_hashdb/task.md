We have this extension to hash/digest a table.

we build the extension with these

```
PG17_PGCONFIG="$(brew --prefix postgresql@17)/bin/pg_config"
cargo pgrx init --pg17 "$PG17_PGCONFIG"

cargo pgrx package --pg-config "$PG17_PGCONFIG"

```

and installed with this 

```
# IMPORTANT: build explicitly for PG17 to avoid ABI crashes
cargo pgrx install --release --pg-config "$PG17_PGCONFIG" --no-default-features --features pg17
```

and when we run it with 

```
psql coinleverprod --user aybarsb
```


```
coinleverprod=# \timing
Timing is on.
coinleverprod=# SELECT vkar_hash_table('public.runtime_commandrunhistory'::regclass, 10000);
 27626b9e17bdeee99e6005f670aa5a7a5d3cb5d2a957476466c6d671ab2776a1

Time: 29284.050 ms (00:29.284)
```

Task
- I need you to make this faster. 


for example we are doing this 

```
            // Build the base query and open a SPI cursor (Portal) using the C API
            let select_sql = format!(
                "select to_jsonb(t) from \"{}\".\"{}\" t",
                nsp.replace('"', "\"\""),
                rel.replace('"', "\"\"")
            );
            let select_c = CString::new(select_sql).unwrap();

```

I'd assume converting to jsonb in each row would take great amount of time. we can use any binary/safe encoding.

Also think of anything that can improve performance for this. 
