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
- **User availability (critical):** the US decision cycle must finish early in the session. The agent observes the open, **finalizes candidates and pushes them at 10:30 ET** (22:30 Shanghai during EDT), then leaves a full hour for the user to confirm **by 11:30 ET**.
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
| 09:30–10:00 | 21:30–22:00 | Monitors poll; analysis sub-agents build theses |
| 10:00–10:30 | 22:00–22:30 | Decision core aggregates → candidate orders; Risk Engine validates |
| **10:30** | **22:30** | **Publish risk-approved candidates to the Finance portal and push Telegram cards** |
| 10:30–11:30 | 22:30–23:30 | **User approves / edits / rejects from Desktop, Web, or Telegram**; approved → placed (GTC limit/stop or MOC/LOC) |
| 16:00 | 04:00 | MOC/LOC fills; resting GTC orders may fill |
| next 09:00 | next 21:00 | Reporter: overnight fills, ledger update, morning summary |

Order-type policy for approvals: **entries** = GTC limit or MOC/LOC; **protection** = GTC stop-loss (attach on entry fill via bracket/OCA); never leave a position without a resting stop.

Human-facing research delivery uses a separate Finance-owned Beijing cadence:
**every calendar day at 09:00 and 21:00 Asia/Shanghai**, one non-blocking
coordinator refreshes US/HK/CN/KR through the same evidence pipeline, asks the
configured Hermes primary model for each final market narrative, persists the
four results, then pushes them. Market-specific intraday jobs may continue to
refresh structured evidence, but they do not send an additional human brief or
erase the latest scheduled narrative. The Web/Desktop tab only reads persisted
results; Hermes conversational cron is not the source of truth for this cycle.

**Research-quality contract:** article publication/event time, not poll time,
determines freshness. A headline older than 72 hours is historical context and
cannot enter current sentiment or be called a catalyst; duplicate URL/headline
events are counted once, and vector-retrieved older research is always dated.
The primary-model brief receives prior distinct publications plus deterministic
regime/theme/signal/news deltas and read-only live holdings. It must explain
what changed, maintain or invalidate prior theses, and emit a structured trend
action map (`buy_on_confirmation` / `hold` / `reduce_on_weakness` /
`exit_if_invalidated` / `watch` / `avoid`) with horizon, confidence, evidence
and an observable invalidation. Technical alignment alone cannot justify a
buy-on-confirmation view. Intraday refreshes update volatile evidence only;
only canonical 09:00/21:00 primary-model editions enter brief/prediction history.

Local Docker operations are documented in `docker/README.md` and use the
non-destructive `docker/compose.sh` wrapper: `./docker/compose.sh up`,
`./docker/compose.sh rebuild`, `./docker/compose.sh logs finance`, and
`./docker/compose.sh health`. Runtime secrets remain in `~/.hermes/.env`;
behavior/model routing remains in `~/.hermes/config.yaml`.

### 4b. Two daily sessions — CN morning research + US evening trading (human decision, 2026-07-13)

The user checks the system **twice a day**. Each session runs on its own market clock/calendar; an execution-capable market completes candidate selection at 10:30 local and preserves a one-hour human window, while research-only sessions may retain a later evidence pass:

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
- Adapters: **`PaperBroker`** (deterministic fills plus currency-isolated cash/reservations), `AlpacaPaperBroker` (optional), and **`IBKRBroker`** (offline-tested `ib_async` adapter with SMART/USD and qualified SEHK/HKD contracts; connected-paper acceptance remains required).
- `DataFeed` interface (`get_quote`, `get_bars`, `get_news`): adapters `YFinanceFeed` (now), IBKR feed (later)

**5.2 Monitors** (scheduled pollers; each persists timestamped snapshots)
- `MarketMonitor` (indices, VIX, breadth, risk-on/off)
- `PortfolioMonitor` (holdings + watchlist: chips/AI/storage/optical/grid + S&P/Dow)
- `NewsMonitor` (earnings calendar, macro, breaking; sentiment scoring)
- `CryptoMonitor` (optional; only after OSL permission + API support confirmed)
- `AccountRiskMonitor` (equity, P&L, drawdown, per-pool exposure, breaker status)

