use futures::StreamExt;
use std::time::Instant;
use tokio_postgres::{types::Type, NoTls};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let (client, connection) = tokio_postgres::connect(
        "host=127.0.0.1 dbname=coinleverprod user=aybarsb",
        NoTls,
    ).await?;

    tokio::spawn(async move {
        if let Err(e) = connection.await {
            eprintln!("connection error: {e}");
        }
    });

    let tables = client
        .query(
            "SELECT schemaname, relname
             FROM pg_catalog.pg_statio_user_tables
             ORDER BY pg_total_relation_size(relid) DESC
             LIMIT 10;",
            &[],
        )
        .await?;

    for row in tables {
        let schema: &str = row.get(0);
        let table: &str = row.get(1);
        let full = format!("\"{}\".\"{}\"", schema, table);
        let sql = format!("COPY {} TO STDOUT (FORMAT binary)", full);

        let column_count = fetch_column_count(&client, schema, table).await?;
        if column_count == 0 {
            println!("{} -> skipped (no columns)", full);
            continue;
        }

        let dummy_types: Vec<Type> = vec![Type::TEXT; column_count];

        println!("Streaming {}", full);
        let start = Instant::now();
        let copy_stream = client.copy_out(sql.as_str()).await?;
        let stream =
            tokio_postgres::binary_copy::BinaryCopyOutStream::new(copy_stream, &dummy_types);
        tokio::pin!(stream);
        let mut total_rows = 0u64;

        while let Some(row) = stream.next().await {
            let _ = row?;
            total_rows += 1;
        }

        println!(
            "{} -> {} rows decoded in {:.3?}",
            full,
            total_rows,
            start.elapsed()
        );
    }

    Ok(())
}

async fn fetch_column_count(
    client: &tokio_postgres::Client,
    schema: &str,
    table: &str,
) -> anyhow::Result<usize> {
    let rows = client
        .query(
            "SELECT COUNT(1)
             FROM pg_attribute a
             INNER JOIN pg_class c ON a.attrelid = c.oid
             INNER JOIN pg_namespace n ON c.relnamespace = n.oid
             WHERE n.nspname = $1
               AND c.relname = $2
               AND a.attnum > 0
               AND NOT a.attisdropped;",
            &[&schema, &table],
        )
        .await?;

    let count: i64 = rows.first().map(|row| row.get(0)).unwrap_or(0);
    Ok(count as usize)
}
