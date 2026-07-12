# Loop.md — Autonomous Agent-Assisted Swing Trading System (v0)

> Single source of truth for the building agent. This project is a **human-in-the-loop, daily-cadence, swing-trading assistant** for one retail user (IBKR Hong Kong cash account, small capital). It is a **research/learning tool, not an income engine**. Profit is NOT the primary success metric; a reliable, well-instrumented, safe loop is.

---

## 0. How the building agent must operate (the "loop")

On every work iteration:
1. Read this file top to bottom. Identify the **Current Phase** (Section 7) and the next unchecked item in the **Task Backlog** (Section 10).
2. Implement exactly that item. Keep changes small and reviewable.
3. Write/extend tests. Run the full test suite. Do not proceed if tests fail.
4. Update the checkbox, append a dated note to the **Progress Log** (Section 11), and stop for human review at every **Review Gate** (marked ⛔).
5. If anything is ambiguous or would require violating a Guardrail (Section 3), STOP and ask the human. Never guess on risk, money, or credentials.

Definition of "done" for v0 = **Phase 0 exit criteria met** (Section 7).

---

## 1. Vision & Goal

Build a system that, each US trading day:
- **monitors** markets, the user's holdings/watchlist, news, and account risk;
- runs **analysis sub-agents** to form theses;
- a **decision core** proposes candidate orders with rationale + confidence + explicit take-profit/stop-loss;
- a **hardcoded risk engine** validates or vetoes them;
- exposes a permanent **Finance portal** in Hermes Desktop and Web, and pushes survivors to Telegram, inside a fixed daily window for **explicit confirmation from either surface**;
- **executes** approved orders through a **pluggable broker layer** using set-and-forget order types;
- **logs everything** (paper and live kept separate) and **improves** analysis quality from feedback.

**Primary success metric:** a dependable loop + clean data that lets the user measure whether any edge survives paper → live. Not returns.

---

## 2. Hard constraints (shape the whole design)

- **Account:** IBKR Hong Kong, **CASH account** (no margin, < $25k). No PDT, but **T+1 settlement** ⇒ **no stock day-trading**; positions are held overnight / multiple days. **Swing/positional only.**
- **IBKR not opened yet** ⇒ Phase 0 runs entirely on a **PaperBroker** backend + **free market data**; IBKR is a **stub behind an interface** (instrumented for later).
- **User availability (critical):** user can only watch the US market **09:30–12:30 ET** (= **21:30–00:30 Asia/Shanghai**); offline afterwards until next day.
  - The agent **finalizes candidates and pushes them to Telegram at 11:30 ET** (23:30 Shanghai). User confirms **by 12:30 ET**.
  - Because the user is offline for the US afternoon, **approved orders must be set-and-forget**: **GTC limit**, **GTC stop-loss**, and/or **MOC/LOC** (market/limit-on-close, filling at 16:00 ET close while the user sleeps).
- **Capital is small and partly family money** ⇒ tiny position sizes, hard risk caps, human-in-loop mandatory.

---

## 3. Guardrails (NEVER violate)

- No HFT / intraday stock churn.
- **No autonomous order placement without human confirmation** in Phases 0–2.
- The **Risk Engine is pure code, deterministic, and authoritative**. The LLM/agent **cannot modify, disable, or bypass it**.
- **Self-improvement affects analysis/signal quality ONLY** — never risk limits, position caps, or order authority.
- **Per-trade risk ≤ 1.6%** of total equity (hard cap). **Daily drawdown circuit breaker = −4%** ⇒ halt new entries for the day.
- No live order unless `HUMAN_CONFIRM=true` AND `BROKER != paper`.
- **Model tools are not approval authority:** an LLM may research, propose, and query finance state, but it may never transition a candidate to `APPROVED` on its own. `approve_candidate` is an authenticated human action from Desktop, Web, or Telegram in Phases 0–2; its actor, surface, candidate version, and timestamp are ledger-audited.
- **Order authority is service-bound:** `place_order` is never a free-form LLM action. Only `ExecutionEngine` may submit it after an approved candidate has passed its final deterministic checks. A future Quant service may receive narrowly scoped autonomous authority only in Phase 3 under an explicit whitelist, per-strategy notional/risk limits, versioned deterministic rules, kill switch, and separate audit identity; discretionary LLM signals never gain that authority.
- Secrets (API keys) live in env / secrets store; never in code, logs, or the ledger.
- Every external dependency (data, LLM, broker) must be **mockable** so tests never hit the network.

---

## 4. Timezone-aware daily loop (state machine, times in ET)

