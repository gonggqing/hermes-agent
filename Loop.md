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

### 4b. Two daily sessions — CN morning research + US evening trading (human decision, 2026-07-13)

The user checks the system **twice a day**. Both sessions share the same `monitors → build → 11:30 push` shape but run on their own market clock/calendar:

| Session | Clock | Focus | Orders | Output |
|---|---|---|---|---|
| **CN morning** | Asia/Shanghai 09:30 → 11:00 → **11:30** | China/HK market, **technology-first** (semiconductors, electronics, AI; other sectors informative-not-focus) | **NONE** — report-only; build the ability/function for the future | a lighter **Investment Research brief** pushed to the group |
| **US evening** | ET 09:00–16:00 (§4 table) | US market (watchlist §11) | Paper (Phase 0), the full confirm→execute flow | brief + risk-checked candidate approval cards |

CN is **research-only**: it runs monitors + analysis sub-agents + the research brief but has NO decision core, NO RiskEngine execution, NO ConfirmationService, NO broker — so it structurally cannot place an order. Upgrading it to order-capable later means adding those components behind the SAME §3 authority boundaries; the mainland A-share + HK universe is config-editable and degrades to HK-only when mainland data is unreachable.

**Dual-bot roles (both bots live in the same group chat; refined 2026-07-13).** Two Telegram bots run concurrently as TWO separate processes — the Hermes gateway manages the general bot (one gateway = one `TELEGRAM_BOT_TOKEN`), and the finance service runs the finance bot (`FINANCE_TELEGRAM_BOT_TOKEN`). Hard rule: one token = one long-poller (two pollers on the same token → Telegram 409).
- **General / gateway bot** (`TELEGRAM_BOT_TOKEN`): the everyday Hermes conversational agent — it replies to the user in the group and now carries the read-only Finance toolset, so it can do complex finance analysis + real-time feedback in chat. The finance service ALSO sends daily summaries / research briefs OUTBOUND via this token (sendMessage only — it never long-polls it, so no 409 with the gateway).
- **Finance bot** (`FINANCE_TELEGRAM_BOT_TOKEN`): stays quiet by default. It proactively sends ONLY confirmations (approval cards + approve/reject buttons) when candidates need review, and it REPLIES only when directly addressed — a DM, or an @mention in the group (allowlist-gated via `TELEGRAM_ALLOWED_USERS`). A mentioned/DMed ticker gets a quick multi-agent read; it long-polls its OWN token. READ-ONLY otherwise (no order/approve beyond the human approval buttons, §3).

**Cost note:** the search/summary LLM subagent is pinned to the cheap flash model (`deepseek-v4-flash`, via `FINANCE_LLM_SEARCH_MODEL`) independent of any pricier decision model, to save token fees.

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

**Research-first information architecture (Phase 0.5 requirement):** the Finance landing view is **Investment Research**, not an order queue. A human operator should see, in priority order: (1) a dated market/risk pulse — regime, VIX/breadth, breaker, data freshness, and material exposure/cash warnings; (2) a concise daily investment brief — macro/theme changes, watchlist movers, earnings/events, news, bull/bear synthesis, confidence and uncertainty; (3) supporting source citations and links into the historical knowledge store; and only then (4) an intentionally compact **Actions requiring attention** section for pending confirmations, expiring cutoffs, failed orders, and risk exceptions. The approval queue remains immediately reachable and badged, but is never the default Desktop tab or top Web section. Every displayed claim must identify its as-of time and source/absence of source; stale or unavailable data is an explicit warning, never silently presented as current.

**UI/UX contract (applies to every Finance change):** Finance is a first-class Hermes surface, not a visually separate trading dashboard. Reuse the existing Desktop/Web application shell, routes, page primitives, design tokens, typography, spacing, responsive behavior, status patterns, loading/empty/error states, and accessible interaction conventions. Do not introduce a parallel design system, a duplicate chat UI, or finance-specific visual language that conflicts with Hermes. Desktop and Web must provide the same information hierarchy while adapting to their native layouts. Their default Finance route/tab is permanently **Investment Research**; trade Queue is a secondary, clearly badged action surface and may never become the default simply because a candidate exists.

