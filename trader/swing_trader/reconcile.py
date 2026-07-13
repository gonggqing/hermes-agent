"""Ledger <-> broker reconciliation (Loop.md §5.8, Phase 0.8).

Compares the broker's live positions against the positions IMPLIED by the
ledger's recorded fills, so any drift between the accounting record and the
broker is caught before it can mislead risk sizing or (in Phase 1, with a real
IBKR broker) real orders. In Phase 0 the PaperBroker is the source and the
ledger is written from its fills, so this is a consistency self-check that
should always pass; it becomes load-bearing once the broker is external.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swing_trader.interfaces import BrokerInterface
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.schemas import Mode, Side

logger = get_logger(__name__)

__all__ = ["PositionMismatch", "ReconciliationResult", "reconcile_broker_ledger"]

#: Fractional-share tolerance below which a qty difference is treated as equal.
_QTY_TOL = 1e-6


@dataclass(frozen=True)
class PositionMismatch:
    symbol: str
    broker_qty: float
    ledger_qty: float


@dataclass(frozen=True)
class ReconciliationResult:
    ok: bool
    mismatches: list[PositionMismatch] = field(default_factory=list)
    n_symbols: int = 0

    def summary(self) -> str:
        if self.ok:
            return f"{self.n_symbols} symbol(s) consistent"
        return "; ".join(
            f"{m.symbol}: broker {m.broker_qty:g} vs ledger {m.ledger_qty:g}"
            for m in self.mismatches
        )


def _ledger_positions(ledger: Ledger, mode: Mode) -> dict[str, float]:
    """Net position qty per symbol implied by recorded fills (BUY +, SELL −)."""
    qty: dict[str, float] = {}
    for fill in ledger.get_fills(mode):
        delta = fill.qty if fill.side is Side.BUY else -fill.qty
        qty[fill.symbol] = qty.get(fill.symbol, 0.0) + delta
    return {s: q for s, q in qty.items() if abs(q) > _QTY_TOL}


def reconcile_broker_ledger(
    broker: BrokerInterface, ledger: Ledger, mode: Mode | str
) -> ReconciliationResult:
    """Compare broker positions vs ledger-fill-derived positions. Never raises;
    on any failure it returns a conservative UNRECONCILED result (ok=False)."""
    try:
        mode = Mode(mode)
        broker_pos = {p.symbol: float(p.qty) for p in broker.get_positions()
                      if abs(p.qty) > _QTY_TOL}
        ledger_pos = _ledger_positions(ledger, mode)
    except Exception as exc:  # noqa: BLE001 — reconciliation must never crash
        logger.warning("reconciliation failed", extra={"error": str(exc)[:200]})
        return ReconciliationResult(
            ok=False,
            mismatches=[PositionMismatch("<error>", float("nan"), float("nan"))],
            n_symbols=0,
        )

    symbols = sorted(set(broker_pos) | set(ledger_pos))
    mismatches = [
        PositionMismatch(s, broker_pos.get(s, 0.0), ledger_pos.get(s, 0.0))
        for s in symbols
        if abs(broker_pos.get(s, 0.0) - ledger_pos.get(s, 0.0)) > _QTY_TOL
    ]
    return ReconciliationResult(
        ok=not mismatches, mismatches=mismatches, n_symbols=len(symbols)
    )
