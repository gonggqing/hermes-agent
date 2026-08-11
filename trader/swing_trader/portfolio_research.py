"""Read-only projection of the auditable portfolio into research briefs."""

from __future__ import annotations

from swing_trader.brief import HoldingView
from swing_trader.instrument_names import name_for
from swing_trader.portfolio import AccountEnvironment, MarketScope

__all__ = ["portfolio_research_holdings"]


def portfolio_research_holdings(
    journal,
    market: str,
    *,
    name_overrides=None,
    environment: AccountEnvironment = AccountEnvironment.LIVE,
) -> list[HoldingView]:
    """Aggregate one market's journal positions for analysis only.

    Unknown cost remains unknown and mixed-currency ticker records are kept as
    separate rows. The projection has no mutation or broker path.
    """

    scope = MarketScope(market.upper())
    marks = journal.get_marks()
    grouped: dict[tuple[str, str], dict] = {}
    for account in journal.list_accounts(environment=environment):
        if account.market_scope is not scope:
            continue
        for holding in journal.holdings(account.id).holdings:
            if holding.market is not None and holding.market is not scope:
                continue
            key = (holding.symbol, holding.currency)
            row = grouped.setdefault(
                key,
                {
                    "qty": 0.0,
                    "cost": 0.0,
                    "cost_known": True,
                    "accounts": [],
                },
            )
            row["qty"] += holding.qty
            if holding.cost_basis_known and holding.avg_cost is not None:
                row["cost"] += holding.avg_cost * holding.qty
            else:
                row["cost_known"] = False
            row["accounts"].append(account.name)

    output: list[HoldingView] = []
    for (symbol, currency), row in sorted(grouped.items()):
        qty = float(row["qty"])
        if abs(qty) <= 1e-9:
            continue
        avg_px = row["cost"] / qty if row["cost_known"] and qty > 0 else None
        mark = marks.get(symbol)
        mkt_px = mark.price if mark is not None and mark.currency == currency else None
        unrealized_pct = (
            (mkt_px / avg_px - 1.0) * 100.0
            if mkt_px is not None and avg_px is not None and avg_px > 0
            else None
        )
        override = name_overrides.get(symbol) if name_overrides is not None else None
        output.append(
            HoldingView(
                symbol=symbol,
                display_name=override or name_for(symbol),
                currency=currency,
                qty=qty,
                avg_px=avg_px,
                mkt_px=mkt_px,
                unrealized_pct=unrealized_pct,
                environment=environment.value,
                account_names=sorted(set(row["accounts"])),
                source="portfolio_journal",
            )
        )
    return output