**5.3 Analysis sub-agents** — Fundamental, Technical, Sentiment/News, Macro; plus a **Debate agent** (bull vs bear). Start rule-based; upgrade to LLM.

**5.4 Decision core** (LLM; Hermes runtime) — consumes monitors + sub-agents + **memory** (user risk profile, past skills, trade journal) → candidate orders `{symbol, side, qty, order_type, limit, stop, tp, sl, rationale, confidence}`.

**5.5 Portfolio controls** — the operator owns durable Web/Desktop controls for total invested target/tolerance, Agent capital window/tolerance, single-position cap, per-trade risk and daily new-position count. The Agent window is a subset of total invested allocation, never additive. Cash and reservations remain currency-specific; aggregate allocation is measured in the configured base currency using explicit FX marks, and a stock order may never trigger implicit conversion.

**5.5 Risk Engine** (pure code, authoritative) — size cap, exposure caps, daily drawdown breaker, liquidity/volatility checks; may **veto** or **shrink size**; agent cannot override.

**5.6 Confirmation service & gateways** — one server-authoritative candidate state machine shared by Desktop, Web, and Telegram. Render concise Telegram cards and native portal approval UI; collect approve/edit/reject; enforce the 10:30–11:30 ET window; expire stale candidates. A canonical candidate ID, idempotency key, authenticated actor/source (`desktop|web|telegram`), and immutable audit trail prevent double execution. After the first human approval, a fresh-quote/news primary-model review runs before submission: unchanged terms retain the approval with an audited review; any material price/risk/thesis change supersedes the old candidate with a **new id** and a clearly explained second-confirmation card. The LLM may recommend or explain but never approve; revised terms re-pass CandidateOrder validation and RiskEngine, missing/stale/model-failed reviews fail closed, and stale buttons on the first card cannot approve the replacement.

**5.7 Execution & authority boundary** — translate human-approved candidates to broker calls; prefer GTC limit + attached GTC stop (bracket/OCA) or MOC/LOC; **re-validate price vs signal validity before send**; handle partials/rejects. `place_order` is a service capability exposed only to ExecutionEngine, not a generic conversational skill. In Phase 3, an independently versioned Quant executor may use the same path for a pre-approved, low-notional strategy whitelist; it must identify itself as `quant:<strategy_version>`, satisfy all existing RiskEngine/ledger gates, and be instantly disabled by the human kill switch.

**Ordinary-order lifecycle:** use `DAY` for an ordinary entry parent and discretionary limit exit; keep only protective stop-loss/take-profit children `GTC`. At market close, cancel any unfilled entry remainder and persist `unfilled/expired`; after a partial fill, cancel the remainder while retaining/resizing GTC protection to the filled quantity. The next trading day must run fresh research, quote/news review and RiskEngine checks, generate new terms, and request a new human confirmation rather than silently replaying or repricing the old approval. Any material change to limit, stop or take-profit invalidates the prior approval and requires a second confirmation. Bounded automatic repricing may be considered only after this deterministic cancel/expire/research/reconfirm lifecycle is proven.

**5.8 Ledger & durable market memory** — SQLite ledger stores signals, orders, trades (`mode = paper|live`), fills, pnl, rationale, and approval audit events; feeds statistics (win rate, payoff ratio, max drawdown). Monitor snapshots and fetched source documents are retained by trading date rather than discarded. The ledger remains the authoritative source for numerical/accounting facts; no vector index may be treated as an order, fill, or risk record.

**5.9 Finance portal (Desktop + Web)** — add a permanent `Finance` tab to the existing Hermes Desktop and Web applications. Match their current UI/UX, routes, design tokens, state patterns, and shared components; do not create a second dashboard or re-implement chat. The tab is a native, structured companion surface with paper/live mode switch, market regime/watchlist, positions, open orders, risk/breaker state, candidate approval queue, fills/audit timeline, daily reports, and historical research search. Start read-only; write actions are limited to the same Confirmation service described in §5.6.

