import os, sys, psycopg, subprocess, shlex

dsn   = os.environ["FLD_DATABASE"]
table = sys.argv[1]

cmd = shlex.split("openssl dgst -sha256 -binary")
p   = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)

with psycopg.connect(dsn, autocommit=True) as con, con.cursor() as cur:
    with cur.copy(f"COPY (SELECT id, val FROM {table} ORDER BY id) TO STDOUT (FORMAT binary)") as cp:
        for chunk in cp:                 # ~8 kB from libpq
            p.stdin.write(chunk)
p.stdin.close()
digest = p.stdout.read().hex()
print(digest)
