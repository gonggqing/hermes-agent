"""Portfolio reconciliation — manual/imported vs broker (Loop.md P0.9 §六).

For US/HK, connected-IBKR positions are authoritative; manual/imported records
bootstrap history but must never SILENTLY override the broker — any
discrepancy is surfaced as reconciliation DRIFT. For mainland China (and any
account without a connected broker), the human-confirmed manual/imported event
is authoritative in Phase 0.9. This module compares an account's derived
holdings against a broker-position snapshot and reports drift; it never mutates
either side. Pure/offline; never raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from swing_trader.portfolio import AccountHoldings, PortfolioAccount, ProviderKind

__all__ = ["PortfolioDrift", "PortfolioReconResult", "reconcile_portfolio_account"]

_QTY_TOL = 1e-6


@dataclass(frozen=True)
class PortfolioDrift:
    symbol: str
    portfolio_qty: float
    broker_qty: float


@dataclass
class PortfolioReconResult:
    account_id: str
    ok: bool  # True when reconciled (or no broker to reconcile against)
    authority: str  # "broker" | "manual"
    drifts: list[PortfolioDrift] = field(default_factory=list)
    as_of: Optional[datetime] = None
    note: str = ""

    def summary(self) -> str:
        if self.ok:
            return self.note or f"{self.authority} authoritative — reconciled"
        return "; ".join(
            f"{d.symbol}: portfolio {d.portfolio_qty:g} vs broker {d.broker_qty:g}"
            for d in self.drifts
        )


def reconcile_portfolio_account(
    account: PortfolioAccount,
    holdings: AccountHoldings,
    broker_positions: Optional[list] = None,
    *,
    now: Optional[datetime] = None,
) -> PortfolioReconResult:
    """Compare an account's derived holdings against a broker snapshot.

    ``broker_positions`` is a list of objects with ``.symbol`` and ``.qty``
    (e.g. :class:`~swing_trader.schemas.Position`), or None when no broker is
    connected. With no broker snapshot the account's own record is authoritative
    (manual for CN / any un-connected account); drift is only possible once a
    broker snapshot exists and the account's provider is a broker.
    """
    authority = "broker" if account.provider is ProviderKind.IBKR else "manual"

    if broker_positions is None:
        return PortfolioReconResult(
            account_id=account.id, ok=True, authority=authority, as_of=holdings.as_of,
            note=("no broker connected — manual/imported record authoritative"
                  if authority == "manual" else "broker not connected this cycle"),
        )

    port_qty = {h.symbol: h.qty for h in holdings.holdings if abs(h.qty) > _QTY_TOL}
    brk_qty = {
        p.symbol.strip().upper(): float(p.qty)
        for p in broker_positions
        if abs(float(p.qty)) > _QTY_TOL
    }
    drifts = [
        PortfolioDrift(sym, port_qty.get(sym, 0.0), brk_qty.get(sym, 0.0))
        for sym in sorted(set(port_qty) | set(brk_qty))
        if abs(port_qty.get(sym, 0.0) - brk_qty.get(sym, 0.0)) > _QTY_TOL
    ]
    return PortfolioReconResult(
        account_id=account.id, ok=not drifts, authority="broker",
        drifts=drifts, as_of=holdings.as_of,
    )
