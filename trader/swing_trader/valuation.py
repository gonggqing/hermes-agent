"""Portfolio valuation — market value + unrealized P&L (Loop.md P0.9).

Holdings store only FACTS (qty + cost). Valuation is a DERIVED, point-in-time
view that applies a current price to each holding:

    市值 market_value   = qty × price
    盈亏 unrealized_pnl = market_value − cost      (cost = qty × avg_cost)
    盈亏% pnl_pct        = unrealized_pnl / cost

The price comes from a :class:`~swing_trader.portfolio_journal.Mark` (a mutable,
non-authoritative valuation input — live quote, imported NAV, or a manual
mark). When no price OR no cost basis is known the value is left None and the
holding is counted as ``unpriced`` — never guessed (Loop.md P0.9). Pure/offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from swing_trader.portfolio import (
    AccountHoldings,
    AggregatePortfolio,
    CashBalance,
    Holding,
    MarketScope,
)
from swing_trader.portfolio_journal import Mark

__all__ = [
    "CurrencyTotal",
    "ValuedHolding",
    "ValuedPortfolio",
    "value_account",
    "value_aggregate",
    "value_holdings",
]


@dataclass
class ValuedHolding:
    symbol: str
    market: Optional[MarketScope]
    currency: str
    qty: float
    avg_cost: Optional[float]
    cost_basis_known: bool
    price: Optional[float]  # current price applied (from a Mark)
    price_as_of: Optional[datetime]
    price_source: str  # "live" | "csv" | "manual" | "none"
    market_value: Optional[float]
    cost: Optional[float]
    unrealized_pnl: Optional[float]
    pnl_pct: Optional[float]
    accounts: list[str] = field(default_factory=list)


@dataclass
class CurrencyTotal:
    """Totals per currency (no FX — a mixed-currency portfolio yields several
    rows). ``market_value`` includes cash of that currency."""

    currency: str
    holdings_value: float = 0.0  # priced holdings only
    cost: float = 0.0  # cost of the priced+known holdings
    cash: float = 0.0
    unrealized_pnl: float = 0.0
    n_priced: int = 0
    n_unpriced: int = 0

    @property
    def market_value(self) -> float:
        return self.holdings_value + self.cash

    @property
    def pnl_pct(self) -> Optional[float]:
        return (self.unrealized_pnl / self.cost) if self.cost > 0 else None


@dataclass
class ValuedPortfolio:
    holdings: list[ValuedHolding] = field(default_factory=list)
    totals: list[CurrencyTotal] = field(default_factory=list)
    as_of: Optional[datetime] = None


def _value_one(h: Holding, mark: Optional[Mark]) -> ValuedHolding:
    price = mark.price if mark is not None else None
    source = mark.source if mark is not None else "none"
    as_of = mark.as_of if mark is not None else None
    cost = (h.avg_cost * h.qty) if (h.cost_basis_known and h.avg_cost is not None) else None
    if price is None:
        mv = pnl = pct = None
    else:
        mv = price * h.qty
        if cost is not None:
            pnl = mv - cost
            pct = (pnl / cost) if cost != 0 else None
        else:
            pnl = pct = None
    return ValuedHolding(
        symbol=h.symbol, market=h.market, currency=h.currency, qty=h.qty,
        avg_cost=h.avg_cost, cost_basis_known=h.cost_basis_known, price=price,
        price_as_of=as_of, price_source=source, market_value=mv, cost=cost,
        unrealized_pnl=pnl, pnl_pct=pct,
        accounts=list(getattr(h, "accounts", []) or []),
    )


def value_holdings(
    holdings: list[Holding],
    marks: dict[str, Mark],
    cash: Optional[list[CashBalance]] = None,
    *,
    as_of: Optional[datetime] = None,
) -> ValuedPortfolio:
    """Value a set of holdings against the given marks, with per-currency totals
    (cash folded in). Works for one account's :class:`AccountHoldings` or an
    aggregate — pass its ``.holdings``/``.cash``."""
    valued = [_value_one(h, marks.get(h.symbol.upper())) for h in holdings]

    totals: dict[str, CurrencyTotal] = {}
    for vh in valued:
        t = totals.setdefault(vh.currency, CurrencyTotal(currency=vh.currency))
        if vh.market_value is not None:
            t.holdings_value += vh.market_value
            t.n_priced += 1
            if vh.cost is not None and vh.unrealized_pnl is not None:
                t.cost += vh.cost
                t.unrealized_pnl += vh.unrealized_pnl
        else:
            t.n_unpriced += 1
    for cb in cash or []:
        if cb.known and cb.amount is not None:
            totals.setdefault(cb.currency, CurrencyTotal(currency=cb.currency)).cash += cb.amount

    return ValuedPortfolio(
        holdings=valued,
        totals=[totals[c] for c in sorted(totals)],
        as_of=as_of,
    )


def value_account(acc: AccountHoldings, marks: dict[str, Mark]) -> ValuedPortfolio:
    return value_holdings(acc.holdings, marks, acc.cash, as_of=acc.as_of)


def value_aggregate(agg: AggregatePortfolio, marks: dict[str, Mark]) -> ValuedPortfolio:
    # AggregateHolding is shape-compatible with Holding for _value_one (+ accounts)
    return value_holdings(agg.holdings, marks, agg.cash, as_of=agg.as_of)