| Time (ET) | Shanghai | Step |
|---|---|---|
| 09:30–11:00 | 21:30–23:00 | Monitors poll; analysis sub-agents build theses |
| 11:00–11:30 | 23:00–23:30 | Decision core aggregates → candidate orders; Risk Engine validates |
| **11:30** | **23:30** | **Publish risk-approved candidates to the Finance portal and push Telegram cards** |
| 11:30–12:30 | 23:30–00:30 | **User approves / edits / rejects from Desktop, Web, or Telegram**; approved → placed (GTC limit/stop or MOC/LOC) |
| 16:00 | 04:00 | MOC/LOC fills; resting GTC orders may fill |
| next 09:00 | next 21:00 | Reporter: overnight fills, ledger update, morning summary |

Order-type policy for approvals: **entries** = GTC limit or MOC/LOC; **protection** = GTC stop-loss (attach on entry fill via bracket/OCA); never leave a position without a resting stop.

---

## 5. Architecture (ports & adapters; keep the core broker-agnostic)

**5.1 Broker abstraction**
- `BrokerInterface`: `get_account()`, `get_positions()`, `get_quote(sym)`, `get_bars(sym, tf)`, `place_order(order)`, `cancel_order(id)`, `get_orders()`
- Adapters: **`PaperBroker`** (Phase 0; simulates fills at limit / next-bar / close with configurable slippage + commission; tracks cash/positions), `AlpacaPaperBroker` (optional), **`IBKRBroker`** (stub now; implement with `ib_async` later)
- `DataFeed` interface (`get_quote`, `get_bars`, `get_news`): adapters `YFinanceFeed` (now), IBKR feed (later)

**5.2 Monitors** (scheduled pollers; each persists timestamped snapshots)
- `MarketMonitor` (indices, VIX, breadth, risk-on/off)
- `PortfolioMonitor` (holdings + watchlist: chips/AI/storage/optical/grid + S&P/Dow)
- `NewsMonitor` (earnings calendar, macro, breaking; sentiment scoring)
- `CryptoMonitor` (optional; only after OSL permission + API support confirmed)
- `AccountRiskMonitor` (equity, P&L, drawdown, per-pool exposure, breaker status)

**5.3 Analysis sub-agents** — Fundamental, Technical, Sentiment/News, Macro; plus a **Debate agent** (bull vs bear). Start rule-based; upgrade to LLM.

**5.4 Decision core** (LLM; Hermes runtime) — consumes monitors + sub-agents + **memory** (user risk profile, past skills, trade journal) → candidate orders `{symbol, side, qty, order_type, limit, stop, tp, sl, rationale, confidence}`.

**5.5 Risk Engine** (pure code, authoritative) — size cap, exposure caps, daily drawdown breaker, liquidity/volatility checks; may **veto** or **shrink size**; agent cannot override.

**5.6 Confirmation service & gateways** — one server-authoritative candidate state machine shared by Desktop, Web, and Telegram. Render concise Telegram cards and native portal approval UI; collect approve/edit/reject; enforce the 11:30–12:30 ET window; expire stale candidates. A canonical candidate ID, idempotency key, authenticated actor/source (`desktop|web|telegram`), and immutable audit trail prevent double execution. Every edit and every approved candidate is re-validated by RiskEngine and ExecutionEngine immediately before broker submission.

**5.7 Execution & authority boundary** — translate human-approved candidates to broker calls; prefer GTC limit + attached GTC stop (bracket/OCA) or MOC/LOC; **re-validate price vs signal validity before send**; handle partials/rejects. `place_order` is a service capability exposed only to ExecutionEngine, not a generic conversational skill. In Phase 3, an independently versioned Quant executor may use the same path for a pre-approved, low-notional strategy whitelist; it must identify itself as `quant:<strategy_version>`, satisfy all existing RiskEngine/ledger gates, and be instantly disabled by the human kill switch.

**5.8 Ledger & durable market memory** — SQLite ledger stores signals, orders, trades (`mode = paper|live`), fills, pnl, rationale, and approval audit events; feeds statistics (win rate, payoff ratio, max drawdown). Monitor snapshots and fetched source documents are retained by trading date rather than discarded. The ledger remains the authoritative source for numerical/accounting facts; no vector index may be treated as an order, fill, or risk record.

**5.9 Finance portal (Desktop + Web)** — add a permanent `Finance` tab to the existing Hermes Desktop and Web applications. Match their current UI/UX, routes, design tokens, state patterns, and shared components; do not create a second dashboard or re-implement chat. The tab is a native, structured companion surface with paper/live mode switch, market regime/watchlist, positions, open orders, risk/breaker state, candidate approval queue, fills/audit timeline, daily reports, and historical research search. Start read-only; write actions are limited to the same Confirmation service described in §5.6.

