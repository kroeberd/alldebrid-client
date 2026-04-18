# PostgreSQL Configuration

AllDebrid-Client supports three database options. SQLite is the default and
requires no setup. Existing SQLite installations continue to work unchanged.

## Option 1 — SQLite (Default)

No configuration required. Works immediately after startup.

```json
{ "db_type": "sqlite" }
```

---

## Option 2 — PostgreSQL Internal (recommended for new installations)

PostgreSQL runs as a managed container alongside the app.
Data is stored in a persistent named Docker volume.

### Prerequisites

Both containers must be able to reach each other by hostname.
This requires a **shared Docker network** — the Compose file handles this
automatically via the `alldebrid-net` bridge network.

### Start with Compose (recommended)

```bash
# Copy and edit the env file
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD=your_secure_password

# Start both containers
COMPOSE_PROFILES=postgres docker compose up -d
```

Alternative with override file:
```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml up -d
```

### Unraid / standalone containers (Bridge mode)

When running containers individually without Compose, you must create a
shared Docker network manually so the containers can reach each other:

```bash
# 1. Create a shared network (once)
docker network create alldebrid-net

# 2. Start the PostgreSQL container on that network
docker run -d \
  --name alldebrid-postgres \
  --network alldebrid-net \
  --restart unless-stopped \
  -e POSTGRES_DB=alldebrid \
  -e POSTGRES_USER=alldebrid \
  -e POSTGRES_PASSWORD=your_secure_password \
  -v alldebrid-postgres-data:/var/lib/postgresql/data \
  postgres:16-alpine

# 3. Start the app on the same network
docker run -d \
  --name alldebrid-client \
  --network alldebrid-net \
  --restart unless-stopped \
  -p 8080:8080 \
  -v /path/to/data:/app/data \
  -v /path/to/config:/app/config \
  -e DB_TYPE=postgres_internal \
  -e POSTGRES_PASSWORD=your_secure_password \
  kroeberd/alldebrid-client:latest
```

Both containers are now on `alldebrid-net` and can reach each other
by container name (`alldebrid-postgres`).

### Truly isolated Bridge containers (PG_HOST override)

If you cannot use a shared network, set `PG_HOST` to the PostgreSQL
container's IP address:

```bash
# Find the IP of the postgres container
docker inspect alldebrid-postgres | grep '"IPAddress"'

# Start the app with the IP override
docker run -d \
  -e DB_TYPE=postgres_internal \
  -e PG_HOST=172.17.0.5 \
  -e POSTGRES_PASSWORD=your_password \
  ...
```

### Fallback behaviour

If PostgreSQL is unreachable at startup (after 15 retries × 10s = 150s),
the app automatically falls back to SQLite and logs a warning.
The Dashboard shows `⚠️ SQLite (PG Fallback)` in that case.
Restart the app once PostgreSQL is available to re-enable it.

---

## Option 3 — PostgreSQL External

Use your own PostgreSQL instance (Synology, Proxmox, external server).

```json
{
  "db_type": "postgres",
  "postgres_host": "192.168.1.10",
  "postgres_port": 5432,
  "postgres_db": "alldebrid",
  "postgres_user": "alldebrid",
  "postgres_password": "secure_password",
  "postgres_ssl": false
}
```

---

## Backward Compatibility

Existing SQLite installations start unchanged. A missing `db_type` in
`config.json` is treated as `"sqlite"`. No migration is required.

## Requirements

`asyncpg>=0.29.0` is already included in `requirements.txt` and `Dockerfile`.

## Migration between backends

See [migration.md](migration.md).