**Research-first information architecture (Phase 0.5 requirement):** the Finance landing view is **Investment Research**, not an order queue. A human operator should see, in priority order: (1) a dated market/risk pulse — regime, VIX/breadth, breaker, data freshness, and material exposure/cash warnings; (2) a concise daily investment brief — macro/theme changes, watchlist movers, earnings/events, news, bull/bear synthesis, confidence and uncertainty; (3) supporting source citations and links into the historical knowledge store; and only then (4) an intentionally compact **Actions requiring attention** section for pending confirmations, expiring cutoffs, failed orders, and risk exceptions. The approval queue remains immediately reachable and badged, but is never the default Desktop tab or top Web section. Every displayed claim must identify its as-of time and source/absence of source; stale or unavailable data is an explicit warning, never silently presented as current.

**UI/UX contract (applies to every Finance change):** Finance is a first-class Hermes surface, not a visually separate trading dashboard. Reuse the existing Desktop/Web application shell, routes, page primitives, design tokens, typography, spacing, responsive behavior, status patterns, loading/empty/error states, and accessible interaction conventions. Do not introduce a parallel design system, a duplicate chat UI, or finance-specific visual language that conflicts with Hermes. Desktop and Web must provide the same information hierarchy while adapting to their native layouts. Their default Finance route/tab is permanently **Investment Research**; trade Queue is a secondary, clearly badged action surface and may never become the default simply because a candidate exists.

**Translation contract (applies to Finance and every future module):** every user-visible Desktop/Web string, empty/error/loading state, action label, accessibility label, notification, and date/number label must use the same i18n/catalog conventions as the surrounding Hermes surface. Add/update translations in the relevant existing locale catalogs in the same change; do not hard-code a Finance-only English or Chinese UI. Preserve localization in tests and review locale fallback behavior whenever shared components or navigation are extended.

**5.10 Finance knowledge store (historical research + semantic retrieval)** — persist collected daily research, financial news, earnings/quarterly reports, company filings, strategy notes, monitor snapshots, decision rationales, and post-trade reviews. Use three layers:
- **facts:** immutable source files and normalized structured data, partitioned by event date/trading date (JSONL/Parquet for market/news snapshots; SQLite ledger for trading records);
- **research documents:** normalized text with source URL/publisher, retrieval date, content hash, symbol/theme, event timestamp, trading date (ET), document type, entitlement/license status, and parser/model version;
- **local vector index:** production semantic retrieval uses OpenAI `text-embedding-3-small` at native 1536 dimensions in versioned collection `finance_knowledge_openai_te3s_1536`; the retired 256-dimension hashing collection has been backed up and removed after count/search verification. Embeddings point back to document IDs and metadata and never replace sources, deterministic market data, predictions, or the Ledger.

For an initial small local corpus, embedded/local Qdrant persistence is acceptable. Before the Finance service becomes long-running, run Qdrant as a dedicated private Docker service (for example `hermes-finance-vector`) with a named/host-mounted data volume, backup procedure, and **no published host port**; only Finance-service containers may connect over the internal Docker network. The vector database is storage/search infrastructure, not an execution dependency: if it is unavailable, trading must fail closed for research-dependent new entries and never lose or alter Ledger records.

Use public/owned/licensed material only. Public investor-relations filings and openly published research may be ingested with provenance; Morgan Stanley, J.P. Morgan, Goldman Sachs, Citi, and similar publisher research may be indexed only when the user has legitimate access and the publisher's terms permit local retention/processing. Never bypass paywalls, credentials, robots controls, copyright restrictions, or redistribute report text. Preserve citations and return source links/snippets rather than treating third-party reports as untraceable model facts.

**Prediction evaluation boundary:** Qdrant is retrieval infrastructure only. Persist every measurable Debate/discovery/brief claim, confidence, horizon, evidence link, revision and deterministic outcome in a separate relational prediction ledger; preserve prose briefs as presentation artifacts. Use append-only revisions, trading-session checkpoints and weekly/monthly/quarterly calibration reviews. Do not add a graph database until real multi-hop requirements exceed relational joins. Full schema and scoring policy: `docs/finance-prediction-ledger.md`.

**Prediction-review UI:** place prediction history and evaluation under **Finance → Investment Research → Research Review → Prediction Review**; expose walk-forward/OOS **Strategy Backtests** as a separate Quant child item beside Prediction Review rather than mixing it into prediction outcomes. Keep four result classes visibly separate: live research forecasts, deterministic forecast evaluation, Quant backtests, and actual account/trade performance. Never present one blended “win rate”; every statistic shows producer, market, horizon, completed sample count and evaluation version. The close evaluator now scores only completed, adjusted market paths; pending, missing-data and correlated horizons remain visible but never count as misses or independent samples.

