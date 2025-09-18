use pgrx::prelude::*;
use pgrx::pg_sys;
use blake3::Hasher;
use std::ffi::CString;

pgrx::pg_module_magic!();

fn hex16(x: u128) -> String {
    let mut s = [0u8; 32];
    let mut v = x;
    for i in (0..32).rev() {
        let d = (v & 0xF) as u8;
        s[i] = if d < 10 { b'0' + d } else { b'a' + (d - 10) };
        v >>= 4;
    }
    unsafe { String::from_utf8_unchecked(s.to_vec()) }
}

// Uses SPI to read table contents; must be VOLATILE and PARALLEL UNSAFE.
#[pg_extern(volatile, strict, parallel_unsafe)]
fn vkar_hash_table(reg: pg_sys::Oid, batch_rows: i32) -> String {
    const KEY: [u8; 32] = [
        b'v', b'k', b'a', b'r',
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ];

    let mut s1: u128 = 0;
    let mut s2: u128 = 0;
    let mut n: u64 = 0;

    unsafe {
        // Connect to SPI and check for basic failure modes.
        let rc_spi = pg_sys::SPI_connect();
        if rc_spi < 0 {
            pgrx::warning!("pg_hashdb: SPI_connect failed (rc={})", rc_spi);
            return String::new();
        }

        // Resolve schema and relation name from OID
        let oid_u32: u32 = reg.into();
        // Cast NAME fields to TEXT to avoid mis-decoding and potential segfaults
        // when converting Datum -> String (NAME is fixed-length, not varlena).
        let nameq = format!(
            "select n.nspname::text, c.relname::text from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.oid = {}",
            oid_u32
        );
        let nameq_c = CString::new(nameq).unwrap();
        let rc = pg_sys::SPI_execute(nameq_c.as_ptr(), true, 1);
        if rc == pg_sys::SPI_OK_SELECT as i32 && pg_sys::SPI_processed > 0 {
            let tt = pg_sys::SPI_tuptable;
            if tt.is_null() {
                pgrx::warning!("pg_hashdb: SPI_tuptable was NULL after name lookup");
                pg_sys::SPI_finish();
                return String::new();
            }
            let tupdesc = (*tt).tupdesc;
            let vals = (*tt).vals;
            let htup = *vals.add(0);
            let mut isnull = false;
            let nsp_d = pg_sys::SPI_getbinval(htup, tupdesc, 1, &mut isnull);
            let rel_d = pg_sys::SPI_getbinval(htup, tupdesc, 2, &mut isnull);
            let nsp = match String::from_datum(nsp_d, false) {
                Some(s) => String::from(s),
                None => {
                    pgrx::warning!("pg_hashdb: could not decode schema name");
                    pg_sys::SPI_finish();
                    return String::new();
                }
            };
            let rel = match String::from_datum(rel_d, false) {
                Some(s) => String::from(s),
                None => {
                    pgrx::warning!("pg_hashdb: could not decode relation name");
                    pg_sys::SPI_finish();
                    return String::new();
                }
            };

            // Build the base query and open a SPI cursor (Portal) using the C API
            // Use `select *` to get each column. We'll encode each value using
            // its text output (fast path via SPI_getvalue) which avoids JSONB
            // overhead and still performs well.
            let select_sql = format!(
                "select * from \"{}\".\"{}\"",
                nsp.replace('"', "\"\""),
                rel.replace('"', "\"\"")
            );
            let select_c = CString::new(select_sql).unwrap();

            // Prepare a plan so we can open a cursor without issuing DECLARE/FETCH SQL
            let plan = pg_sys::SPI_prepare(select_c.as_ptr(), 0, std::ptr::null_mut());
            if plan.is_null() {
                pgrx::warning!("pg_hashdb: SPI_prepare failed for select");
                pg_sys::SPI_finish();
                return String::new();
            }

            // Safety: no parameters, read-only scan
            let portal = pg_sys::SPI_cursor_open(
                std::ptr::null(),
                plan,
                std::ptr::null_mut(),
                std::ptr::null(),
                true,
            );
            if portal.is_null() {
                pgrx::warning!("pg_hashdb: SPI_cursor_open returned NULL");
                pg_sys::SPI_finish();
                return String::new();
            }

            // Fetch in batches via SPI_cursor_fetch to avoid SQL-level FETCH inside a function
            let batch = if batch_rows > 0 { batch_rows as isize } else { 10000 as isize };
            let mut numcols: i32 = 0;
            loop {
                pg_sys::SPI_cursor_fetch(portal, true, batch as _);
                if pg_sys::SPI_processed == 0 { break; }
                let tt = pg_sys::SPI_tuptable;
                if tt.is_null() {
                    pgrx::warning!("pg_hashdb: SPI_tuptable was NULL after cursor fetch");
                    break;
                }
                let tupdesc = (*tt).tupdesc;
                let vals = (*tt).vals;
                if numcols == 0 { numcols = unsafe { (*tupdesc).natts } as i32; }
                for i in 0..pg_sys::SPI_processed {
                    let htup = *vals.add(i as usize);
                    // Hash a canonical per-column text representation
                    let mut row = Hasher::new();
                    let mut row_keyed = Hasher::new_keyed(&KEY);
                    // Include number of columns to delimit structure
                    row.update(&(numcols as i32).to_be_bytes());
                    row_keyed.update(&(numcols as i32).to_be_bytes());
                    let mut col: i32 = 1;
                    while col <= numcols {
                        let mut isnull = false;
                        let val = unsafe { pg_sys::SPI_getbinval(htup, tupdesc, col, &mut isnull) };
                        if isnull {
                            // Length = -1 marks NULL (mirrors COPY BINARY conventions)
                            row.update(&(-1i32).to_be_bytes());
                            row_keyed.update(&(-1i32).to_be_bytes());
                        } else {
                            // Use text output for this column via SPI_getvalue
                            let cstr = unsafe { pg_sys::SPI_getvalue(htup, tupdesc, col) };
                            if cstr.is_null() {
                                row.update(&(0i32).to_be_bytes());
                                row_keyed.update(&(0i32).to_be_bytes());
                            } else {
                                let bytes = unsafe { std::ffi::CStr::from_ptr(cstr) };
                                let blen = bytes.to_bytes().len() as i32;
                                row.update(&blen.to_be_bytes());
                                row_keyed.update(&blen.to_be_bytes());
                                row.update(bytes.to_bytes());
                                row_keyed.update(bytes.to_bytes());
                                unsafe { pg_sys::pfree(cstr as *mut _); }
                            }
                        }
                        col += 1;
                    }
                    let r = row.finalize();
                    let h1 = u128::from_be_bytes(r.as_bytes()[..16].try_into().unwrap());
                    let r2 = row_keyed.finalize();
                    let h2 = u128::from_be_bytes(r2.as_bytes()[..16].try_into().unwrap());
                    s1 = s1.wrapping_add(h1);
                    s2 = s2.wrapping_add(h2);
                    n += 1;
                }
            }

            // Close the portal
            pg_sys::SPI_cursor_close(portal);
        } else if rc != pg_sys::SPI_OK_SELECT as i32 {
            pgrx::warning!("pg_hashdb: failed to resolve relation name (rc={})", rc);
        }

        pg_sys::SPI_finish();
    }

    let mut final_hasher = Hasher::new();
    final_hasher.update(&s1.to_be_bytes());
    final_hasher.update(&s2.to_be_bytes());
    final_hasher.update(&n.to_be_bytes());
    final_hasher.finalize().to_hex().to_string()
}

