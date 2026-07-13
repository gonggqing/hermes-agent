"""Tests for Phase 0.9 aggregation + reconciliation (Loop.md P0.9 §六, risk).

Aggregation sums DISTINCT accounts (US/HK/CN), keeps source tags, and drops
cost basis to unknown if any contributor is unknown. Reconciliation surfaces
manual-vs-broker drift without merging; with no broker connected the account's
own record is authoritative.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from swing_trader.portfolio import EventSource, EventType, MarketScope, ProviderKind
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.portfolio_reconcile import reconcile_portfolio_account

T0 = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def journal(tmp_path):
    return PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")


def _buy(journal, account_id, symbol, market, ccy, qty, price, key, when=T0):
    from swing_trader.portfolio import PortfolioEvent
    journal.append_event(PortfolioEvent(
        account_id=account_id, event_type=EventType.BUY, symbol=symbol,
        market=market, currency=ccy, qty=qty, price=price, occurred_at=when,
        source=EventSource.MANUAL, idempotency_key=key, actor="g", surface="web"))


class TestAggregate:
    def test_sums_same_symbol_across_accounts(self, journal):
        a = journal.create_account(name="A", market_scope="US", base_currency="USD")
        b = journal.create_account(name="B", market_scope="US", base_currency="USD")
        _buy(journal, a.id, "NVDA", MarketScope.US, "USD", 10, 100, "a")
        _buy(journal, b.id, "NVDA", MarketScope.US, "USD", 5, 200, "b")
        agg = journal.aggregate()
        (h,) = agg.holdings
        assert h.symbol == "NVDA" and h.qty == 15.0
        assert h.avg_cost == pytest.approx((10 * 100 + 5 * 200) / 15)
        assert set(h.accounts) == {a.id, b.id}

    def test_multi_market_holdings(self, journal):
        us = journal.create_account(name="US", market_scope="US", base_currency="USD")
        hk = journal.create_account(name="HK", market_scope="HK", base_currency="HKD")
        _buy(journal, us.id, "NVDA", MarketScope.US, "USD", 10, 100, "u")
        _buy(journal, hk.id, "0700.HK", MarketScope.HK, "HKD", 100, 300, "h")
        syms = {h.symbol for h in journal.aggregate().holdings}
        assert syms == {"NVDA", "0700.HK"}

    def test_unknown_cost_propagates(self, journal):
        a = journal.create_account(name="A", market_scope="US", base_currency="USD")
        b = journal.create_account(name="B", market_scope="US", base_currency="USD")
        _buy(journal, a.id, "NVDA", MarketScope.US, "USD", 10, 100, "a")
        from swing_trader.portfolio import PortfolioEvent
        journal.append_event(PortfolioEvent(  # unknown price in b
            account_id=b.id, event_type=EventType.BUY, symbol="NVDA",
            market=MarketScope.US, currency="USD", qty=5, price=None, occurred_at=T0,
            source=EventSource.MANUAL, idempotency_key="b", actor="g", surface="web"))
        (h,) = journal.aggregate().holdings
        assert h.qty == 15.0 and h.cost_basis_known is False and h.avg_cost is None

    def test_include_in_risk_filter(self, journal):
        a = journal.create_account(name="A", market_scope="US", base_currency="USD",
                                   include_in_risk=True)
        b = journal.create_account(name="B", market_scope="US", base_currency="USD",
                                   include_in_risk=False)
        _buy(journal, a.id, "NVDA", MarketScope.US, "USD", 10, 100, "a")
        _buy(journal, b.id, "AMD", MarketScope.US, "USD", 5, 50, "b")
        syms = {h.symbol for h in journal.aggregate(include_in_risk_only=True).holdings}
        assert syms == {"NVDA"}

    def test_empty(self, journal):
        agg = journal.aggregate()
        assert agg.holdings == [] and agg.accounts == []


@dataclass
class _Pos:
    symbol: str
    qty: float


class TestReconcile:
    def _account_and_holdings(self, journal, provider=ProviderKind.IBKR):
        a = journal.create_account(name="US", market_scope="US", base_currency="USD",
                                   provider=provider)
        _buy(journal, a.id, "NVDA", MarketScope.US, "USD", 10, 100, "a")
        return a, journal.holdings(a.id)

    def test_no_broker_snapshot_manual_authoritative(self, journal):
        a = journal.create_account(name="CN", market_scope="CN", base_currency="CNY",
                                   provider=ProviderKind.MANUAL)
        _buy(journal, a.id, "600519.SS", MarketScope.CN, "CNY", 100, 1800, "a")
        res = reconcile_portfolio_account(a, journal.holdings(a.id), broker_positions=None)
        assert res.ok is True and res.authority == "manual"
        assert "manual" in res.note

    def test_broker_match_reconciled(self, journal):
        a, h = self._account_and_holdings(journal)
        res = reconcile_portfolio_account(a, h, broker_positions=[_Pos("NVDA", 10)])
        assert res.ok is True and res.authority == "broker"

    def test_broker_drift_surfaced(self, journal):
        a, h = self._account_and_holdings(journal)
        res = reconcile_portfolio_account(a, h, broker_positions=[_Pos("NVDA", 8)])
        assert res.ok is False
        (d,) = res.drifts
        assert d.symbol == "NVDA" and d.portfolio_qty == 10 and d.broker_qty == 8
        assert "NVDA" in res.summary()

    def test_broker_has_extra_position(self, journal):
        a, h = self._account_and_holdings(journal)
        res = reconcile_portfolio_account(a, h, broker_positions=[_Pos("NVDA", 10),
                                                                  _Pos("AMD", 3)])
        assert res.ok is False
        assert {d.symbol for d in res.drifts} == {"AMD"}