---

## 6. Core data schemas (initial; evolve as needed)

- `Signal(id, ts, source_agent, symbol, thesis, direction, confidence, features_json)`
- `Order(id, ts, mode, symbol, currency, side, qty, order_type[LMT|STP|MOC|LOC|BRACKET], limit, stop, tp, tif[GTC|DAY], status, broker_ref)`
- `Trade(id, entry_order_id, exit_order_id, symbol, qty, entry_px, exit_px, pnl, r_multiple, hold_days, rationale, mode)`
- `Position(symbol, currency, qty, avg_px, mkt_px, upnl, pool)`
- `AccountSnapshot(ts, mode, equity, cash, upnl, day_pnl, drawdown, breaker_state, base_currency, cash_by_currency, equity_by_currency, fx_to_base)`

Paper and live share identical schemas (only the `mode` tag differs) so paper-vs-live comparison is exact.

---

## 7. Multi-stage roadmap — goals, targets, exit criteria

**⛔ Review Gate at the end of every phase — human must approve before advancing.**

**Status rule:** “build complete” means the feature and tests exist; it does **not** satisfy an operational exit gate. A phase advances only when its runtime evidence and human review are also complete.

### Phase 0–0.96 — Foundation and product build (BUILD COMPLETE; evidence gate still open)
- **Phase 0:** paper broker, deterministic risk/decision/execution, audited human confirmation, scheduler, ledger and reports are built; the original ≥20-paper-day exit criterion is **not yet met**.
- **Phase 0.5–0.75:** research-first Web/Desktop/Telegram, persisted knowledge, regional sessions, fundamentals/earnings/RAG and read-only conversational Finance are built.
- **Phase 0.8–0.9:** fail-closed health/reconciliation, IBKR-shaped broker lifecycle, kill switch and audited multi-account Portfolio/manual-import workflow are built and offline-tested.
- **Phase 0.95:** deterministic discovery, emerging-industry/supply-chain research, independent CN/HK/KR desks, upstream-sync process and restart-safe approval recovery are built; real IBKR Paper and uninterrupted paper-day evidence remain outstanding.
- **Phase 0.96:** human-readable briefs and durable listed-instrument watch groups are built across Web/Desktop; advanced group collaboration and sourced crypto-resilience metrics remain optional follow-ups.

