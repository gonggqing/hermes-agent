"""Rehydrate a fresh PaperBroker from the Ledger (Loop.md Phase 0.5).

The PaperBroker keeps state in memory, so a Finance-service restart used to
reset the paper account while the Ledger kept the history. This module
replays the authoritative Ledger records into a fresh broker:

- cash: starting cash minus/plus every recorded fill (commissions included);
- positions: chronological replay of fills with weighted average entry
  (identical arithmetic to PaperBroker._apply_fill);
- resting orders: every still-active order — including bracket parents and
  their protective-stop/take-profit children — reinstated with cash
  reservations and OCA wiring rebuilt. Protection comes back up (§4).

The ExecutionEngine must then be seeded with the ledger's fill ids so
sync_fills() does not re-record history as new fills.

Caveat (documented): ``starting_cash`` must match the value the ledger's
history began with; a mismatch is detected against the latest snapshot and
reported as a warning, not silently accepted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import Mode, Position, Side

logger = get_logger(__name__)

__all__ = ["RehydrationReport", "rehydrate_from_ledger"]

_SNAPSHOT_CASH_TOLERANCE = 0.01


@dataclass
class RehydrationReport:
    performed: bool
    cash: float
    n_positions: int
    n_open_orders: int
    n_fills_seeded: int
    warnings: list[str] = field(default_factory=list)
    fill_ids: set[str] = field(default_factory=set)

    def summary(self) -> str:
        if not self.performed:
            return "rehydration: ledger empty — fresh paper account"
        text = (
            f"rehydrated from ledger: cash {self.cash:.2f}, "
            f"{self.n_positions} position(s), {self.n_open_orders} resting "
            f"order(s), {self.n_fills_seeded} fill(s) seeded"
        )
        if self.warnings:
            text += f"; {len(self.warnings)} warning(s): " + "; ".join(self.warnings)
        return text


def rehydrate_from_ledger(
    broker: PaperBroker,
    ledger: Ledger,
    mode: Mode = Mode.PAPER,
) -> RehydrationReport:
    """Replay ledger history into a FRESH broker. No-op on an empty ledger."""
    fills = sorted(ledger.get_fills(mode), key=lambda f: f.ts)
    open_orders = ledger.get_orders(mode=mode, active_only=True)
    if not fills and not open_orders:
        return RehydrationReport(
            performed=False,
            cash=broker.starting_cash,
            n_positions=0, n_open_orders=0, n_fills_seeded=0,
        )

    warnings: list[str] = []
    cash_by_currency = dict(broker.starting_cash_by_currency)
    book: dict[tuple[str, str], tuple[float, float]] = {}  # (currency, symbol) -> lot

    for f in fills:
        key = (f.currency, f.symbol)
        qty, avg = book.get(key, (0.0, 0.0))
        if f.side is Side.BUY:
            cash_by_currency[f.currency] = (
                cash_by_currency.get(f.currency, 0.0) - f.qty * f.px - f.commission
            )
            new_qty = qty + f.qty
            avg = (qty * avg + f.qty * f.px) / new_qty if new_qty > 0 else 0.0
            book[key] = (new_qty, avg)
        else:
            cash_by_currency[f.currency] = (
                cash_by_currency.get(f.currency, 0.0) + f.qty * f.px - f.commission
            )
            new_qty = qty - f.qty
            if new_qty < -1e-9:
                warnings.append(
                    f"ledger inconsistency: SELL fills exceed BUYs in {f.symbol} "
                    f"(clamped to flat)"
                )
                new_qty = 0.0
            book[key] = (new_qty, avg)

    marks = ledger.get_latest_market_marks(mode)
    positions = [
        Position(
            symbol=sym,
            currency=currency,
            qty=qty,
            avg_px=avg,
            mkt_px=marks.get(sym),
        )
        for (currency, sym), (qty, avg) in book.items()
        if qty > 1e-9
    ]

    # Cross-check replayed cash against the latest recorded snapshot.
    snapshots = ledger.get_snapshots(mode)
    if snapshots:
        last = snapshots[-1]
        # Backwards-compatible recovery for ledgers created before per-symbol
        # market marks existed. Aggregate equity can be decomposed safely only
        # when exactly one position is open.
        if (
            len(positions) == 1
            and positions[0].mkt_px is None
            and positions[0].qty > 0
            and positions[0].currency == broker.base_currency
        ):
            inferred = (last.equity - last.cash) / positions[0].qty
            if inferred >= 0:
                positions[0].mkt_px = inferred
        # Only comparable when no fills landed after that snapshot.
        later_fills = [f for f in fills if f.ts > last.ts]
        replayed_base_cash = sum(
            value * broker._fx_to_base.get(currency, 0.0)
            for currency, value in cash_by_currency.items()
        )
        if not later_fills and abs(last.cash - replayed_base_cash) > _SNAPSHOT_CASH_TOLERANCE:
            warnings.append(
                f"replayed cash {replayed_base_cash:.2f} != last snapshot cash "
                f"{last.cash:.2f} — was --starting-cash changed for this ledger?"
            )

    restore_warnings = broker.restore_state(
        cash_by_currency, positions, open_orders, fills=fills
    )
    warnings.extend(restore_warnings)

    report = RehydrationReport(
        performed=True,
        cash=sum(
            value * broker._fx_to_base.get(currency, 0.0)
            for currency, value in cash_by_currency.items()
        ),
        n_positions=len(positions),
        n_open_orders=len(open_orders),
        n_fills_seeded=len(fills),
        warnings=warnings,
        fill_ids={f.id for f in fills},
    )
    logger.info("rehydration complete", extra={"summary": report.summary()})
    return report
