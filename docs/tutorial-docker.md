# Tutorial: foldout with docker-compose

Same loyalty-points migration story as [the local tutorial](./tutorial-local.md),
but PostgreSQL runs in a docker-compose container alongside the rest of
your app. Foldout itself runs on the host because it needs filesystem-level
access to `PGDATA` to do reflink copies.

If you haven't read the local tutorial, **read it first.** This one only
covers the docker-specific setup — the snapshot/branch/diff/apply flow
is identical once the wiring is right.

## The wiring you need

Foldout needs three things to work against a containerized PG:

1. **A TCP connection** to the PG server (the container's port).
2. **Filesystem read/write access to PGDATA** as the same paths the server sees. That means the container's `PGDATA` must be a **bind mount** from a host directory you can also read/write.
3. **A CoW filesystem** holding that host directory: btrfs, APFS (Docker on Mac), or xfs+reflink. 

If you mount a named volume (eg: `volumes: [pgdata:/var/lib/postgresql/data]`), foldout's will not work. Use a bind mount to a real directory. Foldout goes through file system, we currently don't support docker volumes. 

## 1. The docker-compose setup

Pick a host directory on a CoW filesystem. On Linux with btrfs, anywhere on the btrfs mount works. On macOS, anywhere — APFS is the default. We'll use `./pgdata` next to `docker-compose.yml`.

```yaml
# docker-compose.yml
services:
  postgres:
    image: postgres:17
    container_name: shop-pg
    environment:
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: postgres
    ports:
      - "5499:5432"     # host:container
    volumes:
      # Bind-mount, NOT a named volume. Foldout needs the host path
      # to be on a CoW filesystem (btrfs / APFS / xfs+reflink).
      - ./pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      timeout: 3s
      retries: 10
```

Bring it up:

```bash
docker compose up -d
docker compose logs -f postgres   # wait for "database system is ready"
```

The host directory `./pgdata` now contains the PG cluster's files. You can `ls` it as a normal user (you may need `sudo` depending on docker's UID mapping — see "UID mismatch" below).

## 2. Tell foldout where to look

Foldout runs on the host, so it needs:

- **`FLD_DATABASE`**: a DSN that reaches the container's PG on its published port (5499 in our example).
- **`FLD_PG_DATA_PATH`**: the **host path** to the PGDATA bind mount. The container sees it as `/var/lib/postgresql/data`; the host sees it as `./pgdata`. Foldout needs the host path because that's what it'll pass to `cp --reflink=always`.

```bash
export FLD_DATABASE="postgresql://postgres:postgres@127.0.0.1:5499/postgres"
export FLD_PG_DATA_PATH="$(pwd)/pgdata"
```

Sanity check:

```bash
foldout databases list
# OID        Database Name
# ...
```

If that prints, the DSN is right.

```bash
ls "$FLD_PG_DATA_PATH/base"
# 1  4  5
```

If that prints directory entries (numeric names — PG's database OIDs), the data path is right.

## 3. Run the loyalty-points scenario

From here on, everything is identical to the [local tutorial](./tutorial-local.md). Quick recap with the docker-specific commands inline:

### Build the shop database

```bash
psql "$FLD_DATABASE" -c 'CREATE DATABASE shop'
psql "postgresql://postgres:postgres@127.0.0.1:5499/shop" <<'SQL'
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
INSERT INTO orders (customer_id, total)
SELECT c.id, round((random() * 200 + 20)::numeric, 2)
FROM customers c, generate_series(1, 5);
SQL
```

### Snapshot before a risky update

```bash
foldout snapshot shop
# Snapshot completed successfully: snapshot_shop_20260526_104230 (...)
```

Run the "wrong" promotion query:

```bash
psql "postgresql://postgres:postgres@127.0.0.1:5499/shop" \
     -c "UPDATE orders SET total = total * 0.9"
```

Restore:

```bash
foldout snapshots restore shop snapshot_shop_20260526_104230
```

### Branch, develop, diff, apply, delete

```bash
foldout branch shop loyalty
```

```bash
psql "postgresql://postgres:postgres@127.0.0.1:5499/loyalty" <<'SQL'
ALTER TABLE customers ADD COLUMN loyalty_points integer NOT NULL DEFAULT 0;
UPDATE customers c
SET loyalty_points = COALESCE((
    SELECT floor(sum(total))::int FROM orders WHERE customer_id = c.id
), 0);
SQL
```

```bash
foldout diff loyalty > loyalty.sql
cat loyalty.sql
foldout apply loyalty.sql
foldout delete-branch loyalty
```

See the [local tutorial](./tutorial-local.md) for the full annotated walkthrough.

## Putting it in your dev loop

The flow is the same: snapshot before risky changes, branch for feature work, diff to review, apply to merge, delete-branch to clean up.
