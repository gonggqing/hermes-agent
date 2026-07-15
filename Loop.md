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
- **Goal:** prove the whole system is safe and reproducible before any real money, while expanding research beyond a fixed watchlist without granting the discovery process any trading authority.
- **Build — pre-live gate:** complete the reviewed weekend **upstream Hermes sync merge** (Phase-1 prerequisite, §8); a full end-to-end dry run on IBKR **paper** (once TWS is up) mirroring the §4 daily loop; expand the backtest to walk-forward across ≥2 regimes; a documented **go-live runbook + kill-switch drill**; confirm progress toward the **≥20 paper-day** Phase-0 exit criterion.
- **Build — dynamic market discovery:** add a separate, replayable market-scanning layer for US/HK/CN that can propose NEW research symbols instead of relying only on §11's static monitored universe. Its broad screen combines liquidity and unusual volume, sector/industry relative strength, price/earnings trend, news/theme heat, filings and earnings/capex events, and ETF constituent additions/removals. Results enter a temporary **discovery pool**, never the order queue.
- **Build — emerging-industry supply-chain discovery:** start from evidence-backed secular themes (for example robotics/embodied AI, AI infrastructure, power/cooling, semiconductors or biotechnology), then expand upstream/downstream relationships to locate less-obvious beneficiaries with defensible technology/process barriers and improving business/price trends. A robotics search may discover precision reducers, bearings, joints/shafts, actuators, sensors, controllers or specialist materials, but no component list or example is hard-coded as the answer. Every company↔theme↔supply-chain edge must retain source, date and confidence; an LLM-only ticker/name guess is invalid until the instrument resolver and independent evidence confirm it.
- **Discovery funnel and safety boundary:** broad quantitative screen → theme/supply-chain expansion → source and instrument validation → moat/trend/liquidity scoring → ranked research pool → normal Agent research. Record why a symbol entered, changed rank or was removed; deduplicate cross-market listings; reject stale, unresolved or illiquid candidates. Discovery is research-only and cannot call `approve_candidate`/`place_order`, create broker orders, or bypass the existing Decision → Risk → human-confirmation path.
- **Build — independent CN and HK research:** replace the current shared China/Hong-Kong research session with separate `market_id`, exchange calendar/timezone, index/regime model, universe/scanner inputs, news/theme set, freshness state, schedule, persisted brief history, UI desk and Telegram summary for **CN** and **HK**. Cross-border themes, Stock Connect flows and shared supply chains belong in an explicit China↔HK synthesis section built from the two completed briefs; they must not make one market silently inherit the other's regime or freshness.
- **Exit ⛔ Review Gate:** upstream sync merged + reported; IBKR-paper dry run clean; a guardrail audit passes end-to-end; runbook + kill switch verified; **human sign-off** to enter Phase 1. In addition, identical scanner inputs reproduce the same ranked discovery set with provenance, no unresolved symbol reaches research, no discovered symbol reaches execution without all normal gates, and CN/HK briefs can run/fail/recover independently without cross-market state contamination.