### Phase 0.97 — Operational paper validation + IBKR ordinary-order hardening (NOW)
- **Goal:** prove the built system works unattended on market time and make the ordinary US stock/ETF pending-order lifecycle dependable before granting real-money authority. This phase is deliberately narrow: research → human approval → GTC limit/bracket order → broker state/fill/protection → reconciliation/reporting. No Quant execution or shadow-strategy build belongs in this version.
- **Runtime freeze / first evidence:** keep the currently deployed image unchanged through the next complete US trading day unless the kill switch or a safety-critical failure requires intervention. Capture monitor, research, candidate, approval, 11:30 ET submission, resting-order state, 16:00 close/fill, protection, restart count and next report before deploying another build. A session with an automatic process restart is useful recovery evidence but does not count as the first uninterrupted session.
- **Paper execution acceptance:** capture one uninterrupted US session first, then accumulate ≥20 valid US trading days. Every approved candidate must reach one persisted terminal outcome (`PLACED/FILLED`, broker rejection, explicit skip, or `EXPIRED/missed execution`) with no stranded approval; verify bracket protection, partials, close reporting, restart recovery and ledger↔broker reconciliation.
- **Research model hierarchy:** collection subagents may use the fast/cheap search model for retrieval, de-duplication, extraction, tagging and evidence organization. The final daily brief is an investment judgment and MUST be synthesized by the configured **primary/decision model** (`FINANCE_LLM_MODEL` / resolved primary role), never `FINANCE_LLM_SEARCH_MODEL`. The primary model receives the cited evidence packet, reconciles conflicting signals, writes the market-specific narrative, and records its model/prompt version. If it fails, show a visible missing-brief alert and retry safely; never substitute template prose or silently downgrade to a subagent model.
- **Research stability acceptance:** on five consecutive open-market days, US/HK/CN/KR must each publish its scheduled persisted brief with source/as-of/freshness status, a primary-model readable narrative, and alerting on missing/stale inputs. Briefs must survive image/service restart and remain searchable. Five days is the initial soak gate; longer paper evidence continues through the 20-day run.
- **Ordinary-order scope:** US stocks/ETFs in a CASH account; whole-share GTC limit entries; attached GTC stop-loss + take-profit OCA/brackets; cancel/replace; partial fills; overnight resting orders; reconnect/restart recovery; broker rejection; and broker↔ledger reconciliation. MOC/LOC remain supported but secondary. Options, shorting, margin, complex algos and autonomous discretionary orders are out of scope.
- **IBKR Paper acceptance:** connect the existing adapter to a real IBKR Paper session and prove order-id/permanent-id mapping, `PendingSubmit/PreSubmitted/Submitted/PartiallyFilled/Filled/Cancelled/Inactive/Rejected` normalization, reconnect and resubscription, idempotent submission, cancel/replace, bracket child activation, settled-cash checks and broker-authoritative reconciliation. PaperBroker remains the deterministic test oracle; IBKR Paper measures real broker behavior and differences.
- **Resilience closure:** finish whole-session step idempotency/mid-session recovery, add a secondary-feed path with per-source staleness, and turn scheduled-session or approved-order failures into visible alerts rather than silent gaps.
- **Exit ⛔:** ≥20 paper days logged end-to-end; at least one approved candidate has exercised submit→broker state→fill/protection→report; five-day regional research soak passes; ordinary-order reconnect/cancel/replace/partial-fill cases pass on IBKR Paper; kill-switch drill and guardrail audit pass; human signs off on Phase 1.

### Phase 0.98 — Quant shadow research (NEXT VERSION; DEFERRED)
- Define a versioned deterministic low-frequency strategy, run costed walk-forward OOS across ≥2 regimes, then persist shadow-only daily decisions under `quant:<strategy_version>` with zero broker authority. Compare Agent and Quant decisions on return, drawdown, turnover, rejection reasons and stability. Do not start this work until the Phase-0.97 ordinary-order loop is operationally stable.

### Phase 1 — Validated IBKR Paper → tiny live (AFTER Phase 0.97 sign-off)
- Promote the already validated ordinary-order adapter from IBKR Paper to only a few hundred USD of human-approved live exposure; continue measuring PaperBroker↔IBKR↔live fill/slippage differences. Exit after ≥20 tiny-live trades, stable reconciliation and no guardrail breach.

### Phase 2 — Validated scaling
- Increase capital only after walk-forward OOS plus live records show reproducible positive expectancy after costs across ≥2 regimes, acceptable drawdown and human sign-off.

### Phase 3 — Limited Quant execution (optional)
- A successful Phase-0.98+ shadow strategy may receive a small, explicit whitelist and capital/risk budget. `quant:<strategy_version>` must still use RiskEngine, Ledger, ExecutionEngine and the kill switch; discretionary LLM research never receives direct approval/order authority.

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

### Completed build summary (trimmed)

- [x] **Phase 0:** typed schemas, PaperBroker/DataFeed/Ledger, 100%-covered RiskEngine, monitors, analysis/debate/decision, confirmation, execution, scheduler, reporter and backtest/walk-forward harness.
- [x] **Phase 0.5–0.75:** research-first Web/Desktop/Telegram, regional sessions, persistent source/vector knowledge, fundamentals/earnings/RAG, readable briefs and read-only conversational Finance tools.
- [x] **Phase 0.8–0.9:** fail-closed health/dead-man/reconciliation, retrying feed, offline-tested IBKR lifecycle/kill switch, and audited multi-account Portfolio/manual-import flows.
- [x] **Phase 0.95:** provenance-gated discovery and supply-chain research, independent CN/HK/KR briefs, reviewed upstream sync, and durable approval→execute/expire recovery.
- [x] **Phase 0.96:** durable listed-instrument research groups, canonical multi-market search, Web/Desktop Watch UX and expanded research-only crypto universe.