**Translation contract (applies to Finance and every future module):** every user-visible Desktop/Web string, empty/error/loading state, action label, accessibility label, notification, and date/number label must use the same i18n/catalog conventions as the surrounding Hermes surface. Add/update translations in the relevant existing locale catalogs in the same change; do not hard-code a Finance-only English or Chinese UI. Preserve localization in tests and review locale fallback behavior whenever shared components or navigation are extended.

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

### Phase 0.5 — Research-first operator experience & resilience (AFTER Phase-0 build review; while paper data accumulates)
- **Goal:** make Finance useful to a human reader every day before it becomes a busy order console: research and risk awareness are primary; execution controls are deliberate secondary actions.
- **Build:** PaperBroker restart rehydration; an Investment Research briefing contract/API; Desktop and Web research-first landing views; source-linked knowledge ingestion/search; earnings/event calendar; explicit data-freshness and risk-warning model; dedicated Finance Telegram bot before interactive mobile approvals.
- **Acceptance:** Desktop defaults to `Investment Research`; Web presents the same research/risk summary before any queue; Queue is a compact badged action area rather than the primary canvas; all briefs show as-of time, citations/unknowns, PAPER/LIVE mode, and actionable risk warnings; no UI path gains authority beyond §3/§5.6.

### Phase 0.75 — Deepen the analysis brain + conversational finance agent (AFTER Phase 0.5; while paper data accumulates and IBKR is pending) — human decision 2026-07-13
- **Goal:** two thrusts, both bounded by §3. (A) Move *scheduled* analysis from the rule-based v0 skeleton to a genuinely informed research agent. (B) Make the **general Hermes conversational agent** finance-capable: the human can chat with the everyday Hermes bot and ask for **complex finance analysis and real-time market feedback (K-line/candlestick data + current price)** on any symbol, on demand. "Smarter" means better *analysis quality* and *on-demand access*, never more *authority*.
- **Human intent (2026-07-13):** "we want the current hermes bot (the general one) to handle complex finance analysis and give feedback on the real-time market (k-chart, current stock price)."
- **Why now:** the loop plumbing is done but the brain is shallow — fundamentals are empty, the earnings calendar is unwired, the LLM is one confidence-capped debate voice (the LLM decision core is a stub), the knowledge store doesn't feed analysis (no RAG), and memory only lowers confidence. And nothing yet lets the human *ask* for analysis interactively. Better/on-demand analysis is what decides the project's actual success metric — whether any edge survives paper→live — and it does not need IBKR.
- **Build (A — scheduled brain):** real fundamentals feed; earnings/events calendar; an LLM decision core that synthesizes monitors + sub-agents + retrieved knowledge-store research (RAG-grounded) and PROPOSES candidates only; an analysis feedback loop from closed-trade outcomes; deeper research ingestion (earnings/filings) with provenance.
- **Build (B — conversational agent):** on-demand finance-service read endpoints (real-time quote, K-line bars, one-shot multi-agent symbol analysis, research retrieval); a **fixed Finance toolset** the general Hermes agent can call (thin wrappers over the versioned finance-service API — never trader internals, per §8); real-time price/chart feedback surfaced in chat.
- **§3 authority for thrust B (HARD):** the general agent's Finance toolset is **READ / ANALYSIS ONLY** — quote, bars, analyze, research brief, knowledge search, account/portfolio *views*. It exposes **NO** order-placement or candidate-approval tool. Order authority stays service-bound to ExecutionEngine; approval stays a human-only action from an authenticated surface. This preserves §8's "isolate financial authority from unrelated conversations."
- **Acceptance:** candidates carry data-grounded fundamental + event context; the brief's "no fundamentals / earnings not wired" unknowns clear when data is present; the general Hermes agent can, in chat, return a symbol's current price + recent K-line + a multi-agent analysis with cited sources; any LLM proposal still passes RiskEngine + human approval before execution; the feedback loop adjusts analysis/signal quality only (never risk caps, position limits, or order authority); every external dependency stays mockable and tests never hit the network.

### Phase 0.8 — Resilience & observability (make it trustworthy before real money) — human decision 2026-07-13
- **Goal:** the daily loop survives real-world failure (feed outages, crashes, stale/partial data) and the operator can SEE and TRUST its state at a glance. No new authority; fail-closed everywhere.
- **Build:** DataFeed resilience (retry/backoff, per-source staleness guards, a backup-feed stub behind the `DataFeed` port); loop-step idempotency + mid-session crash recovery (extends rehydration); a system-health / heartbeat model (loop ran? feed fresh? breaker? service up?) surfaced to the reporter bot + the Finance tab; a **dead-man's switch** that halts NEW entries when the loop/data is unhealthy (research-dependent entries fail closed, per §5.10); ledger↔broker reconciliation check.
- **Exit:** injected-failure tests (feed down, crash mid-loop, stale data) all fail closed; the health surface shows green/red with reasons; an unhealthy loop physically cannot place a new entry.

