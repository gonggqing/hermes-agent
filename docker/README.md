# Local Docker development and operations

The repository root [`docker-compose.yml`](../docker-compose.yml) is the
single Compose definition. Use [`docker/compose.sh`](./compose.sh) as the
stable operator entrypoint so UID/GID mapping, project paths, service defaults,
and non-destructive lifecycle behavior stay consistent.

## Runtime layout

| Compose service | Container | Purpose | Local endpoint |
|---|---|---|---|
| `gateway` | `hermes` | Hermes gateway, Telegram general bot, Web dashboard | `http://my.hermes:9119` or `http://127.0.0.1:9119` |
| `finance` | `hermes-finance` | Finance API, scheduler, paper broker, finance Telegram bot | `http://127.0.0.1:9319` |
| `hermes-finance-vector` | `hermes-finance-vector` | Private Qdrant storage for Finance knowledge retrieval | no host port |

`gateway` and `finance` share the locally built `hermes-agent` image. The
Finance process is separate so research/model latency cannot block Hermes chat.
The Finance-owned scheduler, not Hermes conversational cron, runs the persisted
US/CN/HK/KR briefs at 09:00 and 21:00 Asia/Shanghai.

The Compose Finance command explicitly enables `--cn-paper` with a CNY 100,000
opening sleeve. This is behavioral configuration, not a credential: mainland
orders remain `Mode.PAPER`, share no USD/HKD cash, and cannot activate an IBKR
live path. Change the amount only before the first fill in a new ledger; the
rehydration contract assumes opening balances remain stable thereafter.

Finance connects to Qdrant only over `finance-internal`. The Finance API is
published only on host loopback; the vector service has no published port.
Historical normalized documents can be idempotently backfilled with the
`migrate-vector` command documented in [`docs/finance-vector.md`](../docs/finance-vector.md).

## Prerequisites and configuration

1. Docker Desktop/Engine with Compose v2 is running.
2. Secrets exist in `~/.hermes/.env` (Telegram and model-provider credentials).
3. Behavioral/model routing exists in `~/.hermes/config.yaml`.
4. `my.hermes` is optional. To use it, add `127.0.0.1 my.hermes` to
   `/etc/hosts`; otherwise use the loopback URL.

Do not add a repository `.env` for runtime credentials. The Compose file mounts
all durable state from `~/.hermes` into `/opt/data`; rebuilding an image or
recreating a container therefore does not delete the ledger, portfolios,
briefs, sessions, research archive, or provider cache. Qdrant uses the named volume
`hermes-finance-vector-data`.

## Build and start

From the repository root:

```bash
./docker/compose.sh config       # validate configuration without printing secrets
./docker/compose.sh up           # start all services, using the existing image
./docker/compose.sh health       # verify dashboard/API/vector container
./docker/compose.sh status
```

After source or dependency changes:

```bash
./docker/compose.sh rebuild      # rebuild + recreate gateway and finance
./docker/compose.sh health
```

Do not use bare `docker compose config` in copied logs or chat: Compose expands
`env_file` values and can print credentials. The wrapper uses quiet validation.

`rebuild` does not recreate Qdrant and never removes volumes. To rebuild only
one application service, pass its Compose name. Remember that `gateway` and
`finance` share one image, so a later build of either updates the same image
tag; deploy both after changes that affect shared code.

```bash
./docker/compose.sh rebuild finance
./docker/compose.sh up gateway finance
```

## Development workflow

Container development is foreground Compose with a real image build:

```bash
./docker/compose.sh dev gateway finance
```

It streams both services and stops them with `Ctrl-C`. Source is baked into the
image; there is deliberately no source bind mount or hot reload because doing
so would hide the image's virtual environments/frontend build and make testing
different from deployment. Re-run `dev` after edits, or use the faster native
loops below and build once before integration testing.

Finance native loop:

```bash
cd trader
uv sync --extra dev --extra service --extra knowledge --extra ibkr
uv run pytest
uv run ruff check swing_trader tests
uv run python -m swing_trader serve --db /tmp/hermes-finance-dev.db --port 9320 \
  --cn-paper --starting-cash-cny 100000
```

Use a temporary DB and a different port for development. Stop the Compose
`finance` service before running a host process with the real Finance Telegram
token; one token must have exactly one long-poller.

Web/Desktop checks:

```bash
cd web && npm install && npm run check
cd ../apps/desktop && npm install && npm run typecheck
```

## Debugging

```bash
./docker/compose.sh logs finance          # follow the last 200 Finance lines
./docker/compose.sh logs gateway finance  # correlate both processes
./docker/compose.sh shell finance         # interactive container shell
./docker/compose.sh exec finance ps aux   # one non-interactive command
./docker/compose.sh health
```

Useful direct checks:

```bash
curl -fsS http://127.0.0.1:9319/v1/health
curl -fsS http://127.0.0.1:9319/v1/research/brief?market=us
curl -fsS http://127.0.0.1:9319/v1/research/brief?market=cn
curl -fsS http://127.0.0.1:9319/v1/markets
docker inspect --format '{{.RestartCount}}' hermes-finance
```

For an application error, capture the service logs, health response, container
restart count, current Git commit, and image ID before rebuilding. A restart can
erase useful in-memory evidence even though persisted state survives.

## Stop, clean up, and data safety

```bash
./docker/compose.sh stop finance  # stop one service
./docker/compose.sh restart       # restart gateway + finance, no rebuild
./docker/compose.sh down          # remove containers/network, keep all data
```

The wrapper intentionally provides no `down -v` command. Back up
`~/.hermes` and run `docker/finance/backup-vector.sh` before destructive
maintenance or Qdrant upgrades. Never run a host Finance process and the
Compose Finance service against the same live SQLite DB or Telegram token.

## Production-like local acceptance

Before calling a build usable:

1. Relevant unit/type/lint suites pass outside the runtime image.
2. `./docker/compose.sh rebuild` completes.
3. `./docker/compose.sh health` passes and restart counts stay stable.
4. Finance state (cash, positions, orders, latest briefs) is unchanged across
   the recreate unless a migration explicitly changes it.
5. One scheduled cycle or an explicit safe test proves persistence and model
   provenance; do not manually trigger order placement against stale research.

The Phase-1 IBKR paper/live cutover is intentionally separate; follow
[`docs/finance/go-live-runbook.md`](../docs/finance/go-live-runbook.md).