**5.10 Finance knowledge store (historical research + semantic retrieval)** — persist collected daily research, financial news, earnings/quarterly reports, company filings, strategy notes, monitor snapshots, decision rationales, and post-trade reviews. Use three layers:
- **facts:** immutable source files and normalized structured data, partitioned by event date/trading date (JSONL/Parquet for market/news snapshots; SQLite ledger for trading records);
- **research documents:** normalized text with source URL/publisher, retrieval date, content hash, symbol/theme, event timestamp, trading date (ET), document type, entitlement/license status, and parser/model version;
- **local vector index:** `finance_knowledge` embeddings point back to document IDs and metadata. It accelerates semantic retrieval only; it never replaces source records, deterministic market data, or the Ledger.

For an initial small local corpus, embedded/local Qdrant persistence is acceptable. Before the Finance service becomes long-running, run Qdrant as a dedicated private Docker service (for example `hermes-finance-vector`) with a named/host-mounted data volume, backup procedure, and **no published host port**; only Finance-service containers may connect over the internal Docker network. The vector database is storage/search infrastructure, not an execution dependency: if it is unavailable, trading must fail closed for research-dependent new entries and never lose or alter Ledger records.

Use public/owned/licensed material only. Public investor-relations filings and openly published research may be ingested with provenance; Morgan Stanley, J.P. Morgan, Goldman Sachs, Citi, and similar publisher research may be indexed only when the user has legitimate access and the publisher's terms permit local retention/processing. Never bypass paywalls, credentials, robots controls, copyright restrictions, or redistribute report text. Preserve citations and return source links/snippets rather than treating third-party reports as untraceable model facts.

---

## 6. Core data schemas (initial; evolve as needed)

- `Signal(id, ts, source_agent, symbol, thesis, direction, confidence, features_json)`
- `Order(id, ts, mode, symbol, side, qty, order_type[LMT|STP|MOC|LOC|BRACKET], limit, stop, tp, tif[GTC|DAY], status, broker_ref)`
- `Trade(id, entry_order_id, exit_order_id, symbol, qty, entry_px, exit_px, pnl, r_multiple, hold_days, rationale, mode)`
- `Position(symbol, qty, avg_px, mkt_px, upnl, pool)`
- `AccountSnapshot(ts, mode, equity, cash, upnl, day_pnl, drawdown, breaker_state)`

Paper and live share identical schemas (only the `mode` tag differs) so paper-vs-live comparison is exact.

---

## 7. Multi-stage roadmap — goals, targets, exit criteria

**⛔ Review Gate at the end of every phase — human must approve before advancing.**

### Phase 0 — Paper loop (NOW, no IBKR)
- **Goal:** full daily loop runs on `PaperBroker` + free data + Telegram, honoring the 11:30 ET push window.
- **Build:** broker abstraction, PaperBroker, DataFeed, Ledger, Risk Engine (+ full tests), monitors, decision core (rule-based first, then LLM), Telegram gateway, execution, scheduler, reporter, IBKRBroker stub.
- **Exit criteria:** ≥ 20 trading days of paper trades logged end-to-end; all tests green; Risk Engine unit-test coverage 100%; reporter produces a daily summary; schemas frozen for paper=live parity.

### Phase 1 — Shadow & tiny live (AFTER IBKR opens & funds)
- **Goal:** implement `IBKRBroker` (ib_async); run on **IBKR paper first**, then **tiny real money (a few hundred USD)**.
- **Target:** quantify the **sim→real gap** (slippage, fill quality, timing) using paper-vs-live ledger comparison.
- **Exit criteria:** ≥ 20 tiny-live trades; measured slippage/fill stats; loop stable under real fills; no guardrail breaches.

### Phase 2 — Validated scaling
- **Goal:** grow size **only if** the ledger shows **reproducible positive expectancy after costs** (walk-forward OOS + live confirmation).
- **Exit criteria:** documented positive expectancy across ≥ 2 regimes; drawdown within limits; human sign-off.

### Phase 3 — Limited Quant automation (optional, far future)
- **Goal:** permit a separately versioned **Quant executor** to auto-place a whitelist of low-frequency, rule-clear, small-notional strategies (for example a scheduled rebalance or explicitly tested systematic entry). This is an experiment after paper/live evidence, not a grant of autonomous discretionary LLM trading.
- **Guardrail:** `quant:<strategy_version>` must use the same RiskEngine, Ledger, ExecutionEngine, per-strategy capital limits, trade whitelist, and human kill switch as manual flow. Every Quant order remains attributable, replayable, and immediately disableable; discretionary LLM analysis can inform research but cannot itself call `approve_candidate` or `place_order`.

