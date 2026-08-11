# Finance daily-research quality audit — 2026-08-11

## Observed failure

Read-only inspection of `trader/briefs.db` found 445 snapshots across 61
market-days (roughly two weeks of sessions), because monitor/research/decision
refreshes were archived in addition to the promised morning/evening editions.
The same Yahoo stories were repeatedly presented as new information:

- “Cambricon tops Moutai…” appeared on every one of 16 CN research days.
- “Nvidia Faces China Shift… budgets 46%” appeared on all 16 CN days.
- “CXMT stock soars 466%…” appeared on 16 days after its 2026-07-27 event.

Joining the latest CN digest to authoritative document event timestamps showed
that all ten displayed articles were older than the new 72-hour catalyst
boundary (about 82–8,418 hours old). The report writer also received no prior
brief, no deterministic change set, no real CN portfolio holdings, and only a
thin RSI/SMA feature set. It therefore had little basis for doing more than
rephrasing technical readings and recycled headlines.

## Corrective architecture

1. **Current evidence:** use each article's publication timestamp, exclude
   articles older than 72 hours or materially future-dated, deduplicate by
   canonical URL or normalized headline, and recompute sentiment only from the
   retained set. Publisher quality affects ranking, not truth.
2. **Historical context:** vector-retrieved snippets always carry their date.
   Older material remains searchable foundation knowledge but cannot silently
   become a current catalyst.
3. **Thesis continuity:** the primary model receives six prior distinct
   publications and precomputed changes in regime, breadth, themes, signals and
   news recurrence. Repeated stories are explicitly marked non-novel.
4. **Decision usefulness:** current live holdings are projected read-only into
   each market brief. The primary model emits a validated action stance,
   intended session horizon, confidence, what changed, multifactor rationale,
   evidence references and observable invalidation. Technical alignment alone
   cannot produce a buy-on-confirmation stance.
5. **Signal depth:** technical evidence now includes 5/20-session return,
   20-session drawdown and realized volatility, volume ratio and recent up-day
   persistence in addition to RSI/SMA levels.
6. **Clean history:** intraday refreshes remain available to the runtime but do
   not create brief or forecast records. Only the canonical 09:00/21:00
   primary-model publication persists; exact edition/evidence retries are
   idempotent, and history loading skips legacy duplicate evidence.

## Validation and remaining evidence

Deterministic tests cover the 72-hour boundary, future timestamps, URL/title
deduplication, fresh-only sentiment, publication age/source metadata, prior-news
recurrence, signal deltas, archive idempotence/distinct history, real-holding
projection and intraday non-archival. Web and Desktop typechecks and targeted
lint pass.

The next five-open-day research soak must verify that each market publishes at
09:00/21:00 Beijing time, repeated-headline recurrence is zero in the current
catalyst section, action views prioritize actual holdings/material thesis
changes, and every stance contains a usable invalidation. This is quality
evidence, not permission to loosen execution guardrails.
