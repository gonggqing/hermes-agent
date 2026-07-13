# NEXT-STEP.md — handoff for the next agent/session

> Written 2026-07-12 by the Phase-0 scaffolding agent (Fable 5). Per the model
> plan in Loop.md §8, the next phase (refine & complete) is intended for
> Opus 4.8. **Read `Loop.md` first — it is the single source of truth.**

## Where we are (updated 2026-07-14)

**Phases 0 → 0.9 are BUILT; Phase 0.95 (pre-live gate) is LANDING.** Phase 0
(paper loop) was approved; since then: 0.5 (research-first UI + rehydration +
knowledge store), 0.5+ (two-session CN-morning/US-evening + dual bots), 0.75
(fundamentals, on-demand endpoints, finance toolset, K-line skill, earnings,
RAG, deeper ingestion), **0.8 (resilience: dead-man's switch + health/heartbeat
+ ledger↔broker reconcile + feed retry/backoff)**, **0.9 Portfolio Foundation
(auditable multi-account US/HK/CN Portfolio Journal)**, and now **Phase-1
broker backbone + 0.95 go-live gate** (see next block). Still NO live orders —
the §3 triple gate is untouched; IBKR account is the last blocker for Phase 1.
See Loop.md §13 for the dated detail.

### Phase-1 readiness landed this cycle (2026-07-14)

The Phase-1 *code* is now built and validated OFFLINE so IBKR slots in the
moment the account funds:

- **IBKRBroker** (`ibkr_broker.py`) behind a neutral `IBClient` port — full
  place→partial→fill→cancel→reject/bracket-OCA lifecycle, driven by a mock IB
  transport; `ib_async` imported lazily (constructing the broker opens no
  socket, imports nothing — asserted). CASH account spends only `SettledCash`;
  IBKR *paper* account tags `Mode.PAPER`, live account tags `Mode.LIVE`.
- **Broker factory** (`broker_factory.py`) — `BROKER=ibkr` now runs; the
  paper/live account flag is derived from the triple gate, and a live port
  under an un-gated config fails closed at construction. `__main__` Phase-0
  hard stop removed (order gate intact). `live_orders_allowed` threaded
  DailyLoop→ExecutionEngine (default False). Optional `[ibkr]` extra.
- **Kill-switch** (`killswitch.py`) — filesystem HALT flag; halts NEW entries
  via the RiskEngine (does NOT touch protective stops); HTTP engage(any)/
  release(human-only) + `cancel-all`; CLI `kill`/`release`/`killswitch-status`.
- **Go-live runbook** — `docs/finance/go-live-runbook.md` (paper-first dry run,
  cutover, kill-switch drill, emergency + rollback, defence-in-depth appendix).
- **Regime-segmented walk-forward** (`regime_analysis.py`) — OOS validation
  bucketed by market regime with a ≥2-regime coverage gate.
- **Broker parity harness** — PaperBroker↔IBKRBroker substitutability tests.

Still TODO for the ⛔ Phase-0.95 human gate (do NOT self-advance): reviewed
weekend upstream sync merge; a real IBKR-**paper** dry run once TWS is up;
≥20 paper-day exit criterion; guardrail audit; **human sign-off**.

- All trading code: `trader/` (self-contained package `swing_trader`, zero
  Hermes-internal imports, extractable to its own repo before Phase 1).
- **1050 trader tests green** (+15 root finance-tool tests, +81 web tests):
  `cd trader && uv run --no-sync python -m pytest`
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
6. Finance and every future module must use the existing Desktop/Web i18n
   catalogs and locale conventions. Do not ship hard-coded UI text without its
   matching translation updates.

## Fork maintenance — official Hermes sync

This repository is a fork of official Hermes Agent `main`. Treat upstream
maintenance as a recurring engineering task, not a one-time migration:

1. Each weekend, fetch `upstream/main` and compare it to local `main`.
2. Create a dated integration branch; never merge upstream directly into the
   Finance branch without review.
3. Triage security/gateway/provider/Desktop/Web changes and their impact on
   Finance service boundaries, i18n, and Hermes-native UI components.
4. Resolve conflicts deliberately, run relevant Python, Desktop/Web, Docker,
   and Finance tests, then record compared commits, adopted/skipped features,
   conflict decisions, and migration follow-ups in the sync report.
5. Never use `reset --hard`, force-push, or unattended auto-merge. At least
   one reviewed sync cycle is required before Phase 1/live-IBKR work begins.

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

## Deployment state (updated 2026-07-13 — running NOW)

- Host: **launchd** `com.hermes.finance` runs `swing_trader serve` (KeepAlive +
  RunAtLoad, `trader/serve.log`, ledger + portfolio DB `trader/trader.db`, port
  9319). Daily model **MiniMax-M3** (`minimax-cn` provider, `MINIMAX_CN_API_KEY`);
  subagents **MiniMax-M2.7-highspeed**. Restart to pick up new code:
  `launchctl kickstart -k gui/$(id -u)/com.hermes.finance`.