### Phase 0.97 active backlog — prove and harden ordinary pending orders

- [x] Connect Finance to the private Qdrant service and provide an idempotent authoritative-document backfill with count/search verification; retain the embedded index as rollback until DR validation.
- [x] Add the independent relational prediction ledger: primary-model/Debate/discovery claims persist as continuous append-only series with evidence, revision chains, market-session checkpoints, historical backfill and versioned score records; it remains analysis-only.
- [x] Run the restart-safe deterministic evaluator after each relevant market close: fetch adjusted entity/benchmark paths, fill due checkpoints, calculate return/MFE/MAE/calibration metrics, retry missing data without scoring a miss, and expose aggregate APIs.
- [x] Build the shared Web/Desktop **Prediction Review** workspace under Investment Research with active series, due health, market/horizon evaluation and recent outcomes; keep Quant backtests and real trade P&L separate.
- [ ] Publish sourced weekly/monthly/quarterly prediction reviews and postmortems; add ranking/IC aggregation for discovery cohorts and confidence intervals once enough completed samples exist.
- [ ] Freeze deployment through the next complete US session; capture scheduled research→candidate→approval→submission→broker state→fill/protection→close report evidence and record every process restart.
- [ ] Telegram poll failures are isolated from the Finance process and cutoff execution; add bounded backoff, health state and operator alerting to complete the resilience gate.
- [x] Move US selection/push to 10:30 ET with a 10:30–11:30 human window, and add the fail-closed post-approval fresh-market primary-model review: unchanged orders retain approval; changed terms use a new candidate/card and require a second human confirmation.
- [x] Route twice-daily US/HK/CN/KR final synthesis through the Hermes primary model on a non-blocking Beijing 09:00/21:00 schedule; persist model/prompt/evidence provenance and show visible failure without template/weak-model fallback.
- [x] Rebuild brief quality around publication-time news freshness/deduplication, dated historical RAG, prior-publication deltas, real-holding context and a validated trend action map; stop intraday refreshes from multiplying archived briefs/prediction runs.
- [x] Replace the production A-share fixed-stock/US-VIX research path with cross-industry sector anchors plus a live, industry-capped Eastmoney market universe; dynamically extend breadth, movers, themes, signals and the primary-model evidence packet, distinguish scan outage from “no opportunity”, and render substantive CN↔HK comparison from both independent primary-model briefs.
- [ ] Add a secondary broad A-share universe/feed plus sourced mainland fundamentals, filings, earnings calendar, policy/industry-flow evidence and valuation deltas; until then CN action views remain research guidance rather than sufficient evidence for automatic investment confirmation.
- [ ] Accumulate ≥20 valid US paper trading days; every approved candidate must have a persisted terminal outcome and no approval may remain silently stranded.
- [ ] Run a five-consecutive-open-day US/HK/CN/KR research soak; measure on-time publication, freshness/source gaps, readable briefs, alerts and restart persistence.
- [ ] Make the entire session step-idempotent and crash-resumable, not only confirmation execution; prove reruns cannot double-count or double-place.
- [ ] Add a secondary `DataFeed` implementation/failover path and expose per-source staleness; keep new entries fail-closed when all sources are unhealthy.
- [ ] Complete the ordinary PaperBroker order matrix: GTC entry, bracket/OCA protection, cancel/replace, partial fill, overnight rest, rejection, close processing and restart recovery.
- [ ] Connect IBKR Paper and prove ordinary order-state normalization, permanent identity, reconnect/resubscription, cancel/replace, partial fills, bracket activation, settled cash and broker↔ledger reconciliation.
- [x] Build the offline-tested **multi-currency execution foundation**: currency-tagged schemas/ledger, canonical one-currency-per-symbol validation, USD/HKD PaperBroker cash and reservation sleeves, FX-normalized risk/exposure controls, USD 2,000 + HKD 16,000 paper opening balances, and IBKR SEHK/HKD contract qualification plus order/fill/position reconciliation fields.
- [ ] Finish connected **HK IBKR Paper acceptance**: persist qualified contract details/conId, enforce board-lot/tick rules, Hong Kong calendar/lunch/auction behavior, fee rules and quote entitlement/freshness; then prove GTC/bracket/partial/cancel/restart/reconciliation parity through the independent **10:30–11:30 Asia/Hong_Kong** human window. Later USD↔HKD conversion requires a fresh quoted rate and separate human-confirmed audit action; never convert implicitly. Detailed plan: `docs/finance-hk-ibkr-rollout.md`.
- [ ] Add closed-trade analysis feedback for signal/research evaluation only; it may not alter hard risk limits or authority.
- [ ] Run the end-to-end IBKR Paper dry run, reconciliation and kill-switch drill, then obtain human Phase-1 sign-off.

