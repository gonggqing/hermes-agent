# Go-Live Runbook — IBKR Phase 1

**Status:** Phase 0.95 deliverable (Loop.md §Phase-0.95 / §Phase-1).
**Audience:** the operator (公卿). **Precondition for use:** the ⛔ human
sign-off gate (Loop.md Phase 0.95 exit) has been cleared.

This runbook takes the system from *paper* to *tiny-live* on an Interactive
Brokers **Hong Kong CASH** account, and documents the kill-switch drill and the
emergency procedures. It assumes the code already merged: `IBKRBroker`, the
broker factory, the kill-switch, and the live-order triple gate.

> **Golden rule (Loop.md §3):** the system never opens a real-money position
> without ALL of `HUMAN_CONFIRM=true`, `BROKER=ibkr`, `DRY_RUN=false`. The
> RiskEngine and ExecutionEngine enforce this independently; this runbook never
> asks you to weaken it.

---

## 0. One-time prerequisites

1. **IBKR account funded** (HK cash account) and enabled for the products you
   will trade (US stocks/ETFs; HK later).
2. **TWS or IB Gateway installed** and logged in on the host that runs the
   finance service. Enable the API:
   - TWS → *Global Configuration → API → Settings*: ✅ *Enable ActiveX and
     Socket Clients*; set *Socket port*; ✅ *Read-Only API* **OFF** only when
     you are ready to place live orders (keep it ON for the paper dry run's
     account/positions read if you want extra safety).
   - Add `127.0.0.1` to *Trusted IPs*.
   - **Ports:** TWS `7497`=paper / `7496`=live; IB Gateway `4002`=paper /
     `4001`=live.
3. **Install the live-broker extra** (imported lazily; the test suite never
   pulls it in):
   ```
   pip install 'swing-trader[ibkr]'
   ```
4. **Secrets live only in `~/.hermes/.env`** — never commit them, never echo
   their values.

---

## 1. Configuration reference (`~/.hermes/.env`)

| Var | Paper dry-run | Tiny-live | Meaning |
|-----|---------------|-----------|---------|
| `BROKER` | `ibkr` | `ibkr` | select the IBKR adapter |
| `DRY_RUN` | `true` | `false` | live orders need `false` |
| `HUMAN_CONFIRM` | `false` | `true` | live orders need `true` |
| `IBKR_HOST` | `127.0.0.1` | `127.0.0.1` | TWS/Gateway host |
| `IBKR_PORT` | `7497` (paper) | `7496` (live) | **must match the account type** |
| `IBKR_CLIENT_ID` | `1` | `1` | any stable int, unique per API client |

**The paper/live *account* flag is derived from the triple gate, not the port.**
`build_broker` computes `paper = not live_orders_allowed`. Consequences you can
rely on:

- An **un-gated** config (`DRY_RUN=true` or `HUMAN_CONFIRM=false`) can only ever
  connect to a **paper account**. If you also point `IBKR_PORT` at a **live**
  port, the broker **refuses to construct** (`ValueError: … LIVE port`) — it
  fails closed at startup, not at order time.
- A **fully-gated** config connects to a **live account** and tags fills
  `Mode.LIVE`. A paper account always tags `Mode.PAPER`, so testing against
  IBKR paper never pollutes the live-money ledger.

---

## 2. Paper-first dry run (mandatory before live)

Goal: exercise the full §4 daily loop against **IBKR's paper simulator** through
the real API, end to end, with zero real money.

1. `.env`: `BROKER=ibkr`, `DRY_RUN=true`, `HUMAN_CONFIRM=false`,
   `IBKR_PORT=7497`. Start TWS **paper** and log in.
2. Start the service:
   ```
   python -m swing_trader serve
   ```
   Confirm the startup line: `broker: ibkr (live orders blocked)`.
3. **Health check** — `GET /v1/health`. `entries_allowed` should be `true`
   once market+portfolio snapshots are fresh and reconciliation is clean.
