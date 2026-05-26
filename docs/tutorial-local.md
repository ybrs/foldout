# Tutorial: foldout against a local PostgreSQL

This walks through a realistic end-to-end workflow on a local PG cluster:

1. Set up a small e-commerce schema.
2. Snapshot it before a risky one-off `UPDATE`, then restore when the UPDATE goes wrong.
3. Branch the database to build a new feature (`loyalty_points`).
4. Generate a SQL diff, review it, apply it back to the main DB.
5. Clean up.

The story: **you're launching a "loyalty points" program on your shop app.** Product wants every existing customer to get a one-time backfill: 1 loyalty point per dollar of past order spend. So you need to:

- Add a `loyalty_points` column to `customers`.
- Backfill it from each customer's order history.
- Verify the numbers before they hit the live database.

The first attempt at the backfill will use the wrong aggregate, give every customer the same value, and you'll restore from a snapshot to undo it. Then you'll redo it correctly on a branch where you can diff every row before applying.

## 0. Prerequisites

- A local PostgreSQL. Foldout works against PG 13 through PG 18.
- The filesystem holding PGDATA supports CoW reflinks:**APFS** (macOS default), **btrfs** or **xfs with reflink=1**. Without copy-on-write (eg: ext4) works, but every copy becomes a real byte copy.
- foldout installed (`uv pip install -e .` from the repo root).

If you don't have PG installed and want a throwaway one for this tutorial, the repo includes a self-contained PG 17 launcher:

```bash
scripts/my-pg17.sh init
scripts/my-pg17.sh start
export PGHOST=127.0.0.1 PGPORT=5499 PGUSER=postgres PGDATABASE=postgres
```

Otherwise, point at your existing PG. The rest of the tutorial assumes:

```bash
export PGHOST=127.0.0.1 PGPORT=5499 PGUSER=postgres
export FLD_DATABASE="postgresql://postgres@127.0.0.1:5499/postgres"
```

(Replace port/user as needed for your install.)

Sanity check:

```bash
foldout databases list
# OID        Database Name
# ------------------------------
# 5          postgres
# 4          template0
# 1          template1
```

## 1. Build the shop database

```bash
psql -c 'CREATE DATABASE shop'
```

Switch to it and create the schema:

```bash
psql -d shop <<'SQL'
CREATE TABLE customers (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email       text NOT NULL UNIQUE,
    name        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE orders (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id  uuid NOT NULL REFERENCES customers(id),
    total        numeric(10, 2) NOT NULL CHECK (total >= 0),
    placed_at    timestamptz NOT NULL DEFAULT now()
);

INSERT INTO customers (email, name) VALUES
    ('alice@shop.com', 'Alice Chen'),
    ('bob@shop.com',   'Bob Martinez'),
    ('carol@shop.com', 'Carol Singh');

-- 5 deterministic orders per customer. Alice is the best customer,
-- Carol middle, Bob lowest — that pattern matters when we backfill
-- loyalty_points later.
INSERT INTO orders (customer_id, total)
SELECT c.id,
  CASE c.email
    WHEN 'alice@shop.com' THEN 50 + 30 * g
    WHEN 'bob@shop.com'   THEN 20 + 15 * g
    WHEN 'carol@shop.com' THEN 40 + 20 * g
  END::numeric(10, 2)
FROM customers c, generate_series(1, 5) AS g;
SQL
```

Check it:

```bash
psql -d shop -c \
  "SELECT c.name, count(*) AS orders, sum(o.total) AS spend
   FROM customers c JOIN orders o ON o.customer_id = c.id
   GROUP BY c.name ORDER BY spend DESC;"
```

```
     name     | orders | spend
--------------+--------+--------
 Alice Chen   |      5 | 700.00
 Carol Singh  |      5 | 500.00
 Bob Martinez |      5 | 325.00
(3 rows)
```

Three customers, five orders each, total spend $1525 across them. Hold those numbers in your head — we'll watch them transform.

## 2. First attempt at the backfill (and the snapshot that saves you)

You're about to add a `loyalty_points` column to `customers` and
populate it from each customer's order history. Schema change plus
bulk update on every row of a table — exactly the kind of migration
where you want a way back if the numbers come out wrong.

Snapshot first:

```bash
foldout snapshot shop
```

```
Creating snapshot of database 'shop'...
Source database OID: 26924
Registered source database 'shop' in fld_databases
PostgreSQL data directory: /path/to/pgdata
Locked source database 'shop' for snapshot (8 ms)
CHECKPOINT (18 ms)
Created snapshot database 'snapshot_shop_20260526_084127' with OID: 26978 (17 ms) [FILE_COPY: 2 internal CHECKPOINTs]
Database files copied successfully (266 ms)
Registered snapshot 'snapshot_shop_20260526_084127' in fld_databases with parent OID 26924
Snapshot completed successfully: snapshot_shop_20260526_084127 (337 ms)
```

