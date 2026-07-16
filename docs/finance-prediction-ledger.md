# Finance prediction ledger and evaluation design

## Decision

Use a relational prediction ledger as the authoritative evaluation store.
Start with a dedicated SQLite WAL database (`prediction_evaluation.db`) beside
the existing Finance databases and keep its schema PostgreSQL-compatible.
Qdrant stores source documents for retrieval; it does not store forecast truth,
prices, labels, revisions, or scores. A graph database is not justified yet:
many-to-many evidence and supersession relationships fit normalized join
tables, and can later be projected into a graph if multi-hop research queries
become a measured requirement.

The prose brief and the structured forecast record are two outputs of the same
primary-model run. Never parse the published prose later to reconstruct a
prediction. Morning, evening and intraday research editions are observation
times, not separate truth silos: they append revisions to the same continuous
symbol/market/theme series.

## Core records

1. `forecast_runs` — one immutable research edition: market, as-of time,
   provider/model, prompt version, evidence snapshot/hash and brief ID.
2. `forecast_series` — the continuity key: market + entity + claim type +
   producer. Primary brief, Debate, technical signal and discovery producers
   remain separate comparable series instead of being averaged together.
3. `forecast_revisions` — the measurable claim and append-only `supersedes`
   chain: direction, confidence, baseline, benchmark, thesis, invalidation,
   expected condition and producer version. Morning Long and evening Short are
   separate revisions; neither overwrites history.
4. `forecast_checkpoints` — 1/3/5/10/20/60-session evaluation dates calculated
   from the relevant US/CN/HK/KR market calendar, plus pending/evaluated state.
5. `forecast_evidence_links` — claim-to-source/debate/signal/discovery links,
   including source ID, stance, role, weight and producer version.
6. `forecast_outcomes` — deterministic adjusted market observations at
   checkpoints: return, benchmark return, excess return, MFE, MAE, volatility,
   drawdown and event realization.
7. `forecast_evaluations` — versioned scoring result per claim/checkpoint:
   directional hit, calibration score, ranking score, risk/path score and final
   state (`active`, `confirmed`, `refuted`, `expired`, `invalidated`).
8. `review_annotations` — planned sourced human/LLM postmortems with a controlled
   failure taxonomy. These explain outcomes but never determine correctness.
9. `model_prompt_registry` and `experiments` — planned immutable prompt/model versions,
   champion/challenger assignment and promotion evidence.

The implemented ledger lives in `prediction_evaluation.db`, isolated from the
trading ledger and Qdrant. Brief persistence writes prose and structured
forecasts independently, startup idempotently backfills historical briefs, and
read-only API routes expose series history and due checkpoints. The remaining
operational step is the market-close worker that fetches adjusted prices and
calls the versioned evaluator automatically.

Run-level and revision-level idempotency are separate. `forecast_runs.run_key`
prevents the same archived evidence packet from being imported again on every
restart. A per-series content fingerprint additionally suppresses an unchanged
claim when some unrelated part of the broader packet changes. On the initial
96-snapshot backfill this preserved 81 distinct evidence observations while
reducing forecast revisions from 6,308 to 1,381; replaying all 96 snapshots a
second time creates zero runs and zero revisions. These 81 observations must
not be described as 81 primary-model reports: only eight historical snapshots
were scheduled `morning` primary briefs; the rest are legacy or intermediate
research-state observations retained for provenance.

Keep predictions separate from actions: a Long view can produce no order, and
an approved order can fail execution. Candidate/order/fill IDs may link to a
claim, but execution quality is scored independently.

## Horizon policy

Use trading sessions, not elapsed calendar days, and allow multiple checkpoints
without pretending they are independent samples:

| Claim type | Checkpoints | Normal expiry |
|---|---:|---:|
| Daily market regime | close + 1/3/5 sessions | 5 sessions or invalidation |
| Swing symbol Long/Short | 1/3/5/10/20 sessions | declared horizon, max 20 |
| Discovery-pool ranking | 5/20/60 sessions | 60 sessions |
| Theme/supply-chain thesis | 5/20/60 sessions | 60 sessions or thesis break |
| Earnings/event claim | event + 1/5 sessions | 5 sessions after event |

Every claim must declare its horizon at creation. Later evidence creates a
revision; it must not retroactively shorten an old claim to improve its score.

## Scoring and review

- Direction: absolute and benchmark-relative return signs.
- Probability quality: Brier/log loss plus reliability/calibration bins.
- Path/risk: MFE, MAE, drawdown and whether declared invalidation/targets fired.
- Discovery scores: Spearman information coefficient, top-bucket lift and
  drawdown, not a binary win rate.
- Regime calls: score against future index trend, breadth and volatility rules
  frozen in the evaluator version.

Run the evaluator after each relevant market close and refresh all active
checkpoints nightly. Publish descriptive weekly reviews. Recalibrate monthly
only after at least 20 comparable completed claims; consider prompt/model
promotion quarterly only after roughly 50–100 comparable claims, walk-forward
holdout evidence and confidence intervals. Overlapping horizons are correlated,
so use block bootstrap or an equivalent time-series method rather than treating
every checkpoint as an independent win.

Failure reasons use a controlled taxonomy: stale/missing data, unsupported
inference, regime shift, catalyst failure, valuation/timing, technical false
breakout, benchmark/correlation, liquidity/execution, and model disagreement.

## Safe optimization loop

No model or prompt edits itself online. A challenger is created as a new
version, replayed against frozen historical evidence snapshots, evaluated
out-of-sample, then run in shadow beside the champion. Promotion requires the
sample gates above and human approval. Hard risk limits and order authority are
never optimized by this feedback loop.