### Phase 0.9 — Broker-integration backbone (IBKR-shaped, buildable without the live account) — human decision 2026-07-13
- **Goal:** build and harden the REAL broker adapter + order lifecycle so IBKR slots in cleanly the moment the account funds, and establish one trustworthy multi-account portfolio record that Hermes can reason about without pretending that manually reported holdings were system-executed trades. Validate fully offline against mocks (and IBKR paper when TWS is available). This front-loads the Phase-1 *code* during the account wait, leaving Phase 1 to be "connect + validate + go tiny-live."
- **Build — broker backbone:** `IBKRBroker` (ib_async) implementing `BrokerInterface` — bracket/OCA mapping, MOC/LOC/GTC, **client-order-id idempotency**, partial/reject/cancel/timeout handling, reconnection + pacing, and **T+1 settled-cash** tracking (HK cash account, §2); a mock IB transport so the whole place→partial→fill→cancel→reject lifecycle is unit-tested offline (same suite the PaperBroker passes); order-state **reconciliation on restart**; a **paper↔live ledger-comparison harness** (measures the sim→real gap) reusing the backtest code paths.
- **Build — Portfolio foundation:** add a dedicated Portfolio page/view within the existing Finance Portal (Desktop + Web, Hermes-native components/i18n) for manual account setup, opening-position entry, manual trade/event recording, CSV import and later IBKR Flex import. Support aggregated portfolios across US, HK and mainland China while preserving account/market/source attribution. The symbol field must provide type-ahead search by partial ticker or name and return likely stocks, ETFs and funds with exchange, currency and security type so the user does not need the complete code.
- **Portfolio authority:** for **US and HK**, connected IBKR positions, executions, orders, cash and settled-cash are authoritative; manual/imported records bootstrap history and other accounts but must never silently override broker state. For **mainland China**, the authoritative Phase-0.9 record is a human-confirmed manual/imported portfolio event because IBKR is not the execution source. Any broker/manual discrepancy is surfaced as reconciliation drift, never silently merged or discarded.
- **Conversation contract:** Hermes may turn a statement such as “today I bought/cleared…” into a structured portfolio-event **draft**, but free-form conversation can never mutate holdings directly. The human must review/confirm or edit the draft in an authenticated Finance/Telegram surface before an append-only event is recorded. Opening balances and externally executed trades remain distinct from system candidates/orders/fills so they do not contaminate strategy win-rate, execution attribution or audit history.
- **Exit:** `IBKRBroker` passes the mock-exchange lifecycle tests offline; reconciliation detects drift; the comparison harness runs on paper data; the Portal can initialize and aggregate US/HK/CN holdings with searchable instruments and an audited manual/import workflow; conversational updates require explicit human confirmation; source precedence is test-enforced; still NO live orders (the §3 triple gate `HUMAN_CONFIRM ∧ BROKER≠paper ∧ ¬DRY_RUN` is untouched).

### Phase 0.95 — Pre-live validation gate (readiness checkpoint) — human decision 2026-07-13
- **Goal:** prove the whole system is safe and reproducible before any real money.
- **Build:** complete the reviewed weekend **upstream Hermes sync merge** (Phase-1 prerequisite, §8); a full end-to-end dry run on IBKR **paper** (once TWS is up) mirroring the §4 daily loop; expand the backtest to walk-forward across ≥2 regimes; a documented **go-live runbook + kill-switch drill**; confirm progress toward the **≥20 paper-day** Phase-0 exit criterion.
- **Exit ⛔ Review Gate:** upstream sync merged + reported; IBKR-paper dry run clean; a guardrail audit passes end-to-end; runbook + kill switch verified; **human sign-off** to enter Phase 1.