Note the snapshot name (`snapshot_shop_<timestamp>`). You can list them:

```bash
foldout snapshots list
```

```
Database: shop (OID: 26924)
  OID        Snapshot Name                  Created
  ------------------------------------------------------------
  26978      snapshot_shop_20260526_084127  2026-05-26 08:41:27
```

Now write the migration. You're a bit rushed, you reach for `count(*)`
when you really wanted `sum(total)` (or your coding agent did):

```bash
psql -d shop <<'SQL'
ALTER TABLE customers
    ADD COLUMN loyalty_points integer NOT NULL DEFAULT 0;

-- WRONG: this counts orders, but spec says one point per dollar of spend.
UPDATE customers c
SET loyalty_points = (
    SELECT count(*) FROM orders WHERE customer_id = c.id
);
SQL
```

Check the result:

```bash
psql -d shop -c \
  "SELECT name, loyalty_points FROM customers ORDER BY loyalty_points DESC, name;"
```

```
     name     | loyalty_points
--------------+----------------
 Alice Chen   |              5
 Bob Martinez |              5
 Carol Singh  |              5
(3 rows)
```

Every customer has exactly 5 points — because each has exactly 5
orders, and `count(*)` returns row counts, not spend totals. Alice's
$700 of lifetime value got the same reward as Bob's $325. Obviously
wrong.

You could try to fix it forward — drop the column, recompute, add it
back. But: you've already mutated the live database, the schema
history is dirty, and any other process connecting in this window
would see the wrong values. Instead, restore the snapshot. That puts
`shop` back to exactly where it was before the bad migration,
including dropping the column you added:

```bash
foldout snapshots restore shop snapshot_shop_20260526_084127
```

```
Restoring database 'shop' from snapshot 'snapshot_shop_20260526_084127'...
Moved original data directory to: /path/to/pgdata/base/fld_delete_26924_20260526_084136
Restored database OID 26980 from snapshot OID 26978
Connected successfully. Public tables found: 2
Restore completed successfully
```

Verify the column is gone and `customers` is back to its
pre-migration shape:

```bash
psql -d shop -c "\d customers"
```

```
                             Table "public.customers"
   Column   |           Type           | Collation | Nullable |      Default
------------+--------------------------+-----------+----------+-------------------
 id         | uuid                     |           | not null | gen_random_uuid()
 email      | text                     |           | not null |
 name       | text                     |           | not null |
 created_at | timestamp with time zone |           | not null | now()
Indexes:
    "customers_pkey" PRIMARY KEY, btree (id)
    "customers_email_key" UNIQUE CONSTRAINT, btree (email)
Referenced by:
    TABLE "orders" CONSTRAINT "orders_customer_id_fkey" FOREIGN KEY (customer_id) REFERENCES customers(id)
```

Clean slate. The snapshot was milliseconds on a CoW filesystem; the
restore was milliseconds too. You haven't lost anything except the
time it took to type the wrong query.

> **Note:** `restore` is destructive. It drops `shop` and rebuilds it from the snapshot. Any open connection to `shop` gets terminated. If you'd like to do forensics, you can get another snapshot, then restore your snapshot. Snapshots are almost free and almost instant on copy-on-write. There is no problem in having many snapshots.   

When you're satisfied the restore worked, you can clean up the snapshot:

```bash
foldout snapshots delete snapshot_shop_20260526_084127
```

```
Deleting snapshot 'snapshot_shop_20260526_084127'...
Dropped database 'snapshot_shop_20260526_084127'
Removed record for 'snapshot_shop_20260526_084127' from fld_databases
Snapshot 'snapshot_shop_20260526_084127' deleted successfully
```

You can also use a branch — an isolated copy where you can iterate freely, then *review
the diff* before applying.

## 3. Branch the database to build the feature

```bash
foldout branch shop loyalty
```

