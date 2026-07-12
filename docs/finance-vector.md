# hermes-finance-vector — private Qdrant deployment

Private, internal-only Qdrant vector store backing the Finance knowledge
store's semantic-retrieval layer (Loop.md §5.10, backlog item "Private
`hermes-finance-vector` Qdrant deployment"). It accelerates semantic search
over research documents only — it is never a source of truth and never
replaces the facts layer or the SQLite Ledger.

Key properties (all enforced in `docker-compose.yml`):

- **No published host port.** The service has no `ports:` mapping and is
  attached only to the `finance-internal` network (`internal: true`), so it
  is unreachable from the host, the LAN, and every non-finance container.
- **Only Finance-service containers may join `finance-internal`.** The
  gateway and dashboard stay off it. A commented `finance-service` example in
  `docker-compose.yml` shows how a future Finance container joins.
- **Persistent named volume** `hermes-finance-vector-data` mounted at
  `/qdrant/storage`. Back it up before every image upgrade.
- **Pinned image** `qdrant/qdrant:v1.15.4` (latest stable known as of
  2025-09; verify the tag/release notes before the first pull and before any
  bump).

## Starting only this service

```sh
docker compose up -d hermes-finance-vector
```

This starts Qdrant alone (it has no `depends_on`), creates the
`hermes-finance-vector-data` volume and the `finance-internal` network on
first run, and does not touch the gateway or dashboard. Health is reported by
`docker compose ps` via the built-in healthcheck (an HTTP `GET /readyz`
against the container-internal port 6333).

## How the Finance service connects

**Inside the compose network (containerized Finance service):** the container
must join the `finance-internal` network, then connect to

```
http://hermes-finance-vector:6333
```

(gRPC, if enabled client-side, is `hermes-finance-vector:6334` on the same
network). No credentials cross the host boundary because the network has no
host route and no egress.

**Host-run Phase 0 (no Docker):** the trader runs on the host and uses
qdrant-client's embedded local mode instead of this service — per Loop.md
§5.10, embedded/local persistence is acceptable for the initial small corpus.
Install the `knowledge` extra (`trader/pyproject.toml`) and point the client
at a data path under `trader/`:

```python
from qdrant_client import QdrantClient

# Path relative to a process started inside trader/ (e.g. from
# swing_trader code); from the repo root use "trader/data/finance_vector".
client = QdrantClient(path="../trader/data/finance_vector")
```

Embedded mode and the Docker service must not share a storage directory; when
the Finance service becomes long-running, migrate the corpus into this
service and retire the embedded path.

## Backup

```sh
sh docker/finance/backup-vector.sh
```

Takes a stop-less snapshot: mounts the volume read-only into a throwaway
container and writes `./backups/finance-vector/<UTC timestamp>.tar.gz`
(run it from the repo root). For a guaranteed-consistent snapshot, stop the
service first (`docker compose stop hermes-finance-vector`). **Always back up
before changing the pinned image tag** — Qdrant storage-format migrations are
one-way.

## Restore

```sh
sh docker/finance/restore-vector.sh backups/finance-vector/<stamp>.tar.gz
```

Refuses to run while the container is running, and refuses to write into a
non-empty volume unless `--force` is given (`--force` untars over existing
contents; for a clean restore destroy and recreate the volume as in the drill
below).

## Backup/restore DRILL

> **Run this drill once, end to end, before the service is considered
> production.** A backup procedure that has never been restored is not a
> backup procedure.

All commands from the repo root. `<stamp>` is the timestamp printed by step 2.

```sh
# 0. Record the current collection count (needs a throwaway curl container
#    on the internal network; the compose project prefixes the network name
#    with the directory name, hence "hermes-agent_finance-internal").
docker run --rm --network hermes-agent_finance-internal \
  curlimages/curl:8.14.1 -sf http://hermes-finance-vector:6333/collections

# 1. Stop the service so the snapshot is consistent.
docker compose stop hermes-finance-vector

# 2. Take the backup.
sh docker/finance/backup-vector.sh

# 3. Destroy the volume (remove the stopped container first so the volume
#    is unreferenced).
docker compose rm -f hermes-finance-vector
docker volume rm hermes-finance-vector-data

# 4. Recreate an empty volume and restore into it.
docker volume create hermes-finance-vector-data
sh docker/finance/restore-vector.sh backups/finance-vector/<stamp>.tar.gz

# 5. Start the service and verify the collection count matches step 0.
docker compose up -d hermes-finance-vector
docker run --rm --network hermes-agent_finance-internal \
  curlimages/curl:8.14.1 -sf http://hermes-finance-vector:6333/collections
```

The drill passes when step 5 returns the same collections (and counts) as
step 0 and `docker compose ps hermes-finance-vector` shows the container
healthy.

## Fail-closed contract (Loop.md §5.10)

The vector database is storage/search infrastructure, **not** an execution
dependency:

- **Vector service down ⇒ research-dependent NEW entries are blocked.** Any
  trade entry whose thesis depends on semantic retrieval from
  `finance_knowledge` must fail closed — no degraded-mode entries, no
  fallback to stale unverifiable context.
- **Ledger and facts layers are unaffected.** The SQLite ledger (orders,
  fills, PnL, audit events) and the immutable facts layer never depend on
  Qdrant; a vector outage must never lose or alter Ledger records, and
  position management/exits driven by ledger + deterministic market data
  continue normally.
- Restoring the service restores search only; nothing in the vector index is
  ever treated as an order, fill, or risk record.
