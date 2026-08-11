"""Real-portfolio holdings are available to research without mutation paths."""

from types import SimpleNamespace

import pytest

from swing_trader.portfolio import (
    AccountEnvironment,
    AccountHoldings,
    Holding,
    MarketScope,
)
from swing_trader.portfolio_research import portfolio_research_holdings


class _Journal:
    def __init__(self):
        self.cn = SimpleNamespace(
            id="cn-live",
            name="蚂蚁财富",
            market_scope=MarketScope.CN,
            environment=AccountEnvironment.LIVE,
        )
        self.paper = SimpleNamespace(
            id="paper",
            name="IBHK Paper",
            market_scope=MarketScope.US,
            environment=AccountEnvironment.PAPER,
        )

    def list_accounts(self, environment=None):
        return [
            account
            for account in (self.cn, self.paper)
            if environment is None or account.environment is environment
        ]

    def holdings(self, account_id):
        assert account_id == "cn-live"
        return AccountHoldings(
            account_id=account_id,
            holdings=[
                Holding(
                    symbol="017470",
                    market=MarketScope.CN,
                    currency="CNY",
                    qty=2000,
                    avg_cost=2.5,
                    cost_basis_known=True,
                )
            ],
        )

    def get_marks(self):
        return {"017470": SimpleNamespace(symbol="017470", price=3.0, currency="CNY")}


class _Names:
    def get(self, symbol):
        return "嘉实芯片基金" if symbol == "017470" else None


def test_live_market_holdings_become_read_only_research_context() -> None:
    rows = portfolio_research_holdings(_Journal(), "CN", name_overrides=_Names())

    assert len(rows) == 1
    row = rows[0]
    assert row.symbol == "017470" and row.display_name == "嘉实芯片基金"
    assert row.account_names == ["蚂蚁财富"]
    assert row.environment == "live" and row.source == "portfolio_journal"
    assert row.avg_px == pytest.approx(2.5)
    assert row.mkt_px == pytest.approx(3.0)
    assert row.unrealized_pct == pytest.approx(20.0)


def test_paper_accounts_are_not_duplicated_into_live_research_projection() -> None:
    assert portfolio_research_holdings(_Journal(), "US") == []
