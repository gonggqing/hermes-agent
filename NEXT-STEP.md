# NEXT-STEP.md — handoff for the next agent/session

> Written 2026-07-12 by the Phase-0 scaffolding agent (Fable 5). Per the model
> plan in Loop.md §8, the next phase (refine & complete) is intended for
> Opus 4.8. **Read `Loop.md` first — it is the single source of truth.**

## Where we are

**Phase 0 is BUILT and at the ⛔ Review Gate.** Every backlog item in
Loop.md §10 is checked. The human must now review and approve before
anything advances (Loop.md §0 rule 4).

- All trading code: `trader/` (self-contained package `swing_trader`, zero
  Hermes-internal imports, extractable to its own repo before Phase 1).
- **614 tests green**: `cd trader && uv run --no-sync pytest`
- RiskEngine 100% branch coverage gate:
  `uv run --no-sync pytest tests/test_risk_engine.py --cov=swing_trader.risk --cov-branch --cov-fail-under=100`
- Offline E2E demo: `uv run python -m swing_trader simulate --days 22 --crash-day 12`
- Production paper loop + Finance API: `uv run python -m swing_trader serve`
  (FastAPI on 127.0.0.1:9319; the Hermes dashboard proxies `/api/finance/*`
  to it via `hermes_cli/finance_proxy.py`, so the Web/Desktop Finance tabs
  and their auth come for free).
- Web tab: `web/src/pages/FinancePage.tsx` (route `/finance`); built into
  `hermes_cli/web_dist`. The docker deploy at http://my.hermes:9119/ serves a
  PREBUILT bundle — **rebuild the docker image** (or run `hermes dashboard`
  on the host) to see the tab there.
- Desktop view: `apps/desktop/src/app/finance/` (typecheck green; not yet
  run as a packaged app).
- Knowledge store: `swing_trader/knowledge.py` (facts/documents/vector,
  fail-closed). Qdrant service: `docker compose up -d hermes-finance-vector`
  (internal-only, no host port); backup drill in `docs/finance-vector.md`.

## Architecture invariants (do NOT weaken — Loop.md §3)

1. RiskEngine is pure code; hard caps re-clamped at use time (1.6% per-trade,
   −4% breaker). 100% branch coverage is a merge gate.
2. ONE server-authoritative `ConfirmationService` (in the loop process).
   LLM/system can publish/expire but NEVER approve. Every attempt (incl.
   refused) audited in the ledger `audit_events` table with actor+surface+
   version+idempotency key.
3. Only `ExecutionEngine` talks to the broker; entries are always BRACKET
   with a protective stop; a failed exit re-places the stop.
4. Paper/live never mix (`mode` tag everywhere). No secrets in code/logs/ledger.
5. Tests never touch the network.

## What the human must decide at this gate

1. **Review the build** (this file + Loop.md §13 progress log + run the
   simulate command). Approve or request changes.
2. **Start the real 20-day paper run** (exit criterion: ≥20 trading days
   logged end-to-end). Needs: a long-running `swing_trader serve` process
   (host or a new docker service) so PaperBroker state survives the day;
   note serve restarts reset the paper account (ledger keeps history) —
   broker rehydration from ledger is the top Phase-0.5 TODO.
3. **Provide credentials** (all optional, in `trader/.env`, never committed):
   - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — enables the Telegram
     confirmation surface (portal-only works without it).
   - A real market-data API key IF yfinance proves too flaky (Polygon/
     Alpaca) — implement behind `DataFeed` (see `StubPaidFeed`); free
     yfinance is the Phase-0 default and already wired.
   - An LLM provider key (OpenRouter/OpenAI) — only needed for Phase 0.5
     LLM upgrades below; the rule-based v0 runs without any LLM.
   - An embedding provider for the knowledge store (current
     `HashingEmbedder` is a documented NON-semantic placeholder).

## Deployment state (2026-07-12 evening — running NOW)

- Host: `swing_trader serve --check-now` running (nohup, `trader/serve.log`,
  ledger `trader/trader.db`, port 9319). LLM analyst ENABLED (DeepSeek-v4-flash;
  GLM5-turbo fallback; keys from `~/.hermes/.env`).