4. **Run a manual session** (don't wait for the schedule):
   `POST /v1/session/run` (human surface). Approve a candidate in the queue,
   then `POST /v1/session/finalize`. Verify the order appears in TWS paper.
5. **Reconciliation** — after fills, confirm `GET /v1/health`'s `reconciliation`
   check is OK (broker positions == ledger-derived positions). Any drift →
   `UNHEALTHY` → new entries auto-halt (dead-man's switch).
6. **Let it fill**, then confirm the ledger recorded the fills with
   `mode=paper` and the P&L looks right.

Do **not** proceed to live until at least one clean end-to-end paper cycle
(entry → protective stop attached → exit) has been observed, plus progress
toward the **≥20 paper-day** Phase-0 exit criterion.

---

## 3. Kill-switch drill (practise BEFORE going live)

The kill-switch is a filesystem HALT flag next to the ledger DB. **Engaging it
halts all NEW entries; it does NOT cancel resting protective stops** (Loop.md
§4: never leave a position naked). Three equivalent ways to engage:

| Method | Command | When |
|--------|---------|------|
| CLI | `python -m swing_trader kill --reason "why"` | normal operator halt |
| HTTP | `POST /v1/killswitch/engage {actor,reason}` | from Desktop/Web/agent |
| bare touch | `touch <db_dir>/KILL_SWITCH` | service wedged / emergency |

**Drill steps:**

1. Engage: `python -m swing_trader kill --reason "drill"`.
2. Verify: `GET /v1/health` → `kill_switch` check `UNHEALTHY`,
   `entries_allowed=false`; `GET /v1/killswitch` → `engaged:true`.
3. Run a session — confirm **every new entry is vetoed** by the RiskEngine
   ("system unhealthy — new entries halted"), while **exits still flow**.
4. Release (**human only**): `python -m swing_trader release`, or
   `POST /v1/killswitch/release` from a **human** surface (desktop/web/telegram
   with a human actor). System/LLM/agent actors get `403` — the agent can HALT
   but can never UN-HALT.
5. Verify `entries_allowed=true` returns once released and data is fresh.

**Flatten (deliberate, separate from the kill-switch):** to cancel resting
working orders, `POST /v1/orders/cancel-all` (human only). `include_protection`
= `true` cancels protective stops too (leaves positions naked — only when
you are about to close manually); `false` cancels pending entries and keeps
protective stops resting. The kill-switch does **not** do this automatically.

Properties you can rely on: the flag **survives a restart** (a halt is never
forgotten on a crash), and if its state can't be read it **fails closed**
(treated as engaged).

---

## 4. Go-live cutover (tiny size)

1. **Pre-flight:** paper dry run clean; kill-switch drill done; guardrail audit
   green; upstream sync merged; **human sign-off recorded**.
2. Start TWS **live** (port `7496`) and log in to the funded account.
3. `.env`: flip the triple gate — `DRY_RUN=false`, `HUMAN_CONFIRM=true`,
   `IBKR_PORT=7496`. Keep position sizes at the Phase-1 tiny floor
   (`PER_TRADE_RISK_PCT` small; a handful of shares).
4. Restart the service. **Confirm the startup line:**
   `broker: ibkr (live orders ALLOWED)`. If it says `blocked`, the gate is not
   fully set — stop and fix `.env`.
5. First live session: approve **one** small candidate, finalise, and **watch
   the fill in TWS**. Confirm the ledger tags it `mode=live`.
6. Sit at tiny size for the Phase-1 exit criteria (≥20 tiny-live trades,
   measured slippage/fills, no guardrail breaches).

---

## 5. Emergency procedures

| Symptom | Action |
|---------|--------|
| Something feels wrong, halt now | `python -m swing_trader kill --reason "..."` (or `touch <db_dir>/KILL_SWITCH`). Entries stop; stops stay. |
| Need to pull all working orders | `POST /v1/orders/cancel-all` (human). Decide `include_protection` deliberately. |
| Ledger/broker drift reported | System auto-halts entries. Investigate via `GET /v1/portfolio/accounts/<id>/reconcile` and TWS; do **not** override. |
| TWS/Gateway disconnected | `IBKRBroker` reconnects lazily on the next call; positions/fills re-read from IBKR. If drift persists, halt and reconcile. |
| Drawdown breaker tripped | RiskEngine blocks new entries for the day automatically; no action needed. |
| Total stop | Kill-switch + `cancel-all(include_protection=false)` to keep stops, or close manually in TWS and record via the Portfolio Journal. |

## 6. Rollback to paper

1. Kill-switch engage (halt).
2. `.env`: `DRY_RUN=true` (and/or `HUMAN_CONFIRM=false`), `IBKR_PORT=7497` or
   `BROKER=paper`.
3. Restart; confirm `live orders blocked`.
4. Release the kill-switch once you've verified paper mode.

---

## Appendix — what enforces the golden rule (defence in depth)

1. **Startup:** `build_broker` refuses a live IBKR port under an un-gated config.
2. **Broker:** `IBKRBroker(paper=True)` refuses to construct on a live port;
   a CASH account spends only `SettledCash` (no free-riding on unsettled sale
   proceeds), failing closed locally before an order reaches IBKR.
3. **Execution:** `ExecutionEngine` raises `GuardrailError` if asked to run
   `Mode.LIVE` without `live_orders_allowed` threaded from the triple gate.
4. **Risk:** the pure, 100%-branch-covered `RiskEngine` vetoes every new entry
   when `system_healthy` is false (stale data, ledger drift, **or kill-switch
   engaged**) — it cannot be bypassed.
5. **Human gate:** live candidates are placed only after an authenticated human
   confirmation on an approved surface; LLM/agent actors are refused.