---

## 8. Tech stack (suggested; the building agent may substitute with justification)

Python 3.11 · `ib_async` (later) · `alpaca-py` (optional) · `yfinance` · `pandas` · `APScheduler` (ET-aware) · `SQLite` + `SQLModel` · JSONL/Parquet source archive · local Qdrant (or equivalent local vector store) + configurable embedding provider · Hermes Agent runtime for Finance tools/skills/gateway · Electron/React Desktop + existing Web component system · LLM via OpenRouter/OpenAI/local (model-agnostic) · `pytest` · `pydantic` for schemas.

**Data source policy:** start free — `yfinance` / Yahoo Finance for quotes, bars, and basic news. Put any **paid feed behind the `DataFeed` interface as a stub** (Polygon / Alpaca data / IBKR) so it can be swapped in later without touching the core.

**Model plan (build → refine → maintain):** scaffold v0 with **Fable 5**; refine & complete with **Opus 4.8**; long-term maintenance with **Sonnet 5**. The decision core is model-agnostic (chosen via config), so switching models is a config change, not a rewrite.

**Hermes runtime:** this is a product-focused Hermes fork at `/Users/gongqing/projects/hermes-agent/`. Finance is a first-class, always-visible Desktop/Web module plus a dedicated backend service/agent. The generic Hermes core remains stable, while the Finance agent receives a fixed Finance toolset for its session lifetime; this preserves predictable prompts and isolates financial authority from unrelated conversations. Keep trading-domain code in a separately extractable package/service with a versioned API — do not entangle broker/risk/ledger semantics with generic Hermes internals.

---

## 9. Testing & quality bar

- **Risk Engine:** exhaustive unit tests, 100% branch coverage. It is the safety core.
- Broker adapters tested against a mock exchange; order lifecycle (place→partial→fill→cancel→reject) covered.
- Scheduler tested for correct ET timing incl. DST; confirmation-window expiry tested.
- **Backtest harness** reuses the SAME signal + risk code paths; walk-forward / out-of-sample only; model slippage + commission.
- `DRY_RUN` mode default; live orders blocked unless `HUMAN_CONFIRM=true` and `BROKER != paper`.

---

## 10. Task backlog (work top-down; check off + log each)

- [x] Repo scaffold: config, `.env`/secrets, structured logging, `DRY_RUN`
- [x] `pydantic` schemas (Section 6) + tests
- [x] `BrokerInterface` + `PaperBroker` (fills, slippage, commission, cash/positions) + tests
- [x] `DataFeed` + `YFinanceFeed` + tests (mocked)
- [x] `Ledger` (SQLite/SQLModel) with `mode` tagging + tests
- [x] **`RiskEngine`** (size cap, exposure caps, daily drawdown breaker, liquidity/vol checks) + **100% tests**
- [x] Monitors: market, portfolio(holdings+watchlist), news, account/risk
- [x] Analysis sub-agents: technical + fundamental + sentiment + macro (rule-based v0) → LLM
- [x] Debate agent (bull vs bear)
- [x] Decision core + memory hookup (Hermes)
- [x] Telegram confirmation gateway (cards, approve/edit/reject, window enforcement)
- [x] Execution (bracket/OCA, MOC/LOC, re-validate before send, partial handling)
- [x] ET-aware daily scheduler (11:30 push, 12:30 cutoff, close/next-day report)
- [x] Reporter/dashboard (paper/live switch)
- [x] `IBKRBroker` stub + clearly marked integration TODOs
- [x] Finance service API + canonical dual-surface confirmation state machine (Desktop/Web/Telegram, idempotency, actor audit, server-side ET expiry) + tests
- [x] Permanent Finance Tab in Desktop and Web, reusing existing routes/components/design system; read-only market/portfolio/risk/audit views first
- [x] Finance knowledge store: dated raw-source archive + normalized research documents + local `finance_knowledge` vector index, provenance/retention/retrieval tests
- [x] Private `hermes-finance-vector` Qdrant deployment with persistent volume, backup/restore drill, internal-only network, and service-health failure tests
- [x] Backtest harness (walk-forward OOS)
- [x] Phase-0 end-to-end paper dry run for N days ⛔ **Review Gate — AWAITING HUMAN REVIEW (see NEXT-STEP.md)**

---

## 11. Watchlist universe (monitored set, NOT a buy list)

The monitors track this whole universe; the decision core trades only a small risk-checked subset. It is deliberately structured along the **AI value-chain progression** so the system can reason about **rotation over the next 2–3 years: infrastructure (now) → memory/networking/power → application/software/cloud**. US tickers (IBKR US market). Verify tickers/IPO status in-app; `NewsMonitor` keeps the set current.

