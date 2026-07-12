# swing-trader (Phase 0)

Human-in-the-loop, daily-cadence swing-trading assistant for one retail user
(IBKR HK cash account, small capital). **Research/learning tool, not an income
engine.** Single source of truth: [`../Loop.md`](../Loop.md).

## Status

Phase 0 — paper loop only. `PaperBroker` + free data + Telegram confirmation.
No IBKR connection exists yet (stub only). `DRY_RUN=true` by default; live
orders are structurally impossible unless `HUMAN_CONFIRM=true` **and**
`BROKER != paper` **and** `DRY_RUN=false`.

## Layout

- `swing_trader/` — the package (config, logging, schemas, broker, risk, …)
- `tests/` — pytest suite; external deps are always mocked (no network in tests)
- `.env.example` — documented env vars; copy to `.env`, never commit `.env`

This directory is deliberately **self-contained** (own `pyproject.toml`, no
imports of Hermes internals). Per Loop.md §8 it will be extracted into its own
repository before Phase 1; the Hermes runtime plugs in via a thin adapter only.

## Run tests

```bash
cd trader
uv sync --extra dev
uv run pytest
```

The Risk Engine additionally enforces 100% branch coverage:

```bash
uv run pytest tests/test_risk_engine.py --cov=swing_trader.risk --cov-branch --cov-fail-under=100
```

## Guardrails (Loop.md §3 — never violate)

- Risk Engine is pure code, deterministic, authoritative; the LLM cannot touch it.
- Per-trade risk ≤ 1.6% equity (hard cap); daily drawdown breaker −4%.
- No autonomous order placement without human confirmation (Phases 0–2).
- Secrets only in env; never in code, logs, or the ledger.