- Two daily sessions (Loop.md §4b): CN morning research (Asia/Shanghai, 09:30→
  11:30, report-only) + US evening trading (ET, full monitor→decide→push→approve→
  execute). Dual Telegram bots in one group: reporter (shared gateway token,
  outbound-only) + gatekeeper (dedicated finance token, interactive approvals +
  @mention/DM). NOTE: a session MISSED while serve was down is NOT re-run (the
  runner's watermark = boot time); a manual "run session now" trigger is the next
  task (#43). `serve --check-now` refreshes data/research only (no candidates).
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

## Getting REAL data into the Finance tab (do these, in order)

1. **Keep the service alive permanently.** It currently runs from a nohup
   shell (`trader/serve.log`) and DIES on reboot/logout. Make it a launchd
   agent (macOS):
   `~/Library/LaunchAgents/com.hermes.finance.plist` running
   `cd <repo>/trader && uv run --no-sync python -m swing_trader serve --db trader.db`
   with `KeepAlive=true` — or move it into a docker `finance` service later.
   Check: `curl http://127.0.0.1:9319/v1/health` → `loop_attached: true`.
2. **Wait for a US trading day.** The loop is schedule-driven (§4): Mon–Fri
   21:30 Beijing monitors poll → 23:00–23:30 decide → 23:30 candidates appear
   in the tab's Approval Queue + Telegram group → you approve/edit/reject
   before 00:30 → orders place → fills at 04:00 (US close) → morning report
   09:00 ET. Today's tab already shows the one-shot check data (market
   regime/VIX/watchlist/snapshot); positions/orders/trades stay empty until
   the first approved candidate fills.
3. **Actually approve something** (paper money) in the 23:30–00:30 window —
   that populates Orders → Fills → Positions → Trades → Stats → Audit.
   Refresh data anytime with a manual one-shot:
   `pkill -f "swing_trader serve" && nohup uv run --no-sync python -m swing_trader serve --check-now --db trader.db > serve.log 2>&1 &`
4. **Let it run ≥20 trading days** (~1 month) — that satisfies the Phase-0
   exit criterion and gives the stats block real win-rate/expectancy numbers.
5. Known gaps that limit data quality (Phase 0.5 queue below): PaperBroker
   state resets on service restart (ledger history survives; avoid restarts
   mid-position or rehydrate first), fundamentals provider is empty (agent
   returns None), earnings calendar returns [], knowledge search not yet
   wired into the tab.

## Phase 0.5 status (2026-07-13)

DONE: broker rehydration (restarts safe), launchd keep-alive
(com.hermes.finance), research brief API (/v1/research/brief) + research-first
Web/Desktop views (full en+zh i18n), knowledge ingestion + /v1/knowledge/search
wired into both UIs, upstream sync dry run (67 commits, zero conflicts,
docs/upstream-sync/2026-07-13.md; branch sync/upstream-2026-07-13 awaits the
reviewed weekend merge).
REMAINING: dedicated Finance Telegram bot — human creates it via BotFather,
puts FINANCE_TELEGRAM_BOT_TOKEN in ~/.hermes/.env (TELEGRAM_ALLOWED_USERS
gates who may approve); interactive approvals then enable automatically.

## Phase 0.5 TODO queue (original, superseded by the status above)

1. **PaperBroker rehydration from ledger** (`serve` restart safety) — replay
   fills/orders to rebuild cash/positions/resting orders; add tests.
2. **Research-first Finance home (human priority)** — do not start from an
   order queue. Build a dated `Investment Research` briefing API/model with:
   market regime/VIX/breadth; breaker, concentration/cash and data-freshness
   warnings; macro/theme changes; watchlist movers; earnings/events; sourced
   news and bull/bear synthesis; confidence/uncertainty; and links to the
   underlying knowledge documents. Desktop must default to this tab. Web must
   show the same brief first. `Queue` becomes a compact, badged **Actions
   requiring attention** surface, expanded only for pending confirmations,
   approaching cutoff, failed orders, or risk exceptions. Preserve clear
   PAPER/LIVE labels and never manufacture a source or current timestamp.
3. **Knowledge ingestion pipeline**: NewsMonitor/daily research/earnings →
   FactsArchive + DocumentStore + vector index; surface
   `/v1/knowledge/search` in the API and source-linked research search in both
   Finance clients (endpoint currently absent by design).
4. **Earnings calendar** in NewsMonitor (`get_earnings_calendar` returns []).
5. **LLM upgrade path**: implement `LLMAnalysisAgent` and `LLMDecisionCore`
   (model-agnostic via config; Loop.md §8) — analysis quality only; RiskEngine
   and confirmation flow unchanged. Wire Hermes memory adapter behind
   `MemoryStore` (currently `JsonMemory`).
6. **Dedicated Finance Telegram bot** for authenticated interactive approval;
   do not share the Hermes gateway token, because two `getUpdates` consumers
   evict one another. The existing Finance group remains outbound-only until
   this is done.
7. **Desktop IPC header pass-through** (electron/main.ts fetchJson) so
   X-Finance-Surface replaces the body-surface fallback; real user identity
   for `actor` (currently "hermes-user").
8. **Docker**: a `finance` compose service running `swing_trader serve` +
   image rebuild so the deployed dashboard at my.hermes:9119 shows the tab;
   join it to `finance-internal` for Qdrant.
9. Reporter cosmetic: morning summary "as of" uses wall clock (shows real
   time in simulations; correct in production).
10. Known pre-existing repo noise (NOT ours): web lint has 32 baseline
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

- Phase-0 implementation is committed as `2faf51615` (with subsequent
  deployment, Finance UX, Desktop-navigation, and persona commits on `main`).
  Check `git status --short` before editing; preserve unrelated user work and
  commit a focused Phase-0.5 change only after its tests pass.
- Memory notes for future sessions live in the Claude memory dir
  (`swing-trader-phase0`, `gongqing-investor-profile`).