**Thesis backdrop (2026, keep updated):** inference cost fell ~10x in ~18 months; inference is now ~2/3 of AI compute (training→inference shift); the five hyperscalers (AMZN/MSFT/GOOGL/META/ORCL) ≈ $600B capex, ~75% to AI infra. Cheaper tokens ⇒ more viable AI products ⇒ demand broadens UP the stack toward software/application & cloud, while still feeding compute/memory/networking. The watchlist spans infra→application on purpose so the system catches the rotation early. (Caveat: rotation timing is uncertain and valuations are high — hedges matter.)

Each symbol is tagged `{theme, ai_phase(infra|memory|network|power|application|cloud), role(core|conviction|rotation|hedge)}`; the risk engine enforces **per-role exposure caps**.

- **A. Base / reference indices** (context, low-vol anchors) — `SPY`/`VOO`/`IVV` (S&P 500), `DIA` (Dow), `QQQ` (Nasdaq-100), `VTI`.
- **B. AI infra — compute & chips** (current conviction) — `NVDA`, `AMD`, `AVGO`, `MRVL`, `TSM`, `ASML`, `AMAT`, `LRCX`, `KLAC`.
- **C. AI infra — memory / storage** (supercycle) — `MU`, `WDC`/`SNDK`.
- **D. AI infra — networking / optical** — `ANET`, `CIEN`, `LITE`, `COHR`, `CRDO`.
- **E. AI infra — systems / power / cooling / energy** — `SMCI`, `DELL`, `VRT`, `ETN`, `GEV`; power utilities `CEG`, `VST`; nuclear/uranium `CCJ`, `URA`; data-center REITs `EQIX`, `DLR`.
- **F. AI application / software / cloud** (the 2–3y upcycle to watch early) — hyperscalers `MSFT`, `AMZN`, `GOOGL`, `META`, `ORCL`; software/SaaS `PLTR`, `NOW`, `CRM`, `SNOW`, `DDOG`, `CRWD`, `ADBE`; software/cloud ETFs `IGV`, `WCLD`, `SKYY`.
- **G. Rotation / rate-sensitive upcycle** — biotech `XBI`, `IBB` (benefit as rates fall); optional small-caps `IWM`.
- **H. Hedges / diversifiers** (uncorrelated to the AI bet) — energy/oil&gas `XLE`, `XOP`, `XOM`, `CVX`; gold `GLD`/`IAU`; long/mid bonds `TLT`, `IEF`.
- **I. Crypto** (only after OSL permission + API support confirmed) — `BTC`, `ETH`.

---

## 12. Long-horizon thesis framework — value migration / Perez cycle (DIRECTIONAL & heavy-position ONLY, NOT daily trading)

**Purpose:** a slow-cadence (monthly/quarterly) lens for WHERE to overweight over years, kept strictly separate from the daily order loop. Output = directional tilts + heavy-position candidates that inform the human's rebalancing — never intraday entries.

**Framework (Carlota Perez, "Technological Revolutions & Financial Capital"):** every tech revolution runs Installation phase (infrastructure frenzy, financial-capital speculation, ends in a crash at the "turning point") → Deployment phase (broad-adoption golden age; value accrues to the application/usage layer). Historically each phase ≈ 10–15y; the biggest durable fortunes are made in DEPLOYMENT, not the installation frenzy. Value migrates UP the stack over time: **Infrastructure → Platform/Cloud → Application.**

**AI wave map (dynamic — the monitors keep company lists current):**
- **Wave 1 — Infrastructure / picks-&-shovels (NOW):** compute/GPU (NVDA, AMD, AVGO, MRVL), foundry/equipment (TSM, ASML, AMAT, LRCX, KLAC), memory (MU, WDC), optical/networking (ANET, CIEN, LITE, COHR, CRDO, GLW), power/cooling/energy (VRT, ETN, GEV, CEG, VST), DC REITs (EQIX, DLR). **CISCO LESSON:** the tech leader can be right yet a terrible investment if bought at the installation peak (Cisco +236% into 2000 → −85%, no new high for 20y). Distinguish **durable-moat infra** (TSM, ASML) from **commoditizing infra** (lost-moat risk). Trim conviction infra into frenzy; never chase the peak.
- **Wave 2 — Platform / cloud / model services (≈1–2y):** selling compute/tokens/models on top of infra. Reps: hyperscalers MSFT, AMZN, GOOGL, ORCL, META; model providers (OpenAI/Anthropic, mostly private). **CAUTION:** pure "pipes" commoditize — token prices already −~95%, so pure API/token sellers face margin compression (like debt-laden telecom carriers that died post-2000). Value accrues to platforms with **distribution + data moat**, not commodity token-sellers.
- **Wave 3 — Application / deployment golden age (≈2–3y+):** killer apps/agents/vertical-SaaS on near-zero token cost; the most durable value (historically Google/Amazon/Meta on cheap bandwidth). Reps: existing platforms extending (distribution edge) + new application-layer entrants (PLTR early example; many still private/newly public). Direction = enterprise AI, agents, vertical SaaS, AI-native software.

