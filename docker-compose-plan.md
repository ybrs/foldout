# Docker Compose Plan (macOS dev)

Goal: Run PostgreSQL 17 via Docker Compose for local development on macOS (APFS host), with a bind-mounted data directory. Provide a companion Ubuntu 22 tool image with `psql` and `npm`, installing `@openai/codex` and `@anthropic-ai/claude-code` globally at build time.

## Prerequisites
- Docker Desktop for Mac (latest stable).
- macOS with APFS (default on modern Macs).

## Layout
- `example-docker-compose/`
  - `docker-compose.yml`: PostgreSQL 17 + tool container.
  - `Dockerfile`: Ubuntu 22.04 image with `psql` client and `npm` tools preinstalled.
  - `pgdata/`: Host-mounted directory for PostgreSQL data (created automatically on first up).

## PostgreSQL service
- Image: `postgres:17`.
- Data directory bound to `./pgdata` on the host.
- Exposes `5432` by default; change the published port if it conflicts locally.
- Health check using `pg_isready`.

## Tool container
- Base: `ubuntu:22.04`.
- Installs: `curl`, `ca-certificates`, `gnupg`, and `postgresql-client-17` (via PGDG).
- Node.js: installed via NVM (Node 22), symlinked to `/usr/local/bin` for ease of use.
- Global npm installs: `@openai/codex`, `@anthropic-ai/claude-code`.

## APFS notes (macOS)
- Bind-mount performance on macOS can be slower than Linux native; add `:delegated` or `:cached` to the mount for better performance with Docker Desktop.
- Ownership/permissions: the official Postgres image runs as user `postgres` (UID 999). Let Docker create `./pgdata` on first run to avoid permission mismatches, or `chown -R 999:999 ./pgdata` if pre-creating.

## Usage
1) Navigate to `example-docker-compose/`.
2) Start services: `docker compose up -d`.
3) Wait for Postgres to be healthy, then connect from the tool container:
   - Open a shell: `docker compose exec devtools bash`
   - Connect: `psql "postgresql://postgres:postgres@postgres:5432/postgres"`
4) To stop: `docker compose down` (use `-v` to remove volumes if you switch to named volumes).

## Environment for foldout
- Example DSN (reachable inside the compose network):
  - `postgresql://postgres:postgres@postgres:5432/postgres`
- Example DSN (reachable from host when port is published to 5444):
  - `postgresql://postgres:postgres@localhost:5444/postgres`

## Alternatives
- If bind-mount performance or permissions cause issues, use a named volume instead of `./pgdata`:
  - `volumes: [pg_data:/var/lib/postgresql/data]` with `volumes: { pg_data: {} }`.
