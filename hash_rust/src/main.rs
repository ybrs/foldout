use std::{env, io::Read, thread, time::Instant};
use postgres::{Client, NoTls};
use blake3::Hasher;

fn pretty_bytes(b: u64) -> String {
    let g = 1024u64.pow(3);
    let m = 1024u64.pow(2);
    if b >= g { return format!("{:.2} GB", b as f64 / g as f64); }
    if b >= m { return format!("{:.2} MB", b as f64 / m as f64); }
    format!("{:.2} KB", b as f64 / 1024f64)
}

fn pretty_time(s: f64) -> String {
    if s < 1.0 { return format!("{:.0}ms", s * 1000.0); }
    if s < 60.0 { return format!("{:.2}s", s); }
    let m = (s / 60.0).floor() as u64;
    let sec = s - (m as f64)*60.0;
    format!("{}m{:0.0}s", m, sec)
}

fn list_user_tables_with_stats(client: &mut Client) -> Vec<(String,String,i64,i64)> {
    let rows = client.query(
        "select n.nspname, c.relname, greatest(c.reltuples,0)::bigint, pg_total_relation_size(c.oid)
         from pg_class c
         join pg_namespace n on n.oid = c.relnamespace
         where c.relkind = 'r'
           and n.nspname not in ('pg_catalog','information_schema')
         order by n.nspname, c.relname", &[]).unwrap();
    rows.into_iter().map(|r| {
        (r.get::<_,String>(0), r.get::<_,String>(1), r.get::<_,i64>(2), r.get::<_,i64>(3))
    }).collect()
}

fn list_columns(client: &mut Client, schema: &str, table: &str) -> Vec<String> {
    let rows = client.query(
        "select column_name
         from information_schema.columns
         where table_schema = $1 and table_name = $2
         order by ordinal_position", &[&schema, &table]).unwrap();
    rows.into_iter().map(|r| r.get::<_,String>(0)).collect()
}

fn list_pk_columns(client: &mut Client, schema: &str, table: &str) -> Vec<String> {
    let rows = client.query(
        "select a.attname
         from pg_index i
         join pg_class c on c.oid = i.indrelid
         join pg_namespace n on n.oid = c.relnamespace
         join pg_attribute a on a.attrelid = c.oid and a.attnum = any(i.indkey)
         where i.indisprimary
           and n.nspname = $1 and c.relname = $2
         order by array_position(i.indkey, a.attnum)", &[&schema, &table]).unwrap();
    rows.into_iter().map(|r| r.get::<_,String>(0)).collect()
}

fn db_total_bytes(client: &mut Client) -> u64 {
    let row = client.query_one("select pg_database_size(current_database())", &[]).unwrap();
    row.get::<_,i64>(0) as u64
}

fn table_estimates(client: &mut Client, schema: &str, table: &str) -> (u64,u64) {
    let row = client.query_one(
        "select greatest(c.reltuples,0)::bigint, pg_total_relation_size(c.oid)
         from pg_class c join pg_namespace n on n.oid=c.relnamespace
         where n.nspname=$1 and c.relname=$2", &[&schema, &table]).unwrap();
    (row.get::<_,i64>(0) as u64, row.get::<_,i64>(1) as u64)
}


fn digest_table(client: &mut Client, schema: &str, table: &str) -> (String,u64,f64) {
    let interval = std::env::var("VKA_BW_INTERVAL").ok().and_then(|s| s.parse::<u64>().ok()).unwrap_or(0);
    let cols = list_columns(client, schema, table);
    if cols.is_empty() { return (String::new(), 0, 0.0); }
    let pk = list_pk_columns(client, schema, table);
    let select_list = cols.iter().map(|c| format!("\"{}\"", c.replace('"', "\"\""))).collect::<Vec<_>>().join(", ");
    let order_by = if !pk.is_empty() {
        pk.iter().map(|c| format!("\"{}\"", c.replace('"', "\"\""))).collect::<Vec<_>>().join(", ")
    } else {
        cols.iter().map(|c| format!("\"{}\"", c.replace('"', "\"\""))).collect::<Vec<_>>().join(", ")
    };
    // let sql = format!("COPY (SELECT {} FROM \"{}\".\"{}\" ORDER BY {}) TO STDOUT (FORMAT binary)",
    //                   select_list, schema.replace('"', "\"\""), table.replace('"', "\"\""), order_by);

    let sql = format!("COPY (SELECT {} FROM \"{}\".\"{}\" ) TO STDOUT (FORMAT binary)",
                      select_list, schema.replace('"', "\"\""), table.replace('"', "\"\""));

    let mut reader = client.copy_out(sql.as_str()).unwrap();
    let mut hasher = blake3::Hasher::new();
    let start_wall = std::time::Instant::now();
    let mut buf = [0u8; 1<<20];
    let mut streamed: u64 = 0;
    let mut read_time_total: f64 = 0.0;
    let mut last_tick = std::time::Instant::now();
    let mut last_bytes: u64 = 0;
    let mut read_time_since_last: f64 = 0.0;
    loop {
        let t0 = std::time::Instant::now();
        let n = match reader.read(&mut buf) {
            Ok(0) => 0,
            Ok(n) => n,
            Err(_) => 0,
        };
        let rd = t0.elapsed().as_secs_f64();
        if n == 0 { break; }
        hasher.update(&buf[..n]);
        streamed += n as u64;
        read_time_total += rd;
        read_time_since_last += rd;
        if interval > 0 && last_tick.elapsed().as_secs_f64() >= interval as f64 {
            let delta_bytes = streamed - last_bytes;
            let inst_rate = if read_time_since_last > 0.0 { (delta_bytes as f64 / read_time_since_last) as u64 } else { 0 };
            let avg_rate = if read_time_total > 0.0 { (streamed as f64 / read_time_total) as u64 } else { 0 };
            let t = start_wall.elapsed().as_secs_f64();
            println!(
                "PG {}.{} t={:.1}s inst {}/s avg {}/s total {}",
                schema, table, t, pretty_bytes(inst_rate), pretty_bytes(avg_rate), pretty_bytes(streamed)
            );
            last_tick = std::time::Instant::now();
            last_bytes = streamed;
            read_time_since_last = 0.0;
        }
    }
    let dt = start_wall.elapsed().as_secs_f64();
    (hasher.finalize().to_hex().to_string(), streamed, dt)
}


fn partition_round_robin<T: Clone>(v: &[T], k: usize) -> Vec<Vec<T>> {
    let mut parts = vec![Vec::new(); k];
    for (i, item) in v.iter().enumerate() {
        parts[i % k].push(item.clone());
    }
    parts
}

fn partition_lpt(v: &[(String,String,i64,i64)], k: usize) -> Vec<Vec<(String,String,i64,i64)>> {
    let mut sorted: Vec<_> = v.to_vec();
    sorted.sort_by_key(|t| std::cmp::Reverse(t.3.max(0)));
    let mut parts: Vec<Vec<(String,String,i64,i64)>> = vec![Vec::new(); k];
    let mut loads: Vec<i64> = vec![0; k];
    for item in sorted {
        let (idx, _) = loads.iter().enumerate().min_by_key(|(_,l)| *l).unwrap();
        loads[idx] += item.3.max(0);
        parts[idx].push(item);
    }
    parts
}

fn main() {
    if let Ok(t) = env::var("VKA_HASH_THREADS") {
        if let Ok(n) = t.parse::<usize>() {
            std::env::set_var("RAYON_NUM_THREADS", n.to_string());
        }
    }

    let dsn = env::var("VKA_DATABASE").expect("VKA_DATABASE");
    let workers: usize = env::var("VKA_HASH_WORKERS").ok().and_then(|s| s.parse().ok()).unwrap_or(1);
    let args: Vec<String> = env::args().collect();
    let table_arg = if args.len() > 1 { Some(args[1].clone()) } else { None };

    if let Some(tname) = table_arg {
        let (schema, table) = if tname.contains('.') {
            let mut it = tname.splitn(2, '.');
            (it.next().unwrap().to_string(), it.next().unwrap().to_string())
        } else { ("public".to_string(), tname) };
        let mut client = Client::connect(&dsn, NoTls).unwrap();
        let db_size = db_total_bytes(&mut client);
        let (est_rows, total_b) = table_estimates(&mut client, &schema, &table);
        let t0 = Instant::now();
        let (digest, streamed, dt) = digest_table(&mut client, &schema, &table);
        let spent = t0.elapsed().as_secs_f64();
        let rate = if dt > 0.0 { (streamed as f64 / dt) as u64 } else { 0 };
        println!("{}.{} {} size {} rows~{} took {} rate {}/s", schema, table, digest, pretty_bytes(total_b), est_rows, pretty_time(dt), pretty_bytes(rate));
        println!("SUMMARY tables=1 set_size={} db_size={} rows~{} took {}", pretty_bytes(total_b), pretty_bytes(db_size), est_rows, pretty_time(spent));
        return;
    }

    let mut client = Client::connect(&dsn, NoTls).unwrap();
    let tables = list_user_tables_with_stats(&mut client);
    let total_rows: u64 = tables.iter().map(|t| t.2.max(0) as u64).sum();
    let total_bytes: u64 = tables.iter().map(|t| t.3.max(0) as u64).sum();
    let db_size = db_total_bytes(&mut client);
    drop(client);

    let k = if workers == 0 { 1 } else { workers };
    let use_lpt = env::var("VKA_HASH_LPT").ok().map(|s| s != "0").unwrap_or(true);
    let parts = if use_lpt { partition_lpt(&tables, k) } else { partition_round_robin(&tables, k) };
    let start = Instant::now();

    let mut handles = Vec::new();
    for part in parts {
        let dsn_clone = dsn.clone();
        let handle = thread::spawn(move || {
            let mut client = Client::connect(&dsn_clone, NoTls).unwrap();
            let mut bytes_done: u64 = 0;
            let mut rows_done: u64 = 0;
            let mut spent_local: f64 = 0.0;
            for (schema, table, est_rows_i64, total_b_i64) in part {
                let est_rows = est_rows_i64.max(0) as u64;
                let total_b = total_b_i64.max(0) as u64;
                let t0 = Instant::now();
                let (digest, streamed, dt) = digest_table(&mut client, &schema, &table);
                let rate = if dt > 0.0 { (streamed as f64 / dt) as u64 } else { 0 };
                let bytes_pct = if total_bytes > 0 { (total_b as f64 / total_bytes as f64) * 100.0 } else { 0.0 };
                let rows_pct = if total_rows > 0 { (est_rows as f64 / total_rows as f64) * 100.0 } else { 0.0 };
                println!("{}.{} {} size {} ({:.2}% of set) rows~{} ({:.2}% of set) took {} rate {}/s",
                         schema, table, digest, pretty_bytes(total_b), bytes_pct, est_rows, rows_pct, pretty_time(dt), pretty_bytes(rate));
                bytes_done += total_b;
                rows_done += est_rows;
                spent_local += t0.elapsed().as_secs_f64();
            }
            (bytes_done, rows_done, spent_local)
        });
        handles.push(handle);
    }

    let mut sum_bytes: u64 = 0;
    let mut sum_rows: u64 = 0;
    let mut sum_spent: f64 = 0.0;
    for h in handles {
        let (b, r, s) = h.join().unwrap();
        sum_bytes += b;
        sum_rows += r;
        sum_spent += s;
    }
    let wall = start.elapsed().as_secs_f64();
    let set_size = if total_bytes > 0 { total_bytes } else { sum_bytes };
    println!("SUMMARY tables={} set_size={} db_size={} rows~{} took {}",
             tables.len(), pretty_bytes(set_size), pretty_bytes(db_size), sum_rows, pretty_time(wall));
}