### Phase 0.98 deferred backlog — Quant shadow only

- [ ] Define and freeze the first low-frequency Quant strategy/version; run costed walk-forward OOS across ≥2 regimes, then persist shadow-only daily decisions under `quant:<strategy_version>` with zero broker authority.
- [ ] Compare Agent and Quant decisions on return, drawdown, turnover, rejection reasons and stability before considering any autonomous whitelist.

### Deferred product refinements (do not block paper validation)

- [ ] Watch groups: owner/profile scoping, reorder, optimistic concurrency, multi-add/undo, symbol URL restoration and compact narrow-layout selector.
- [ ] Crypto: sourced resilience/liquidity metrics and scheduled freshness/news monitoring; trading remains disabled.
- [ ] Portfolio: IBKR Flex import and live broker exposure aggregation after an IBKR Paper connection exists.

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
- **I. Crypto research universe** (observation is allowed; execution remains disabled until venue/custody/permission approval) — liquid/durable core `BTC-USD`, `ETH-USD`; higher-risk major networks `SOL-USD`, `BNB-USD`, `XRP-USD`, `ADA-USD`; peg/counterparty monitors `USDT-USD`, `USDC-USD`. Rank by transparent resilience/liquidity metrics with source/as-of; never present “保值” as a guarantee.

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

