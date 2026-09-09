"""Refresh non-authoritative marks for currently held portfolio instruments."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from swing_trader.portfolio import AccountEnvironment


@dataclass(frozen=True)
class PortfolioMarkRefreshReport:
    environment: str
    refreshed: tuple[str, ...] = field(default_factory=tuple)
    failed: tuple[str, ...] = field(default_factory=tuple)
    skipped: tuple[str, ...] = field(default_factory=tuple)

    def model_dump(self) -> dict:
        return {
            "environment": self.environment,
            "refreshed": list(self.refreshed),
            "failed": list(self.failed),
            "skipped": list(self.skipped),
        }


def refresh_held_marks(
    portfolio,
    feed,
    nav_provider=None,
    *,
    environment: AccountEnvironment | str = AccountEnvironment.LIVE,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PortfolioMarkRefreshReport:
    """Refresh only open holdings in one environment.

    Marks are globally keyed by symbol, so a symbol recorded with conflicting
    currencies is skipped rather than allowing one quote to contaminate another
    currency bucket.
    """

    env = (
        environment
        if isinstance(environment, AccountEnvironment)
        else AccountEnvironment(str(environment))
    )
    symbols: dict[str, set[str]] = {}
    for account in portfolio.list_accounts(environment=env):
        for holding in portfolio.holdings(account.id).holdings:
            if abs(holding.qty) <= 1e-9:
                continue
            symbols.setdefault(holding.symbol.upper(), set()).add(holding.currency)

    refreshed: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    for symbol in sorted(symbols):
        currencies = symbols[symbol]
        if len(currencies) != 1:
            skipped.append(symbol)
            continue
        currency = next(iter(currencies))
        base = symbol.split(".")[0]
        quotable = symbol.endswith((".SS", ".SZ", ".BJ", ".HK", ".KS", ".KQ")) or base.isalpha()
        if quotable:
            try:
                quote = feed.get_quote(symbol)
                portfolio.set_mark(
                    symbol,
                    quote.last,
                    currency=currency,
                    source="live",
                    actor="system",
                    as_of=quote.ts,
                )
                refreshed.append(symbol)
            except Exception:  # one provider failure must not abort the batch
                failed.append(symbol)
            continue
        nav = None
        if nav_provider is not None:
            try:
                nav = nav_provider.get_nav(symbol)
            except Exception:
                nav = None
        if nav is None:
            skipped.append(symbol)
            continue
        portfolio.set_mark(
            symbol,
            nav.price,
            currency=currency,
            source="live",
            actor="system",
            as_of=nav.as_of,
        )
        refreshed.append(symbol)

    return PortfolioMarkRefreshReport(
        environment=env.value,
        refreshed=tuple(refreshed),
        failed=tuple(failed),
        skipped=tuple(skipped),
    )
