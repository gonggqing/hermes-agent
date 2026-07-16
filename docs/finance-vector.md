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
  `finance` service joins it plus the normal Compose network for market/model
  egress. Gateway and dashboard stay off the private network.
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

**Host-run development (no Docker):** the trader uses
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

Embedded mode and the Docker service must not share a storage directory.

## Historical migration

The source of truth is `/opt/data/data/knowledge/documents.db`, not the old
embedded Qdrant directory. Rebuild vectors from normalized documents because
embedded and server Qdrant storage files are not a supported copy boundary.
The command uses deterministic point IDs, so it is safe to repeat:

```sh
./docker/compose.sh exec finance \
  /opt/hermes/trader/.venv/bin/python -m swing_trader migrate-vector \
  --documents-db /opt/data/data/knowledge/documents.db \
  --target-url http://hermes-finance-vector:6333 \
  --embedding-provider openai \
  --embedding-model text-embedding-3-small \
  --embedding-dim 1536 \
  --collection finance_knowledge_openai_te3s_1536 \
  --batch-size 32
```

Success prints JSON with `verified: true`, equal `source_documents` and
`target_after`, and resolved sample searches. Run it a second time after the
first migration to close any ingestion race; `target_after` must remain
unchanged. Keep `/opt/data/data/knowledge/vector` as a rollback snapshot until
the remote service has passed normal search and backup/restore checks.

Production retrieval uses OpenAI `text-embedding-3-small` at its native 1536
dimensions in the versioned `finance_knowledge_openai_te3s_1536` collection.
`OPENAI_API_KEY` remains a runtime secret in `~/.hermes/.env`; no key belongs
in source or `config.yaml`. At startup Finance selects this profile when that
key exists and idempotently fills any source/index count gap before enabling
semantic search. A failed or partially verified migration disables retrieval
for that process while authoritative documents and facts remain writable.

The original `finance_knowledge` collection retains the deterministic
256-dimensional `hashing-blake2b-v1` vectors as a rollback path. Never mix
dimensions or embedding models in one collection, and never delete the old
collection until the semantic collection has passed backup/restore and normal
retrieval validation.

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

# 3. Destroy the volume (resolve its real Compose-prefixed name before
#    removing the stopped container).
volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/qdrant/storage"}}{{.Name}}{{end}}{{end}}' hermes-finance-vector)
docker compose rm -f hermes-finance-vector
docker volume rm "$volume"

# 4. Let Compose recreate an empty volume + stopped container, then restore.
docker compose up --no-start hermes-finance-vector
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