- 2026-08-11 — **Daily research quality refactor.** Audited 445 archived snapshots and proved repeated Yahoo headlines were being treated as new for up to 16 research days; current CN digest entries were all older than 72h (oldest >350 days). Added publication-time freshness/deduplication, dated RAG context, richer persistence/volume/drawdown factors, prior-thesis deltas, read-only real-holding context, structured trend stances/invalidation, canonical-edition-only archival, and Web/Desktop/Telegram rendering; deterministic tests and historical replay are recorded in `docs/finance-research-quality-audit-2026-08-11.md`.
- 2026-08-11 — **A-share research breadth refactor.** Runtime inspection proved CN discovery had zero source symbols, US VIX contaminated the A-share regime, movers overlapped inside an eight-name static universe, and CN↔HK “complete” only meant two files existed. Added dynamic cross-industry market seeds, sector anchors, market-native regime/breadth, non-overlapping dynamic movers and primary-model-backed cross-market comparison; a live read-only scan surfaced healthcare and industrial-metals candidates instead of the legacy technology list.
- 2026-08-11 — **Official upstream refresh.** Merged official `main` through `2cdb30a474d7` (6,331 upstream commits), migrated Finance into the contribution-driven Desktop/Web route architecture, preserved its service/tool authority boundary, and recorded validation plus known baselines in `docs/upstream-sync/2026-08-11.md`.
- 2026-07-17 — **Review-failure spam fix + DAY-entry fill-chain lifecycle.** Fixed the "主模型复核失败" Telegram flood: the live primary model (MiniMax-M3) sometimes returns reasoning-only completions with no `content`, so the post-approval review raised and retried every ~3s with no backoff/dedup while fail-closed still blocked all placement. `http_complete` now recovers `reasoning_content`/`reasoning_details`, the review budget rose 800→2000 tokens, and the post-approval review is bounded to 3 attempts at 1/5-min delays with audit-persisted state and first-failure-only alerting (deployed under the §0.97 freeze exception since the bug blocked every order). Then implemented the §5.7 ordinary-order lifecycle: entries are now a **DAY** limit-entry parent with **GTC** protective children — an unfilled entry is cancelled at the close and re-researched next day, and a partial fill keeps GTC protection sized to the filled quantity (close-cancel, child resize, next-day no-replay and §5.6 material-change reconfirmation already existed). Full suite 1323 passed; 4 pre-existing e2e failures are a sim price-drift calibration issue (execution re-validation correctly skips entries that ran >1.5% above ref), not a regression. Open: reconcile §4 line 71 (older GTC/MOC-LOC entry wording) with §5.7; connected HK IBKR Paper acceptance still pending.
- 2026-07-16 — **Balanced prediction review + allocation controls + HK currency foundation.** All-market forecast/recent rows now round-robin US/HK/CN/KR, remove semantic duplicates and use verified regional names; Web/Desktop gained durable operator allocation controls and currency cash visibility; PaperBroker/Ledger/Risk/IBKR now carry isolated USD/HKD execution state with offline SEHK qualification tests, while connected lot/tick/session/fee acceptance remains open.
- 2026-07-16 — **Automatic prediction evaluation + review UI.** Added restart-safe US/HK/CN/KR close evaluation over adjusted entity/benchmark paths, aggregate calibration APIs and Hermes-native Web/Desktop Prediction Review; tiny samples remain visibly descriptive, while Quant backtests and P&L stay separate.
- 2026-07-16 — **Prediction-review placement + HK rollout boundary.** Reserved Finance → Investment Research → Prediction Review for live forecasts/evaluation/backtests (kept separate from P&L), fixed market order to US/HK/CN/KR, and sequenced SEHK/HKD/lot-tick/session-aware IBHK Paper validation before any HK live authority.
- 2026-07-16 — **Semantic retrieval + longitudinal prediction foundation.** Selected OpenAI `text-embedding-3-small` native 1536 in a versioned Qdrant collection with fail-closed migration, and implemented isolated forecast series/revisions/evidence/trading-session checkpoints/outcome scoring plus two-level idempotent historical backfill (96 snapshots → 81 distinct evidence observations, 1,381 changed revisions; replay adds zero); automatic market-close evaluation remains next.
- 2026-07-16 — **Dedicated Finance Qdrant migration and forecast-evaluation design.** Finance now reaches private Qdrant over an internal-only network; 1,636 authoritative documents were idempotently backfilled and count/search verified. Defined a separate relational prediction ledger with versioned claims, horizons, revisions, evidence and deterministic weekly/monthly/quarterly evaluation; implementation remains the next Phase-0.97 item.
- 2026-07-16 — **Twice-daily primary-model briefs and restart audit repair.** Added one Finance-owned 09:00/21:00 Beijing cycle for US/HK/CN/KR, isolated Telegram polling failures, made brief restore deterministic, corrected close-fill timestamps and persisted market marks for broker rehydration; soak/backoff/IBKR evidence remain open.
- 2026-07-16 — **Phase 0.97 narrowed to ordinary orders.** Freeze the deployed runtime through the next complete US session; move final briefs from the search/subagent model to the primary decision model; defer all Quant shadow work to Phase 0.98; prioritize Telegram/process resilience and complete PaperBroker→IBKR Paper GTC/bracket/cancel/partial/reconnect/reconciliation evidence.
- 2026-07-15 — **Operational truth reset / Phase 0.97 opened.** Runtime audit found persisted regional research and restart recovery working, but the active paper ledger still had 0 orders, 0 fills and 0 trades; build completion was explicitly separated from the ≥20-day execution/research evidence gate.
- 2026-07-15 — **Phase 0.96 research workspace.** Added human-readable Researcher's Briefs plus durable Hermes-native listed-instrument watch groups and expanded research-only crypto coverage; advanced collaboration/metrics work is deferred.
- 2026-07-15 — **Phase 0.95 discovery and execution recovery.** Added provenance-gated market/supply-chain discovery, independent CN/HK/KR research, restart-safe approved-order retry, and close-time `EXPIRED / missed execution` audit; real paper execution evidence remains pending.
- 2026-07-14 — **Broker/Portfolio/pre-live backbone.** Added offline-tested IBKR lifecycle, settled cash, kill switch, regime walk-forward/parity harness, audited multi-account Portfolio, KR research and the reviewed upstream merge.
- 2026-07-12–13 — **Foundation through Phase 0.8.** Built the paper loop, risk/confirmation/ledger stack, research-first Web/Desktop/Telegram, persistent knowledge/RAG, regional schedules, K-lines and fail-closed health/reconciliation.