### Phase 0.96 — Personal research watchlists & broader crypto coverage — human decision 2026-07-15
- **Goal:** turn Research → Watch into a durable, operator-curated research workspace. The user can organize instruments into named groups across markets, while the system expands crypto observation without implying that any token preserves value or granting the watchlist trading authority.
- **Hermes-native page design:** keep the existing Research master-detail shell, monochrome tokens, typography, cards, buttons, Lucide icon language, loading/error/empty states and translations. In the left **Watch** section, show existing system modules first, then user groups, and finish with **`新自选组`** using the same dimensions and solid separators as every other Watch row. Selecting a group opens its detail canvas; do not add a new top-level Finance tab or a second visual system.
- **Group interaction:** a new group starts with the editable name `新自选组` (use a unique numeric suffix only when needed). The user may rename, reorder and delete groups. A group header shows its name, instrument count and one search/add control. If the name remains untouched, the UI may suggest a concise name from the resolved members, but must never overwrite a custom name. Deletion requires confirmation; removing a member offers undo and does not alter holdings, Ledger history or system watchlists.
- **Instrument picker:** search by partial ticker or listed stock/ETF name and resolve to a canonical exchange instrument before adding. OTC/open-end funds and crypto stay outside personal K-line groups because they do not share the exchange OHLC contract; legacy OTC members remain removable but are excluded from charts and new recommendations. Each result shows display name, canonical symbol, market/exchange, currency and security type; ambiguous symbols require a choice rather than suffix guessing. Support listed US, HK, mainland CN and KR instruments in the same group. Added members receive their verified display name automatically; duplicates are blocked within one group but allowed across different groups. The picker has **Search** and **Held positions** sources, with the latter recommending listed instruments from the authoritative real-portfolio holdings by default (paper holdings remain explicitly separate). Multi-select add is supported.
- **Group detail/navigation:** when a group has multiple instruments, use the same compact dropdown/selector pattern as existing Watch modules to switch the active symbol; persist `group` and `symbol` in the URL so refresh/back navigation restores context. Desktop/Web use the same API and interaction contract. Desktop keeps the existing left master list; narrow Web/Desktop layouts collapse groups into a selector and open create/edit flows in a sheet, with ≥44px targets, keyboard navigation, visible focus, no horizontal page scroll and no color-only status.
- **Persistence and monitoring contract:** store user groups and members in the Finance service's durable SQLite state (`WatchlistGroup` + `WatchlistMember`, versioned and owner/profile scoped), never browser-local storage. Authenticated versioned CRUD APIs serve both Web and Desktop. Custom members join the lightweight quote/news/freshness observation layer and on-demand analysis, but do **not** silently enlarge the order universe, change risk roles or enter the candidate queue; deeper scheduled analysis must be explicit and budgeted. All names/symbols retain resolver source and as-of metadata, and data survives image rebuilds.
- **Crypto research coverage:** separate research visibility from broker permission. The initial liquid/durable core is `BTC-USD` and `ETH-USD`; the higher-risk major-network set is `SOL-USD`, `BNB-USD`, `XRP-USD` and `ADA-USD`; `USDT-USD` and `USDC-USD` are peg/counterparty-risk monitors, not return assets. A deterministic **resilience** view may compare market-cap rank, 24h liquidity, asset age, 30/90/365-day return, realized volatility, max drawdown, distance from ATH and stablecoin peg deviation, always with source/as-of and methodology. Never label the score or any token as guaranteed “保值”. Trading remains disabled until a supported broker/venue, custody and permission path is independently approved.
- **Exit / acceptance:** users can create/rename/reorder/delete groups; add/remove/reorder resolved multi-market members; multi-add from search or real holdings; recover the same state after service/image restart; deep-link to the selected group/symbol; and see clear stale/error/ambiguous states. Web/Desktop behavior and translations match, CRUD and resolver contracts are tested, crypto metrics are provenance/freshness tested, and no custom-watchlist or crypto path can approve or place an order.

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

### Phase 0.5 backlog — research-first product surface (completed)

- [x] PaperBroker restores ledger-backed cash, positions, open orders and protective stops after Finance-service restarts.
- [x] `Investment Research` briefs expose dated regime, freshness/risk warnings, themes, movers, events, synthesis, uncertainty and provenance.
- [x] Desktop opens Finance on `Investment Research`, with Queue as a secondary badged action tab.
- [x] Web leads with the research/risk brief and expands its compact action queue only when attention is required.
- [x] Daily research, news and earnings feed a source-linked facts/document/vector knowledge pipeline searchable from Desktop and Web.
- [x] The dedicated Finance Telegram bot provides allowlist-gated interactive approvals while the general Hermes bot remains the gateway.
- [x] The official `upstream` remote, reviewed sync reports and weekly integration-branch workflow are established.

### Phase 0.5+ backlog — regional research sessions and dual-bot roles (completed)

- [x] Reporter and gatekeeper transports are separated: the reporter sends summaries while only the Finance bot handles interactive cards.
- [x] `SessionSchedule` supports independent US, mainland CN, HK and KR calendars without changing US behavior.
- [x] CN/HK/KR `ResearchSession` instances run isolated research-only monitors and analysis with no broker or approval authority.
- [x] Regional research serves independent persisted briefs and bilingual Telegram summaries without cross-market state inheritance.
- [x] Search/summary inference is independently pinned to a low-cost high-speed model.
- [x] Finance Web/Desktop strings are translated and the research view supports US and China/HK market selection.

### Phase 0.75 backlog — deepen the analysis brain

> §3 invariant for every item: analysis quality only; the LLM never approves, RiskEngine remains authoritative, human approval is unchanged, dependencies are mockable and tests do not use the network.