### Phase 1 — Shadow & tiny live (AFTER IBKR opens & funds)
- **Goal:** implement `IBKRBroker` (ib_async); run on **IBKR paper first**, then **tiny real money (a few hundred USD)**.
- **Target:** quantify the **sim→real gap** (slippage, fill quality, timing) using paper-vs-live ledger comparison.
- **Exit criteria:** ≥ 20 tiny-live trades; measured slippage/fill stats; loop stable under real fills; no guardrail breaches. Before entering Phase 1, complete at least one reviewed upstream Hermes sync cycle (§8 Fork maintenance) so live-trading work starts from a known, tested base.

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

**Fork maintenance (weekly, and before Phase 1):** this repository is a fork of the official Hermes Agent `main`. Each weekend, fetch the official `upstream/main`, compare it with our `main`, and create a dated integration branch for review. Inspect upstream changes for security fixes, gateway/platform behavior, provider/model changes, Desktop/Web design-system changes, and migration requirements. Resolve conflicts deliberately — preserve Finance authority boundaries, tests, translations, and the Hermes-native UI contract — then run the relevant full Python, Desktop/Web, Docker, and Finance test suites before merging. Never use `reset --hard`, force-push, or an unattended automatic merge; an upstream sync report must record compared commits, adopted/skipped features, conflicts/resolutions, tests, and any follow-up migrations. Keep Finance code isolated behind its service/API boundary so upstream integration remains tractable.

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

### Phase 0.5 backlog (do not start until the Phase-0 build review is approved)

- [x] PaperBroker rehydration from Ledger across Finance-service restart, including open orders/protective stops + tests
- [x] Research-first `Investment Research` briefing API/model: dated market regime, risk/freshness warnings, themes/movers, events/earnings, news/debate synthesis, confidence/uncertainty, and provenance links
- [x] Desktop: make `Investment Research` the default Finance tab; move Queue to a secondary badged action tab
- [x] Web: make the research/risk brief the top Finance section; render Queue as compact `Actions requiring attention`, expanded only for pending/expiring/problem states
- [x] Knowledge ingestion and semantic search: daily research/news/earnings → facts/documents/vector index; source-linked research search in Desktop/Web
- [x] Dedicated Finance Telegram bot + authenticated interactive approval adapter (existing Hermes bot remains gateway-only) — **DONE: human created the bot; FINANCE_TELEGRAM_BOT_TOKEN is set; interactive allowlist-gated approvals live**
- [x] Configure official `upstream` remote and complete a reviewed, tested Hermes `main` sync dry run before Phase 1; establish weekly weekend sync reports/integration branches (dry run 2026-07-13: 67 commits behind, ZERO conflicts — report in docs/upstream-sync/; real merge = weekend reviewed task)

### Phase 0.5+ backlog — two-session (CN morning) & dual-bot roles (2026-07-13)

- [x] Distinct dual-bot roles in the shared group: reporter (shared gateway bot, outbound-only summaries/briefs) vs gatekeeper (dedicated finance bot, interactive approval-only). Split transports; `on_push` sends preamble via reporter and cards via gatekeeper.
- [x] `SessionSchedule` scheduler generalization (US + CN) preserving all US behaviour/tests; CN calendar (Asia/Shanghai, combined mainland+HKEX 2026 holidays, TODO authoritative source).
- [x] CN morning **research-only** session: `research_session.ResearchSession` (monitors + technical/sentiment/debate + optional LLM → lighter research brief; NO orders/decision-core/risk-exec/confirmation/broker). `cn_watchlist` (mainland A-share + HK, tech-focused, config-editable via `FINANCE_CN_SYMBOLS`, graceful HK-only degrade).
- [x] `build_research_brief` params (`watchlist_lookup`, `trading_tz`, in-memory `signals`/`candidates`, `include_account`) so CN research never touches the US trading ledger; CN brief served at `/v1/research/brief?market=cn`; Telegram brief renderer (`brief_telegram`, zh/en).
- [x] Search/summary LLM pinned to `deepseek-v4-flash` (`FINANCE_LLM_SEARCH_MODEL`, role-based) to save token fees, independent of any decision-tier model.
- [x] Complete Finance i18n across all Web + Desktop surfaces (en+zh) and add a US / China·HK market toggle to the Investment Research view (`?market=cn`). — DONE: Web migrated ~63 hard-coded strings (ApprovalQueue/HistorySection were 100% English) + CN toggle (RiskStrip/queue/account hidden in CN research-only mode); Desktop migrated ~38 (sidebar nav label + a `finance.enums` catalog localizing all backend enum vocabularies) + CN toggle. Web typecheck/lint/build green; Desktop typecheck/lint green.