- Docker topology (changed 2026-07-12): SINGLE container — the dashboard runs
  INSIDE the gateway container (`HERMES_DASHBOARD=1`, s6-supervised alongside
  `gateway-default`). Required for the dashboard's "Restart Gateway" button,
  which drives the LOCAL s6 slot (a separate dashboard container has no such
  slot → "no such gateway 'default'"). Under `network_mode: host` the
  dashboard binds host loopback directly (`HERMES_DASHBOARD_HOST=127.0.0.1`,
  token auth — no login page) and the /api/finance/* proxy reaches the
  host-run Finance service at its default `http://127.0.0.1:9319`.
  `hermes-finance-vector` (Qdrant) up. Visit http://my.hermes:9119/finance.
- Env: **~/.hermes/.env is the single source of truth for all secrets**
  (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DEEPSEEK_API_KEY, GLM_API_KEY,
  dashboard basic-auth). trader/.env was removed; `serve` loads
  ~/.hermes/.env first, optional trader/.env only for non-secret overrides.
- Telegram: bot posts to the "Hermes Finance" GROUP (TELEGRAM_CHAT_ID in ~/.hermes/.env).
  **Interactive approvals via Telegram are disabled** and the finance loop is
  OUTBOUND-ONLY by default (`TelegramSurfaceAdapter(interactive=False)`): the
  Hermes gateway long-polls getUpdates with the same bot token and a second
  consumer 409-kicks it offline (this actually happened on 2026-07-12 — the
  finance loop's 30s polling knocked the gateway's Telegram connection out).
  Approvals go through the portal. To enable Telegram buttons later: create a
  DEDICATED finance bot, put its token in trader/.env, set
  FINANCE_TELEGRAM_POLL=true.

## Phase 0.5 TODO queue (in priority order, after human approval)

1. **PaperBroker rehydration from ledger** (`serve` restart safety) — replay
   fills/orders to rebuild cash/positions/resting orders; add tests.
2. **LLM upgrade path**: implement `LLMAnalysisAgent` and `LLMDecisionCore`
   (model-agnostic via config; Loop.md §8) — analysis quality only; RiskEngine
   and confirmation flow unchanged. Wire Hermes memory adapter behind
   `MemoryStore` (currently `JsonMemory`).
3. **Earnings calendar** in NewsMonitor (`get_earnings_calendar` returns []).
4. **Knowledge ingestion pipeline**: NewsMonitor/daily research → FactsArchive
   + DocumentStore + vector index; surface `/v1/knowledge/search` in the API
   and a search box in the Finance tab (endpoint currently absent by design).
5. **Desktop IPC header pass-through** (electron/main.ts fetchJson) so
   X-Finance-Surface replaces the body-surface fallback; real user identity
   for `actor` (currently "hermes-user").
6. **Docker**: a `finance` compose service running `swing_trader serve` +
   image rebuild so the deployed dashboard at my.hermes:9119 shows the tab;
   join it to `finance-internal` for Qdrant.
7. Reporter cosmetic: morning summary "as of" uses wall clock (shows real
   time in simulations; correct in production).
8. Known pre-existing repo noise (NOT ours): web lint has 32 baseline
   problems; desktop vitest has 47 failing files at HEAD — both verified
   pre-existing by the agents; don't chase them into our files.

## Phase 1 preview (do not start without the gate + IBKR account)

`IBKRBroker` via ib_async — the stub in `swing_trader/ibkr_broker.py` carries
detailed method-by-method TODOs (bracket/ocaGroup mapping, T+1 settledCash,
pacing, 7497 paper port) and an acceptance checklist (mirror 20 paper trades,
measure sim→real slippage per Loop.md §7).

## How to verify everything quickly

```bash
cd /Users/gongqing/projects/hermes-agent/trader
uv sync --extra dev --extra service --extra knowledge
uv run --no-sync pytest                         # 614 tests
uv run python -m swing_trader simulate --days 22 --crash-day 12
uv run python -m swing_trader serve             # then open the dashboard /finance
cd ../web && npm run typecheck && npm run build # web tab
cd ../apps/desktop && npm run typecheck         # desktop view
docker compose config -q                        # qdrant service valid
```

## Session bookkeeping

- Everything is uncommitted in the working tree (user commits themselves).
  Suggested commit split: trader core / finance service+proxy / web tab /
  desktop view / knowledge+qdrant / Loop.md+NEXT-STEP.md.
- Memory notes for future sessions live in the Claude memory dir
  (`swing-trader-phase0`, `gongqing-investor-profile`).