- [x] `YFinanceFundamentals` supplies cached, fail-soft valuation, growth and margin inputs to scheduled and on-demand analysis.
- [x] `YFinanceEarnings` populates brief events and blocks fresh entries within five days of an imminent earnings print.
- [x] RAG-grounded LLM analysis cites retrieved research while deterministic geometry, RiskEngine and human approval retain authority.
- [ ] Analysis feedback loop: closed-trade outcomes adjust analysis/signal quality (never risk limits/caps/authority).
- [x] Fundamentals and earnings documents are provenance-required, content-deduplicated and fail-closed into the research knowledge store.

**Thrust B — conversational finance agent (read/analysis only; no order/approve tools):**

- [x] `/v1/quote`, `/v1/bars` and `/v1/analyze` provide delay-labelled on-demand market and multi-factor analysis.
- [x] The fixed Finance toolset exposes quote, K-line, analysis, brief, research search and account reads, with no place/approve capability.
- [x] The Finance K-line skill renders US/HK candlestick images without putting raw bar arrays into the conversation transcript.

### Phase 0.8 backlog — resilience and observability

> §3 invariant for every item: no new authority, fail closed, never gate exits/protection, preserve human approval and keep dependencies mockable.

- [x] RiskEngine's dead-man switch vetoes unhealthy new entries while exits and protection continue to flow.
- [x] The health model reports feed/portfolio freshness, reconciliation and breaker state with explicit entry eligibility.
- [x] Ledger↔broker reconciliation compares derived and broker positions and fails closed on drift or errors.
- [x] `RetryingFeed` adds bounded exponential retry for transient feed failures while preserving final fail-closed behavior.
- [x] Health is wired through decision, edited-candidate revalidation, reporter alerts and `/v1/health` with consistent simulation timestamps.
- [ ] Loop-step idempotency + mid-session crash recovery: rerunning a session step must not double-place or double-count.
- [ ] Backup-feed stub behind `DataFeed`, with secondary-source failover and per-source staleness on the health surface.

### Phase 0.9 backlog — broker backbone and Portfolio foundation

> Portfolio facts retain provenance, idempotency and fail-closed conflict handling; LLM memory is not a holdings database and manual records are not broker fills.

- [x] The append-only multi-account Portfolio Journal derives cash/holdings from audited events and preserves unknown cost as unknown.
- [x] Native Web/Desktop Portfolio surfaces support accounts, holdings, activity, reconciliation, drafts, CSV import and compensating corrections.
- [x] Cached US/HK/CN type-ahead resolves partial symbols/names, while ambiguous Finance-bot instruments remain blocked pending human confirmation.
- [x] CSV bootstrap supports preview, validation, deduplication and explicit commit without contaminating system trades; IBKR Flex remains deferred.
- [x] Conversational holdings updates create versioned drafts that only authenticated humans can confirm.
- [x] Reconciliation preserves IBKR authority for connected US/HK accounts and human-confirmed authority for mainland holdings without silent merging.
- [x] Source-tagged aggregate holdings feed read-side analysis while execution remains broker-account scoped and live exposure wiring stays deferred to IBKR.

### Phase 0.95 backlog — dynamic discovery and pre-live validation

- [x] A deterministic US/HK/CN discovery funnel validates instruments, dated source evidence, freshness, trend and liquidity before ranking a research-only pool.
- [x] Emerging-industry and supply-chain edges retain company↔theme↔component provenance; malformed, stale, unresolved and LLM-only seeds fail closed.
- [x] Discovered symbols enter normal research and can reach execution only through Decision → Risk → human confirmation; discovery itself has no order authority.
- [x] Mainland CN and HK now use disjoint universes, indices, calendars, snapshots, schedules, archives, API routes, Web/Desktop desks and Telegram briefs.
- [x] The explicit CN↔HK synthesis preserves both markets' independent regime/freshness and joins only shared themes with evidence.
- [x] Web/Desktop retain Hermes components, responsive layout and translated discovery/synthesis copy; the legacy suffix partition was removed.
- [x] Confirmation recovery is durable: pre-cutoff restarts restore cards/versions, post-cutoff restarts re-quote/re-risk and submit idempotently, a 12:00 ET watchdog warns/retries, and unplaced approvals expire with `missed execution` audit at the close.
- [ ] Exit evidence remains external: complete the uninterrupted US paper day, real IBKR Paper dry run and human go-live sign-off before Phase 1.

### Phase 0.96 backlog — personal research watchlists and crypto observation