```
Creating branch 'loyalty' of database 'shop'...
Source database OID: 26980
Registered source database 'shop' in fld_databases
Installed foldout change-capture on source database
PostgreSQL data directory: /path/to/pgdata
page-index: lsn=1/3F26B790  relations=45  9 ms
Locked source database 'shop' for branch (9 ms)
CHECKPOINT (4 ms)
Created branch database 'loyalty' with OID: 27024 (17 ms) [FILE_COPY: 2 internal CHECKPOINTs]
Created base snapshot '__base__loyalty' with OID: 27025 (17 ms) [FILE_COPY: 2 internal CHECKPOINTs]
Database files copied successfully (55 ms)
Base snapshot files copied successfully (24 ms)
page-index: lsn=1/3F26C068  relations=45  34 ms
Saved page-indexes for branch_oid=27024 (parent + branch) (44 ms)
foldout change-capture already present on branch database
Registered branch 'loyalty' in fld_databases with parent OID 26980
Registered base snapshot '__base__loyalty' (linked to branch 'loyalty')
Logged branch creation operation to fld_log
Branch completed successfully: loyalty (247 ms)
```

You now have three databases:

- `shop` — your "main" branch, untouched.
- `loyalty` — a writable copy of `shop`, sharing physical extents via reflinks. Disk cost is essentially zero until you start writing.
- `__base__loyalty` — a frozen copy of `shop` at branch time. Foldout uses it as the merge base for 3-way diffs; you'll never touch it directly.

Develop the feature on `loyalty`:

```bash
psql -d loyalty <<'SQL'
ALTER TABLE customers
    ADD COLUMN loyalty_points integer NOT NULL DEFAULT 0;

-- Backfill: 1 point per whole dollar spent in past orders.
UPDATE customers c
SET loyalty_points = COALESCE((
    SELECT floor(sum(total))::int
    FROM orders WHERE customer_id = c.id
), 0);
SQL
```

Sanity check:

```bash
psql -d loyalty -c \
  "SELECT name, loyalty_points FROM customers ORDER BY loyalty_points DESC;"
```

```
     name     | loyalty_points
--------------+----------------
 Alice Chen   |            700
 Carol Singh  |            500
 Bob Martinez |            325
(3 rows)
```

That matches the spend totals you saw at the very start — Alice's $700
of orders becomes 700 points, Carol's $500 becomes 500, Bob's $325
becomes 325. The backfill query computed the right thing.

While you work, `shop` is untouched — `loyalty_points` only exists on
the `loyalty` branch:

```bash
psql -d shop -c "\d customers"
```

```
                             Table "public.customers"
   Column   |           Type           | Collation | Nullable |      Default
------------+--------------------------+-----------+----------+-------------------
 id         | uuid                     |           | not null | gen_random_uuid()
 email      | text                     |           | not null |
 name       | text                     |           | not null |
 created_at | timestamp with time zone |           | not null | now()
Indexes:
    "customers_pkey" PRIMARY KEY, btree (id)
    "customers_email_key" UNIQUE CONSTRAINT, btree (email)
Referenced by:
    TABLE "orders" CONSTRAINT "orders_customer_id_fkey" FOREIGN KEY (customer_id) REFERENCES customers(id)
Triggers:
    foldout_row AFTER INSERT OR DELETE OR UPDATE ON customers FOR EACH ROW EXECUTE FUNCTION foldout.capture()
```

No `loyalty_points` column on `shop` — the branch is fully isolated from main. You are working on a different database.

Again branches have almost zero cost - even if your database is 50GB, a branch takes 100bytes or similar. Thanks to copy-on-write disk systems. 


## 4. Diff the branch against main

Now generate the SQL diff. `foldout diff` writes the SQL to stdout
with a parseable header on top; progress and summary go to stderr, so
redirecting stdout to a file gives you a clean script:

```bash
foldout diff loyalty > loyalty.sql
```

Stderr during the run:

```
Diffing branch 'loyalty' against parent 'shop' (3-way)
  base:    __base__loyalty
  pgdata:  /path/to/pgdata


4 SQL statement(s), 0 conflict(s), 169 ms
```

And `loyalty.sql`:

```sql
-- foldout-diff v1
-- parent: shop
-- branch: loyalty
-- mode: 3-way
-- base: __base__loyalty
--
ALTER TABLE "public"."customers" ADD COLUMN "loyalty_points" integer DEFAULT 0 NOT NULL;
UPDATE "public"."customers" SET "loyalty_points"='325'::integer WHERE "id"='5894b390-64dd-405d-a52c-e231431d298e'::uuid;
UPDATE "public"."customers" SET "loyalty_points"='700'::integer WHERE "id"='6eee7c96-ad3e-4ef2-bd57-ee329218dba7'::uuid;
UPDATE "public"."customers" SET "loyalty_points"='500'::integer WHERE "id"='4a690410-8bc2-452e-95ce-558b7aea345f'::uuid;
```

Every row that will change, identified by primary key, with the exact value it
will be set to. Three `UPDATE`s, three points totals (325, 700, 500)
matching the spend you saw earlier for Bob/Alice/Carol respectively.
You can verify by hand: cross-reference the UUIDs in the SQL against
`SELECT id, name FROM customers` if you want to triple-check.

