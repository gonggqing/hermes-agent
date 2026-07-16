# Hong Kong equities through IBHK

## Scope and sequencing

Hong Kong stocks and listed ETFs join the existing ordinary-order system only
after the US IBKR Paper lifecycle is stable. The sequence is: offline contract
tests → connected IBKR Paper qualification/data → Paper order lifecycle →
reconnect/reconciliation soak → human review → separately enabled tiny live.
Mainland A shares and derivatives are not part of this rollout.

The initial Paper capital is two explicit currency buckets in the same IBHK
environment: **USD 2,000 for US orders and HKD 16,000 for Hong Kong orders**.
Neither balance is converted or netted into the other for risk or reservations.

## Implemented offline foundation (2026-07-16)

The domain schemas and ledger now carry execution currency and per-currency
account maps; canonical suffixes prevent one symbol from accumulating mixed
currency lots. PaperBroker reserves and settles USD/HKD independently and
reports base-currency equity through explicit FX marks. The IBKR adapter maps
`.HK` orders to qualified `SEHK/HKD` stock contracts and preserves currency on
orders, fills and positions. Operator allocation controls are durable and are
enforced by deterministic risk checks. These are offline-tested capabilities,
not evidence that a connected IBKR Paper session has passed acceptance.

## Required broker work

- Extend the implemented `SEHK/HKD` qualification path to persist `conId`,
  local symbol, trading class, primary exchange and minimum tick from connected
  IBKR contract details.
- Keep broker account identity separate from market and currency. `IBHK Paper`
  can hold both US and HK positions, but cash reservations, settled cash,
  exposure, P&L and reconciliation remain currency-aware.
- Add a later, explicit FX conversion workflow. It may propose USD↔HKD from a
  timestamped real-time quote, but must show source, as-of time, rate,
  spread/fees, source amount and expected destination amount on a confirmation
  card. Only a human-confirmed conversion can reach the broker; a stock order
  may never trigger a hidden conversion.
- Enforce board lots and exchange price increments before approval and again
  before submission. Do not silently round quantity or price after human
  approval; show any normalized order on the confirmation card.
- Use the Hong Kong trading calendar and `Asia/Hong_Kong` sessions, including
  the lunch break, auctions, holidays and half-days. `outsideRth` and GTC/DAY
  behaviour must be explicit and tested rather than inherited from US rules.
- Model commission, stamp duty, trading fee, transaction levy and platform
  charges as versioned fee rules. Paper estimates and broker-reported fills are
  stored separately so reconciliation can explain differences.

## Data and authority

IBKR contract details and broker state are authoritative for order identity,
lot/tick rules, positions and fills. A quote must declare live/delayed status and
as-of time; stale or missing HK data blocks new entries. FX conversion is an
explicit approved action—USD buying power must never be treated as HKD settled
cash implicitly. A stale or missing FX quote blocks conversion without blocking
unrelated sell or protective orders.

The same human confirmation, RiskEngine, idempotent execution, kill switch and
append-only audit trail apply. Paper and live use different account/mode gates;
enabling HK Paper must not grant HK live authority.

## Acceptance

Prove contract ambiguity rejection, lot/tick validation, insufficient HKD,
GTC limit + bracket/OCA, partial fill, cancel/replace, overnight rest, lunch
break, broker rejection, restart/adoption and broker↔ledger reconciliation on
IBKR Paper. Then run at least five clean HK sessions before requesting tiny-live
approval. Market presentation and all-market brief processing use the fixed
order: **United States → Hong Kong → China → Korea Semiconductor**.
