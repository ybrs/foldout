import os, sys, random, psycopg, io

def gen_rows(limit):
    for i in range(limit):
        yield f"{i}\t{random.random()}\n".encode()

def main():
    n = int(sys.argv[1])
    dsn = os.environ.get("FLD_DATABASE")
    if not dsn:
        sys.exit(1)
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("create table if not exists bulk(id bigint primary key, val double precision)")
            cur.execute("set synchronous_commit=off")
            with cur.copy("copy bulk (id,val) from stdin") as cp:
                buf = bytearray()
                for i, row in enumerate(gen_rows(n)):
                    buf.extend(row)
                    if i % 1000000 == 999999:
                        print("now in i", i)
                        cp.write(memoryview(buf))
                        buf.clear()
                if buf:
                    cp.write(memoryview(buf))
            conn.commit()

if __name__ == "__main__":
    main()
