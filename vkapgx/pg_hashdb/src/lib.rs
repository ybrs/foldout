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

// Uses executor APIs to read table contents; VOLATILE and PARALLEL UNSAFE.
#[pg_extern(volatile, strict, parallel_unsafe)]
fn vkar_hash_table(reg: pg_sys::Oid, _batch_rows: i32) -> String {
    const KEY: [u8; 32] = [
        b'v', b'k', b'a', b'r',
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    ];

    let mut s1: u128 = 0;
    let mut s2: u128 = 0;
    let mut n: u64 = 0;

    unsafe {
        // Open the relation by OID and take an AccessShareLock
        let rel = pg_sys::table_open(reg, pg_sys::AccessShareLock as _);
        if rel.is_null() {
            pgrx::warning!("pg_hashdb: table_open returned NULL");
            return String::new();
        }

        // Create a scan using the active snapshot
        let snapshot = pg_sys::GetActiveSnapshot();
        if snapshot.is_null() {
            pgrx::warning!("pg_hashdb: no active snapshot for scan");
            pg_sys::table_close(rel, pg_sys::AccessShareLock as _);
            return String::new();
        }

        let scan = pg_sys::table_beginscan(rel, snapshot, 0, std::ptr::null_mut());
        if scan.is_null() {
            pgrx::warning!("pg_hashdb: table_beginscan returned NULL");
            pg_sys::table_close(rel, pg_sys::AccessShareLock as _);
            return String::new();
        }

        // Prepare a TupleTableSlot and discover column output functions
        let slot = pg_sys::table_slot_create(rel, std::ptr::null_mut());
        let tupdesc = (*rel).rd_att;
        let natts = (*tupdesc).natts as i32;
        let mut outfuncs: Vec<pg_sys::Oid> = Vec::with_capacity(natts as usize);
        let mut att: i32 = 1;
        while att <= natts {
            let atttypid = pg_sys::SPI_gettypeid(tupdesc, att);
            let mut outfn: pg_sys::Oid = pg_sys::InvalidOid;
            let mut isvarlena: bool = false;
            pg_sys::getTypeOutputInfo(atttypid, &mut outfn, &mut isvarlena);
            outfuncs.push(outfn);
            att += 1;
        }

        // Scan forward
        let fwd = pg_sys::ScanDirection::ForwardScanDirection;
        loop {
            let ok = pg_sys::table_scan_getnextslot(scan, fwd, slot);
            if !ok { break; }
            let mut row = Hasher::new();
            let mut row_keyed = Hasher::new_keyed(&KEY);
            row.update(&(natts as i32).to_be_bytes());
            row_keyed.update(&(natts as i32).to_be_bytes());
            let mut col: i32 = 1;
            while col <= natts {
                let mut isnull = false;
                let datum = pg_sys::slot_getattr(slot, col as _, &mut isnull);
                if isnull {
                    row.update(&(-1i32).to_be_bytes());
                    row_keyed.update(&(-1i32).to_be_bytes());
                } else {
                    let outf = outfuncs[(col - 1) as usize];
                    let cstr = pg_sys::OidOutputFunctionCall(outf, datum);
                    if cstr.is_null() {
                        row.update(&(0i32).to_be_bytes());
                        row_keyed.update(&(0i32).to_be_bytes());
                    } else {
                        let bytes = std::ffi::CStr::from_ptr(cstr);
                        let blen = bytes.to_bytes().len() as i32;
                        row.update(&blen.to_be_bytes());
                        row_keyed.update(&blen.to_be_bytes());
                        row.update(bytes.to_bytes());
                        row_keyed.update(bytes.to_bytes());
                        pg_sys::pfree(cstr as *mut _);
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

        // Cleanup
        pg_sys::ExecDropSingleTupleTableSlot(slot);
        pg_sys::table_endscan(scan);
        pg_sys::table_close(rel, pg_sys::AccessShareLock as _);
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