**Timing (AI faster than history):** still deep in Installation, near the turning point. Working assumption: installation ~2022–2027, a turning-point shakeout/crash plausibly in the **2026–2028** window (matches the portfolio's stated high-risk window), deployment golden age ~2028+. Timing is uncertain — do not force it.

**Directional rules for the decision core (heavy-position lens):**
1. Position ahead of the value migration: shift long-term weight infra → platform → application over quarters (not days) as evidence accrues.
2. Do not chase infrastructure at frenzy peaks (Cisco risk); trim conviction infra into strength.
3. Prefer moats/distribution over commoditizing pipes; be wary of pure token/connectivity sellers.
4. Treat a major AI-infra drawdown (the turning point) as the **rotation window** infra → deployment winners, not a reason to panic-sell the whole book.
5. Always keep uncorrelated hedges (energy/gold/bonds) — installation phases end in crashes.

**Phase-tracker signals monitors should feed this review:** inference $/token cost curve; inference-vs-training compute mix; hyperscaler capex + ROI commentary; application-layer revenue growth; any >15–20% AI-infra index drawdown (turning-point candidate); rate cuts (aid deployment/rotation).

**Cadence:** runs in a MONTHLY/QUARTERLY "strategy & allocation review" step; output feeds the human's rebalancing, never the daily order flow.

---

## 13. Progress log (building agent appends; newest first)

- 2026-07-12 (evening) — Deployment + LLM upgrade: `llm.py` LLMAnalyst (OpenAI-compatible; DeepSeek-v4-flash default, GLM5-turbo fallback via FINANCE_LLM_PROVIDER; analysis-only voice in the debate, confidence capped 0.8, ANY failure ⇒ no signal — rule-based agents remain in charge; keys from ~/.hermes/.env, never logged; 9 tests, suite 623). Docker image rebuilt w/ Finance web tab; dashboard gets HERMES_FINANCE_SERVICE_URL=http://host.docker.internal:9319 (container→host proxy verified: /api/finance/v1/health routes + auth-gates). hermes-finance-vector container started. Telegram "Hermes Finance" GROUP verified for OUTBOUND cards/reports; interactive getUpdates conflicts with the Hermes gateway holding the same bot token ⇒ approvals via portal for now (dedicated finance bot token = Phase 0.5 TODO). `serve --check-now` runs a one-shot monitors+report cycle at startup.
- 2026-07-12 — **Phase 0 build COMPLETE — ⛔ Review Gate reached; awaiting human review.** Backlog "E2E dry run" done: `dailyloop.py` orchestrates §4 (morning report → monitors → sub-agents/debate → decision core → RiskEngine → ConfirmationService publish → human surfaces → cutoff execution → close fills); `simulate.py` + `python -m swing_trader simulate --days 22 --crash-day 12` (crash: stops fire, risk-off blocks entries, max DD 5.39%); E2E test drives 22 simulated days with REAL HTTP web approvals + Telegram mock approvals + rejections + server-side expiry + never-naked-position invariant checked daily. `python -m swing_trader serve` = production paper loop + Finance API :9319. Suite 614 green. Fixed /v1/trades serialization (entry_ts) + body-surface fallback so Desktop audits as "desktop". Known Phase-0 limitations: PaperBroker state is in-memory (serve restart resets paper account; ledger keeps history — rehydration is a 0.5 TODO); LLM decision/analysis cores are stubs (rule-based v0 active).
- 2026-07-12 — Backlog "Finance surfaces + knowledge + vector" done: Web tab (`web/src/pages/FinancePage.tsx` + finance/* — approval queue w/ idempotent actions, breaker banner, offline panel; typecheck/test/build green, lint-scoped clean). Desktop view (`apps/desktop/src/app/finance/*` — react-query, non-overlay route, command-center entry; typecheck green; surface rides in body due to IPC header limitation). `knowledge.py` (FactsArchive dated JSONL append-only; DocumentStore w/ mandatory provenance, restricted-license refusal, sha256 dedupe; Qdrant `finance_knowledge` index embedded/remote w/ HashingEmbedder placeholder + KnowledgeUnavailable FAIL-CLOSED; 25 tests). `hermes-finance-vector` compose service (pinned image, no published ports, internal network, named volume) + backup/restore scripts + `docs/finance-vector.md` drill.
- 2026-07-12 — Backlog "Finance service API + confirmation state machine" done: `confirmation.py` ConfirmationService — ONE server-authoritative state machine (publish→approve/edit/reject/expire; idempotency keys replay-safe incl. across restart via the audit trail; per-candidate versions catch stale cards; system surface can publish/expire but NEVER approve; every attempt incl. refusals audited with actor+surface+version into the new append-only `audit_events` ledger table; edits re-validated structurally AND through a RiskEngine hook). `api.py` FastAPI service (`/v1`: health/account/orders/fills/trades/stats/snapshots/market/watchlist/reports/candidates/pending/audit + POST action; NO order-placement endpoint exists — §3 order authority stays with ExecutionEngine; 503-degrades when loop idle). Runs IN the loop process (own port 9319); `hermes_cli/finance_proxy.py` mounts `/api/finance/*` on the dashboard (inherits dashboard auth; strips session tokens; 503 offline hint). Telegram becomes a surface adapter of the same machine (`dailyloop.TelegramSurfaceAdapter`). 26+20 tests.
- 2026-07-12 — Backlog "Backtest harness" done: `backtest.py` — ReplayFeed (as-of cursor, no look-ahead), Backtester reuses the SAME TechnicalAgent→Debate→DecisionCore→RiskEngine→ExecutionEngine→PaperBroker→Ledger paths (§9); decisions on day i fill on bar i+1; auto-approve exists ONLY here (hard-wired Mode.PAPER, isolated ledger — documented bypass); WalkForwardBacktester picks params on TRAIN by expectancy, reports TEST-window-only combined stats. 14 tests (determinism, next-bar fills, OOS isolation, risk-off blocks entries, tiny-equity veto). Suite 528 green.
- 2026-07-12 — Product direction updated: Finance is a permanent first-class Hermes Desktop/Web tab, backed by a dedicated Finance service and a shared Desktop/Web/Telegram confirmation state machine. Added durable three-layer research storage (dated facts, normalized documents, local vector retrieval) with provenance/licensing requirements; third-party institutional research is ingestible only through legitimate access and permitted terms. Explicit authority split: human approval in Phases 0–2; ExecutionEngine-only broker submission; future low-notional Quant automation is separately versioned/whitelisted and never discretionary LLM authority. Added a private persistent Qdrant deployment/backup milestone.
- 2026-07-12 — Backlog#13/#14/#15 done: `scheduler.py` (pure zoneinfo state machine per §4 — substituted for APScheduler with justification; NYSE-2026 holiday table; DST-exact tests in EDT+EST; injectable-clock DailyLoopRunner fires each event once/day; 77 tests). `reporter.py` (AccountView paper/live switch, morning summary w/ overnight fills + stats + safety footer, push preamble, 4096-char clamp). `ibkr_broker.py` stub (all methods NotImplementedError w/ detailed ib_async TODOs: bracket/ocaGroup mapping, T+1 settledCash tracking, pacing, 7497/7496 ports; Phase-1 acceptance checklist). 12+ tests each.
- 2026-07-12 — Backlog#11 done: `telegram_gateway.py` — plain Bot HTTP API via requests (documented substitution for python-telegram-bot per §8: 3 endpoints, injectable transport, token never in logs/exceptions). Cards + ≤64-byte callback_data; 11:30→12:30 ET window enforced in both DST regimes; approve/reject/edit (edits re-validated through CandidateOrder — protection cannot be stripped, qty>0 enforced); post-cutoff: all actions refused; expire_stale → EXPIRED. 36 tests.
- 2026-07-12 — Backlog#7/#8/#9 done: `monitors.py` (Market/Portfolio/News/AccountRisk pollers + JsonlSink; regime rules risk_off-first; ATR14/ADV20 → LiquidityInfo for RiskEngine; positions RE-TAGGED with watchlist roles — required for pool-exposure caps; AccountRiskMonitor is THE breaker-tripper, clamped to −4% even with loose params; 46 tests). `analysis.py` (Technical/Fundamental/Sentiment/Macro rule-based v0 + DebateAgent bull-vs-bear synthesis w/ normalized confidence-weighted vote + disagreement penalty; SHORT = avoid/trim in cash account; LLMClient port + stub for Phase 0.5; 50 tests). Suite total 514 green.
- 2026-07-12 — Backlog#12 done: `execution.py` ExecutionEngine — human-approved candidates only (APPROVED/EDITED); re-validates before send (§5.7: valid_until, adverse drift >1.5% vs ref_px, last≤stop ⇒ "thesis broken"); entries always sent as GTC BRACKET (LMT+sl upgraded — never a naked position); discretionary exits cancel resting protection first and RESTORE the stop if the exit is rejected; GuardrailError blocks live mode without the §3 triple gate; sync_fills() dedupes into ledger with stop_px for r_multiple. 17 tests (incl. full entry→stop-out roundtrip vs real PaperBroker+Ledger).
- 2026-07-12 — Backlog#10 done: `decision.py` RuleBasedDecisionCore — debate signals → BRACKET entries (limit = last−0.5%, sl = 2×ATR, tp = 3×ATR, risk-based sizing re-using RiskParams' CLAMPED per-trade cap) / MOC exits (confidence ≥0.6; survive risk-off); no pyramiding; risk-off blocks entries only; top-3 by confidence. MemoryStore port + JsonMemory (Hermes adapter = Phase 0.5 TODO, keeps repo Hermes-agnostic per §8): memory can only LOWER confidence (§3: self-improvement touches analysis quality only). LLMDecisionCore stub (model-agnostic via config, §8 model plan). 23 tests.
- 2026-07-12 — Backlog#6 done: `risk.py` RiskEngine — pure/deterministic, 11-step first-veto-wins sequence (SELL=exit always allowed incl. breaker-tripped; breaker by state OR drawdown; liquidity None ⇒ veto ["no data, no trade"]; ADV/vol/per-trade-1.6%/cash/role-cap checks with shrink-or-veto). Hard caps re-clamped AT USE TIME (params cannot loosen: 5.0%→1.6%, −10%→−4%). **100% branch coverage verified** (149 stmts, 58 branches, 0 missed). 56 tests.
- 2026-07-12 — Backlog#5 done: `ledger.py` SQLModel ledger (signals/candidates/orders/fills/trades/snapshots; every row mode-tagged; ISO-8601 TEXT timestamps preserve tz; weighted entry, partial-close splits, proportional commission attribution, r_multiple from recorded stop; stats: win rate/payoff/expectancy/max-DD). 32 tests.
- 2026-07-12 — Backlog#4 done: `datafeed.py` YFinanceFeed (lazy yfinance import, injectable ticker_factory so tests never hit network; quote fast_info→history fallback; bars 1d/1h/1wk tz-normalized; news handles old+new yfinance formats) + StubPaidFeed placeholder (§8 data policy). 30 tests.
- 2026-07-12 — Backlog#3 done: `paper_broker.py` PaperBroker (cash reservations across resting BUYs; no-short; LMT gap-open improvement; STP slippage; MOC/LOC; volume-capped partials; BRACKET parent→OCA children activate on fill, sibling auto-cancel; DAY expiry; day-anchored drawdown). Note: per ports&adapters split, quotes/bars live on the DataFeed port, not BrokerInterface (documented deviation from §5.1's method list). 46 tests. Suite total 231 green.
- 2026-07-12 — Backlog#2 done: §6 schemas in `trader/swing_trader/schemas.py` (Signal/Order/Fill/Trade/Position/AccountSnapshot/CandidateOrder + enums; tz-aware ts enforced; bracket price-geometry validation; BUY entry candidates REQUIRE a protective stop per §4) + `interfaces.py` ports (BrokerInterface, DataFeed, Quote/Bar/NewsItem/PlaceResult). 57 tests green.
- 2026-07-12 — Backlog#1 done: `trader/` scaffold (self-contained subproject in the Hermes fork, zero Hermes-internal imports, extractable to its own repo pre-Phase-1). `Settings` (pydantic-settings; env names = §3 verbatim; hard-cap validation: per-trade >1.6% and breaker looser than −4% rejected at load; `live_orders_allowed` requires HUMAN_CONFIRM ∧ BROKER≠paper ∧ ¬DRY_RUN), structured JSON logging with secret-redaction filter, `.env.example`, README. 28 tests green (`cd trader && uv run pytest`).
- 2026-07-12 — Added §12 long-horizon value-migration framework (Perez cycle; infra→platform→application; Cisco lesson; 2026–28 turning-point window; directional rules for quarterly heavy-position review, separate from daily loop).
- 2026-07-12 — Loop.md v0: watchlist universe (AI value-chain phased), confirmed params (per-trade 1.6%, daily breaker −4%, confirm 11:30→12:30 ET), data policy (free yfinance now, paid stub), model plan (Fable 5 scaffold → Opus 4.8 refine → Sonnet 5 maintain), Hermes fork path. Awaiting scaffold.