If a backfill calculation were wrong — say, someone with $1500 in
orders ended up with 0 points because of a bug — you'd spot it here,
before it hits the live DB. Fix it on the branch, re-diff, repeat.
The diff is generated from the *actual page contents* of the branch,
not from a separate manifest of intended changes, so what the SQL
says is what the branch contains.

You can edit `loyalty.sql` directly if you want to tweak the migration. For example, you might wrap it in `BEGIN; ... COMMIT;` or add an audit log insert.

**Note**: We have the diff feature in foldout, but we always recommend asking the agents to create migration scripts/queries. A diff can not capture the intent. 
We simply have the diff to compare with what has changed and if migrations are missing. A diff.sql will not show you the intent and can move you in wrong direction.
There are cases where diff makes sense - eg: create a branch for agents to fill in some research for example, possibly throuh an endpoint or web ui. Yyou would wanna 
copy the data anyways. A diff makes perfect sense there. But for development tasks, diff can not replace migrations. 

## 5. Apply the diff to main

Once you're happy with `loyalty.sql`:

```bash
foldout apply loyalty.sql
```

```
apply: loyalty.sql -> 'shop' (mode=3-way)
Applied loyalty.sql successfully (last statement affected -1 row(s)). Use `foldout delete-branch` to clean up.
```

(The `-1` row count is from the last statement being an `UPDATE` whose
rowcount psycopg reports as `-1` for the final-batch case — cosmetic;
the SQL succeeded.)

`foldout apply` reads the `-- parent: shop` header to know where to
run the SQL. To apply to a different database, pass `--target`:

```bash
foldout apply loyalty.sql --target staging
```

Verify `shop` now has the new column with the correct values:

```bash
psql -d shop -c \
  "SELECT name, loyalty_points FROM customers ORDER BY loyalty_points DESC;"
```

```
     name     | loyalty_points
--------------+----------------
 Alice Chen   |            700
 Carol Singh  |            500
 Bob Martinez |            325
(3 rows)
```

Same values you saw on the `loyalty` branch. The migration is done,
and you reviewed every row that changed before committing to it.

**Note**: Again, you simply have an sql file, you can copy/paste, or run directly on your database. `apply` is a small convinience over using "psql shop < loyalty.sql" 

## 6. Clean up

The branch and base served their purpose. Delete them:

```bash
foldout delete-branch loyalty
```

```
Dropped branch database 'loyalty'
Dropped base snapshot '__base__loyalty'
Removed 'loyalty' from fld_databases
Removed 2 page-index row(s) for branch_oid=27024
delete-branch 'loyalty' complete
```

If anyone is still connected to `loyalty` (a `psql` you forgot, your app), `delete-branch` refuses and tells you who:

```
ERROR: cannot delete-branch 'loyalty' — 1 active connection(s) on the branch or its base.
  pid     app                     state                 ...
  ------------------------------------------------------------------------------
  12345   psql                    idle                  127.0.0.1   ...
Close them yourself, or re-run with --force to terminate them.
Nothing has been dropped.
```

Close the offending sessions and retry, or pass `--force` to terminate them.

## Recap

What you did:

| Step | Command | Cost |
|---|---|---|
| Snapshot before risk | `foldout snapshot shop` | ~milliseconds (reflinks) |
| Restore from snapshot | `foldout snapshots restore shop <snap>` | ~milliseconds |
| Branch to build a feature | `foldout branch shop loyalty` | ~milliseconds |
| Generate reviewable SQL diff | `foldout diff loyalty > loyalty.sql` | depends on changes, not DB size |
| Apply the diff | `foldout apply loyalty.sql` | depends on SQL contents |
| Clean up | `foldout delete-branch loyalty` | ~milliseconds |

Every cost above is essentially independent of how big `shop` is — that's the COW property. A snapshot of a 100 GB database is as cheap as a snapshot of a 100 KB one.

## Common follow-ups

- **What if my branch has conflicts with parent drift?** Foldout's 3-way diff detects when both sides changed the same column or row and reports them as conflicts. Read `foldout diff <branch>` carefully — conflicts appear on stderr.
- **`--force` on snapshot/branch/delete-branch.** Without it, foldout refuses to do anything if other sessions are connected to the source. With it, foldout terminates those sessions first. Use it when your app keeps a connection pool alive against the DB you're snapshotting.
- **Production / replicated setups.** Foldout is designed for development and pre-prod workflows on a single PG instance. On a replicated cluster, run it against the primary; replicas pick up changes through normal streaming replication.