### Phase 0.75 backlog — deepen the analysis brain (2026-07-13)

> §3 invariant for EVERY item: analysis quality only. The LLM never approves; RiskEngine stays authoritative; human approval unchanged; all deps mockable; tests never hit the network.

- [x] Real fundamentals provider (yfinance-backed, behind the existing `FundamentalsProvider` port; per-symbol cached; any failure → None) wired into the US loop so `FundamentalAgent` produces real signals — DONE: `fundamentals.YFinanceFundamentals` (P/E, fwd P/E, revenue growth, margins; injectable ticker factory; TTL cache incl. negatives; fail-None). Wired into the US `DailyLoop` and on-demand `/v1/analyze`. 6 tests.
- [x] Earnings/events calendar feed (behind a mockable port) wired into the brief `EventsView` + a flag — DONE: `earnings.YFinanceEarnings` (injectable date fn, TTL cache, fail-None) + `upcoming_earnings()`; `build_research_brief(earnings=...)` populates `EventsView` and adds an "earnings imminent (≤5d) — avoid opening into the print" warning; the brief's "earnings not wired" unknown clears. Wired into the US loop. 6 tests. (Decision-core AUTO-avoidance of pre-earnings entries is the next layer.)
- [x] **RAG-grounded LLM analysis + pre-earnings avoidance** — DONE: `rag.py` (`retrieve_research` fail-closed over the knowledge store, `research_snippets`/`research_sources`); `LLMAnalyst.analyze(research=...)` grounds its thesis on retrieved, source-cited docs; wired into BOTH the scheduled loop (`_build_signals`) and on-demand `analyze_symbol` (result now carries `research` citations, surfaced via `/v1/analyze` + the finance bot + `research_brief` tool). Decision core: `propose(earnings_symbols=...)` never opens a fresh entry into an imminent-earnings print (exits unaffected). PROPOSES-ONLY preserved — order geometry/sizing stays deterministic, RiskEngine + human approval unchanged (§3). 8 new tests. (A full LLM-authored candidate rationale + replacing the rule-based core is a later, optional step; the current rule-geometry + RAG-LLM-voice is safer.)
- [ ] Analysis feedback loop: closed-trade outcomes adjust analysis/signal quality (never risk limits/caps/authority)
- [x] Deeper research ingestion — DONE (v1): `research_ingest.py` archives per-symbol **fundamentals** + **earnings-calendar** documents into the knowledge store (provenance-mandatory Yahoo source, content-hash deduped so unchanged docs aren't re-indexed, fail-closed on vector trouble), wired into the US loop's `on_monitor`. RAG can now cite real fundamentals/earnings substance, not just news (a test proves `search_knowledge` retrieves an ingested fundamentals doc). 7 tests. (SEC EDGAR filing ingestion = future extension.)

**Thrust B — conversational finance agent (general Hermes bot; READ/ANALYSIS ONLY, no order/approve tools):**

- [x] Finance-service on-demand read endpoints: `/v1/quote`, `/v1/bars` (K-line OHLCV), `/v1/analyze` (one-shot technical+fundamental+sentiment+debate). `FinanceRuntime` gained feed/fundamentals/llm_analyst; honest delay note; 6 tests. Verified LIVE (NVDA quote 210.96; analyze verdict long 0.57).
- [x] Fixed **Finance toolset** registered with the general Hermes agent (`tools/finance_tools.py`, toolset `finance`, added to `_HERMES_CORE_TOOLS`, auto-enables on `HERMES_FINANCE_SERVICE_URL`): get_quote, get_kline, analyze_symbol, research_brief, search_research, account_view — thin httpx wrappers over the service API, NO place_order/approve tool (test-asserted). 9 tests.
- [x] Real-time market feedback in chat: DONE. K-line rendered as a **Skill** (`skills/finance/kline-chart/`, frontmatter-gated to detailed single-stock requests to save tokens) whose `scripts/render_kline.py` fetches `/v1/bars` itself (bars never enter the transcript) and renders a candlestick PNG (Pillow, zero new deps); the agent replies with a one-line summary + `MEDIA:<png>` which every chat surface renders. Verified live in the container for US + HK (NVDA, 0700.HK). Installed to `~/.hermes/skills` (mounted) + committed to repo.

### Phase 0.8 backlog — resilience & observability (2026-07-13)

> §3 invariant for EVERY item: NO new authority. Fail-closed everywhere. The RiskEngine stays authoritative; exits/protection are NEVER gated; the human approval path is untouched; all deps mockable; tests never hit the network.

- [x] **Dead-man's switch** in the authoritative RiskEngine — DONE: `evaluate(..., system_healthy: bool)`; when False every NEW entry is VETOED *after* the SELL/exit path (protection always flows) and *before* every other entry check. Pure/deterministic; 100% branch coverage held (4 new tests).
- [x] **System-health / heartbeat model** — DONE: `health.py` (`assess_health` → `HealthStatus{level, entries_allowed, checks, warnings}`, `HealthLevel` ok/degraded/unhealthy, `STALE_AFTER_MINUTES=120`). `entries_allowed` = market&portfolio FRESH ∧ ledger↔broker reconciled; news staleness only degrades (never blocks); breaker reported (enforced separately in RiskEngine, not double-vetoed). Pure, never raises. 16 tests.
- [x] **Ledger↔broker reconciliation** — DONE: `reconcile.py` (`reconcile_broker_ledger` compares broker positions vs ledger-fill-derived positions, BUY+/SELL−, fractional tolerance; never raises → fail-closed `ok=False` on error). 13 tests.
- [x] **DataFeed retry/backoff** — DONE: `datafeed.RetryingFeed` wraps any `DataFeed`, retries transient `DataFeedError` with exponential backoff (injectable sleep; `ValueError` not retried; final error re-raised → still fail-closed). Wraps the live YFinance feed for the US loop AND the CN research session. 6 tests.
- [x] **Wired end-to-end + surfaced** — DONE: `on_decide` assesses health, passes `system_healthy` to the RiskEngine, alerts the reporter bot (plain-language halt reason) when new entries are paused; human edits re-validate under the switch too; health published on the runtime and exposed via `/v1/health` (level, entries_allowed, per-check reasons) for the Finance tab. Monitors now stamp snapshots with the loop clock so freshness is consistent under the simulator. Integration + API tests prove an unhealthy loop physically cannot open a new entry. Suite 786.
- [ ] Loop-step idempotency + mid-session crash recovery (extends rehydration): re-running a session step must not double-place or double-count.
- [ ] Backup-feed stub behind the `DataFeed` port (secondary source failover) + per-source staleness guards on the health surface.

### Phase 0.9 backlog — broker backbone + Portfolio foundation

> Portfolio facts affect sizing and risk, so they require the same provenance, idempotency, auditability and fail-closed behavior as broker facts. LLM memory is never a holdings database; manual records are never fabricated broker fills; source conflicts are visible to the human.

- [x] **Multi-account Portfolio Journal:** manual accounts, opening balances and append-only portfolio events (buy/sell/dividend/fee/transfer/corporate action) with account, market, currency, source, timestamps, optional cost basis and idempotency keys; derive current holdings without rewriting history. — DONE: `portfolio.py` (domain + pure `derive_holdings`) + `portfolio_journal.py` (`PortfolioJournal`, SQLModel, append-only events + mutable account config, idempotent by key/external_id, compensating CORRECTION events). Holdings/cash derived from events alone; unknown cost = None. 47 tests incl. no-ledger-contamination.
- [x] **Finance Portfolio entry surface:** native Desktop + Web page/view for opening-position entry, manual trade recording, review/edit/delete-by-compensating-event, provenance and staleness; complete translations alongside both surfaces. — DONE: HTTP `/v1/portfolio/*` API + draft-only Hermes tools + a "My holdings" sub-view on BOTH surfaces (Desktop `finance/holdings.tsx` react-query/cmdk; Web `finance/PortfolioManager.tsx` plain-fetch): master-detail Accounts/Holdings/Activity/Reconcile, Record-trade with instrument type-ahead → draft→confirm, Drafts review, CSV import, append-only correction ("delete"). Unknown cost shows localized "unknown". i18n en/zh(+desktop ja/zh-hant). Both typecheck/build green.
- [x] **US/HK/CN instrument search:** type-ahead by partial symbol or company/fund name; results identify symbol, display name, exchange, currency and security type (stock/ETF/fund), behind a mockable search provider with cached metadata and no network in tests. — DONE: `instruments.py` (`normalize_symbol` US/HK/CN, `StaticInstrumentProvider` offline catalog w/ Chinese aliases, `CachedInstrumentSearch` TTL + degraded-not-silent); `/v1/instruments/search`. 36 tests.
- [x] **Import/bootstrap:** reviewed CSV import for manual/other-broker portfolios, followed by IBKR Activity/Flex import; preview, validation, deduplication and explicit commit are mandatory. Imports must not create system-generated candidates/orders/fills. — DONE (CSV): `portfolio_csv.py` (`parse_csv` preview + `commit_csv`, dedup, idempotent) + `/import/preview|commit`. IBKR Flex import = future (network) extension.
- [x] **Conversational portfolio drafts:** a read-safe Finance capability that parses user-reported trades/clears into drafts; only an authenticated human confirmation writes the append-only event. Ambiguous account, symbol, quantity, price, currency or execution status must be clarified, never guessed. — DONE: `portfolio_draft.py` (`PortfolioDraftService` — human-only confirm, refuses system/LLM, missing/ambiguity gating, idempotent+versioned, audits every attempt) + draft-only Hermes tools (`draft_portfolio_trade`/`draft_close_position`; NO confirm tool).
- [x] **Authority + reconciliation:** US/HK IBKR state wins when connected; CN human-confirmed manual/imported events are authoritative in Phase 0.9; other external accounts retain their declared source. Aggregate without double counting, surface drift with reasons, and block risk-dependent new entries when authoritative portfolio state is stale or unresolved. — DONE (drift): `portfolio_reconcile.py` (broker vs manual authority, drift surfaced not merged) + `/reconcile`. Feeding unresolved drift into the P0.8 health dead-man's-switch = wired when IBKR lands (Phase 1).
- [x] **Risk/analysis projection:** expose the aggregated, source-tagged portfolio to `account_view`, Finance research and RiskEngine exposure calculations while keeping execution scoped to the selected broker account; unknown cost basis remains unknown and is never synthesized by the LLM. — DONE (aggregate): `aggregate_holdings` + `/v1/portfolio/aggregate` (source-tagged, no double-count, unknown cost preserved). RiskEngine exposure now reads paper-broker positions; wiring the manual aggregate into live exposure lands with the IBKR account (Phase 1) to keep the 100%-branch RiskEngine authority untouched pre-live.

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

- 2026-07-13 — **P0.9 Portfolio Foundation (6 commits).** Auditable multi-account US/HK/CN Portfolio Journal kept SEPARATE from the trading ledger (boundary #1): append-only `PortfolioEvent`s → holdings/cash DERIVED (`derive_holdings`; unknown cost stays null, never guessed); `instruments.py` US/HK/CN type-ahead (offline+cached, degraded-not-silent); human-only draft→confirm (`portfolio_draft.py` — system/LLM actor or `system` surface refused 403 `not_human` + audited; incomplete/ambiguous blocked; idempotent+versioned; every attempt audited); `/v1/portfolio/*` API (accounts/holdings/events/drafts/action/aggregate/reconcile/import/correct-draft); CSV import (preview→dedup→commit); reconciliation (broker vs manual authority, drift surfaced not merged); aggregate (source-tagged, no double-count); append-only "delete" = CORRECTION event reversing a prior one (history preserved); draft-only Hermes tools (surface=system → cannot self-confirm; NO confirm/place tool); Desktop `finance/holdings.tsx` (react-query+cmdk) + Web `finance/PortfolioManager.tsx` (plain-fetch) "My holdings" sub-view (Accounts/Holdings/Activity/Reconcile + Record-trade + Drafts review + CSV), i18n en/zh(+desktop ja/zh-hant). Storage = SQLite/SQLModel (`portfolio_*` tables, same file as ledger, no shared tables). 935 trader + 15 tool + 81 web tests; RiskEngine still 100% branch; independent agent verified 11/11 lifecycle steps live. Deferred to Phase 1 (w/ IBKR): drift→dead-man's-switch + live RiskEngine exposure; IBKR Flex import.
- 2026-07-13 — **P0.8 Resilience & observability.** Dead-man's switch in the authoritative RiskEngine (`evaluate(system_healthy=)` halts NEW entries when data stale / ledger-broker drift; exits always flow; **100% branch held**); `health.py` heartbeat (`HealthStatus`, `entries_allowed`); `reconcile.py` ledger↔broker; `datafeed.RetryingFeed` backoff. Wired into `on_decide` (+ reporter alert) and `/v1/health`; monitors stamp snapshots with an injectable clock (sim-consistent). 42 tests; suite 786.
- 2026-07-13 — **Pro TradingView-style K-chart (klinecharts)** on Desktop+Web — one large chart, symbol dropdown, timeframe switcher 1D/5D/D/M/Y (auto date range; DataFeed extended to intraday+monthly), MA20/30 curves, indicator toolkit w/ short educational descriptions, currency+unit prices, AU9999 (国内金价) derived (GC=F×CNY=X/31.1035), drag-to-load history, reset-on-switch. (Finance chart is now klinecharts, not inline SVG.)
- 2026-07-13 — **Finance UI iterations** — messaging-style master-detail redesign (Research/Queue/Portfolio → sidebar/detail/bottom switcher; Markets US/China/HK + Gold/Oil/Rates/Crypto watch modules; China/HK frontend-partitioned from one CN brief); candlestick hover/crosshair; cached bars w/ live last candle; one Analyze button; bottom status bar; single paper/live switcher.
- 2026-07-13 — **Model switch → MiniMax** (M3 daily, M2.7-highspeed subagents) + **deeper research ingestion** (`research_ingest.py`: fundamentals+earnings docs → knowledge store, content-hash deduped, fail-closed).
- 2026-07-13 — **P0.75 brain + conversational finance agent** — real fundamentals (`YFinanceFundamentals`); on-demand `/v1/quote|bars|analyze`; read/analysis-only **finance toolset** for the general Hermes bot (NO order/approve, test-asserted); **K-line chat skill** (frontmatter-gated); earnings calendar (`earnings.py`) + pre-earnings-avoidance; **RAG-grounded** `LLMAnalyst.analyze(research=)` (`rag.py`, fail-closed). Roadmap phases 0.8/0.9/0.95 defined.
- 2026-07-13 — **Two-session + dual-bot** — `SessionSchedule` generalizes US evening (ET, full trade flow) + CN morning research-only (Asia/Shanghai; `ResearchSession`, no order authority); reporter bot (shared, outbound-only) vs gatekeeper bot (dedicated finance token, interactive approvals + @mention/DM); CN watchlist (config-editable, HK-degrade); search LLM pinned cheap.
- 2026-07-13 — **P0.5 research-first + resilience** — ResearchBrief-first UI on both surfaces (Queue badged/secondary), `brief.py` + knowledge pipeline + `/v1/research/brief` + `/v1/knowledge/search`; rehydration from ledger (restart-safe paper account + protective stops); Telegram allowlist auth + dedicated-bot support; upstream sync dry-run (0 conflicts, report kept). i18n en/zh.
- 2026-07-12/13 — **Standing contracts** — Finance inherits the Hermes shell/tokens/state/i18n (no separate dashboard, no duplicate chat); **Investment Research is the permanent default Finance route**, Queue is secondary/badged; every UI string ships translations; weekly reviewed `upstream/main` sync (≥1 before Phase 1, never auto-merge).
- 2026-07-12 — **Phase 0 COMPLETE (⛔ approved).** Full timezone-aware daily loop (`dailyloop.py` §4) + `simulate`/backtest + E2E (22 sim days, crash-day). Core: `risk.py` RiskEngine (pure/deterministic, first-veto-wins, per-trade ≤1.6% / breaker −4%, 100% branch gate); `confirmation.py` ConfirmationService (server-authoritative; LLM/system publish+expire but NEVER approve; every attempt incl. refused audited w/ actor+surface+version+idempotency); `execution.py` (BRACKET entries, human-approved only, re-validates, re-places stops); `ledger.py` SQLModel (mode-tagged rows); `paper_broker.py`; `monitors.py`; `scheduler.py` (zoneinfo state machine); `datafeed.py` (YFinance, injectable, offline tests); `decision.py`; §6 `schemas.py`; Web+Desktop Finance tab; knowledge store + Qdrant. Deployed; LLM analyst enabled. (Granular per-backlog build entries collapsed here — recoverable via git log.)
- 2026-07-12 — **Loop.md v0** — watchlist universe (AI value-chain phased), confirmed params (per-trade 1.6%, daily breaker −4%), §3 guardrails, and §12 long-horizon value-migration framework (Perez cycle: infra→platform→application; Cisco lesson).