// Scans all user tables; VOLATILE and PARALLEL UNSAFE.
#[pg_extern(volatile, parallel_unsafe)]
fn vkar_db_hash(batch_rows: i32) -> TableIterator<'static, (name!(rel, String), name!(digest, String))> {
    let mut out: Vec<(String,String)> = Vec::new();
    unsafe {
        let rc_spi = pg_sys::SPI_connect();
        if rc_spi < 0 {
            pgrx::warning!("pg_hashdb: SPI_connect failed (rc={})", rc_spi);
            return TableIterator::new(out.into_iter());
        }
        let q = "select n.nspname::text, c.relname::text, c.oid from pg_class c join pg_namespace n on n.oid=c.relnamespace where c.relkind='r' and n.nspname not in ('pg_catalog','information_schema') order by 1,2";
        let q_c = CString::new(q).unwrap();
        let rc = pg_sys::SPI_execute(q_c.as_ptr(), true, 0);
        if rc == pg_sys::SPI_OK_SELECT as i32 {
            let tt = pg_sys::SPI_tuptable;
            if tt.is_null() {
                pgrx::warning!("pg_hashdb: SPI_tuptable was NULL when listing tables");
                pg_sys::SPI_finish();
                return TableIterator::new(out.into_iter());
            }
            let tupdesc = (*tt).tupdesc;
            let vals = (*tt).vals;
            for i in 0..pg_sys::SPI_processed {
                let htup = *vals.add(i as usize);
                let mut isnull = false;
                let nsp_d = pg_sys::SPI_getbinval(htup, tupdesc, 1, &mut isnull);
                let rel_d = pg_sys::SPI_getbinval(htup, tupdesc, 2, &mut isnull);
                let oid_d = pg_sys::SPI_getbinval(htup, tupdesc, 3, &mut isnull);
                let nsp = match String::from_datum(nsp_d, false) {
                    Some(s) => String::from(s),
                    None => {
                        pgrx::warning!("pg_hashdb: could not decode schema name when listing tables");
                        continue;
                    }
                };
                let rel = match String::from_datum(rel_d, false) {
                    Some(s) => String::from(s),
                    None => {
                        pgrx::warning!("pg_hashdb: could not decode relation name when listing tables");
                        continue;
                    }
                };
                let oid = pgrx::pg_sys::Oid::from_datum(oid_d, false).unwrap();
                let digest = vkar_hash_table(oid, batch_rows);
                out.push((format!("{}.{}", nsp, rel), digest));
            }
        } else {
            pgrx::warning!("pg_hashdb: failed to list tables (rc={})", rc);
        }
        pg_sys::SPI_finish();
    }
    TableIterator::new(out.into_iter())
}