- [x] Added rebuild-safe SQLite research groups/members and versioned CRUD while keeping them completely separate from the trading universe.
- [x] Extended canonical search to US/HK/CN/KR/crypto, added verified names and real-holdings recommendations, and blocked duplicates within a group.
- [x] Built the Hermes-native Web/Desktop flow — semantic system icons, uniform custom-group icon, `新自选组`, rename/delete/add/remove, group deep link, existing Watch chart/analysis reuse and maintained translations.
- [x] Expanded crypto Research to BTC/ETH, SOL/BNB/XRP/ADA and USDT/USDC peg monitors; all remain disabled for automatic trading.
- [ ] Complete advanced group semantics: owner/profile scoping, reorder, optimistic concurrency, multi-add, member undo, symbol-level URL restoration and compact narrow-layout selector.
- [ ] Add sourced crypto resilience metrics plus scheduled quote/news/freshness monitoring; verify provenance, accessibility and no-order-authority E2E.

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

- 2026-07-15 — **Phase 0.96 foundation built.** Added durable personal research groups, multi-market/crypto resolution, holdings recommendations and matching Web/Desktop Watch workspaces with semantic asset icons; advanced ordering/profile/metrics work remains tracked.
- 2026-07-15 — **Phase 0.96 specified.** Designed persistent multi-market personal Research groups with a `新自选组` entry, canonical symbol search/held-position recommendations, shared Web/Desktop UX and research-only expanded crypto observation.
- 2026-07-15 — **Durable approval execution.** Added restart-safe confirmation restoration, candidate-correlated idempotent orders, pre-cutoff warning, intraday retry and close-time `EXPIRED / missed execution` audit without replaying stale approvals.
- 2026-07-15 — **Phase 0.95 local build.** Added provenance-gated dynamic discovery, independent CN/HK sessions and explicit cross-market synthesis across API, Web, Desktop and Telegram; external paper-day/IBKR/human exit evidence remains pending.
- 2026-07-14 — **CI and analysis-date integrity.** Restored cross-platform lockfile dependencies, cleared repository lint failures and surfaced each signal's source-bar timestamp across API, brief, Web and Desktop.
- 2026-07-14 — **KR research + upstream sync.** Added the Korean semiconductor desk and manual research refresh, structured mover regions, and completed the reviewed `upstream/main` merge documented in `docs/upstream-sync/2026-07-14.md`.
- 2026-07-14 — **IBKR backbone + P0.95 safety gate.** Built the offline-tested IBKR lifecycle, settled-cash enforcement, broker factory, persistent kill switch, go-live runbook, regime walk-forward analysis and paper/live parity harness; real IBKR-paper validation and human sign-off remain required.
- 2026-07-13 — **P0.9 Portfolio foundation.** Added an append-only multi-account journal, human-confirmed drafts, US/HK/CN instrument resolution, CSV bootstrap, reconciliation, aggregate reads and native Web/Desktop holdings surfaces.
- 2026-07-13 — **P0.8 resilience.** Wired fail-closed health, dead-man switching, broker reconciliation and retrying feeds through decisions, alerts and `/v1/health`.
- 2026-07-13 — **TradingView-style K-line charts.** Web/Desktop gained multi-timeframe klinecharts with moving averages, indicators, derived domestic gold pricing and incremental history loading.
- 2026-07-13 — **Finance UX consolidation.** Research became the primary Hermes-native master-detail view with shared market/watch modules, cached live candles and one paper/live switcher.
- 2026-07-13 — **Model routing + research ingestion.** Daily/subagent MiniMax routing and provenance-required fundamentals/earnings ingestion were added with fail-closed deduplication.
- 2026-07-13 — **P0.75 analysis brain.** Added real fundamentals, earnings avoidance, RAG-grounded analysis, on-demand Finance APIs, a read-only Finance toolset and the K-line chat skill.
- 2026-07-13 — **Two sessions + dual bots.** Separated the US trading and CN research schedules, plus outbound reporter and interactive gatekeeper responsibilities.
- 2026-07-13 — **P0.5 research-first resilience.** Added restart rehydration, source-linked briefs/search, Finance-first Web/Desktop layouts, authenticated Telegram integration and an upstream-sync dry run.
- 2026-07-12/13 — **Standing contracts.** Finance reuses the Hermes shell/design/i18n, defaults to Investment Research, keeps Queue secondary and requires reviewed weekly upstream syncs.
- 2026-07-12 — **Phase 0 complete and approved.** The timezone-aware paper loop, deterministic risk/decision/execution core, audited human confirmation, ledger, monitoring, simulation, Finance surfaces and knowledge storage were deployed.
- 2026-07-12 — **Loop.md v0.** Established the watchlist, risk parameters, non-negotiable authority guardrails and long-horizon value-migration framework.
