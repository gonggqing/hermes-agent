"""Tests for Phase 0.9 valuation — market value + unrealized P&L (Loop.md P0.9).

P&L derives from a Mark (current price); when price OR cost is unknown the value
is None and counted as unpriced — never guessed. Totals are per-currency with
cash folded in.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swing_trader.portfolio import CashBalance, Holding, MarketScope
from swing_trader.portfolio_journal import Mark, PortfolioJournal
from swing_trader.valuation import value_holdings

NOW = datetime(2026, 7, 13, tzinfo=timezone.utc)


def _mark(sym, price, source="csv", ccy="CNY"):
    return Mark(sym, price, ccy, NOW, source)


def _h(sym, qty, cost, known=True, market=MarketScope.CN, ccy="CNY"):
    return Holding(symbol=sym, market=market, currency=ccy, qty=qty,
                   avg_cost=cost, cost_basis_known=known)


class TestValueHolding:
    def test_gain(self):
        vp = value_holdings([_h("510300.SS", 800, 4.797)],
                            {"510300.SS": _mark("510300.SS", 4.744)})
        (h,) = vp.holdings
        assert h.price == 4.744
        assert h.market_value == pytest.approx(800 * 4.744)
        assert h.cost == pytest.approx(800 * 4.797)
        assert h.unrealized_pnl == pytest.approx(800 * (4.744 - 4.797))
        assert h.pnl_pct == pytest.approx((4.744 - 4.797) / 4.797)
        assert h.price_source == "csv"

    def test_loss(self):
        vp = value_holdings([_h("588200.SS", 300, 3.179)],
                            {"588200.SS": _mark("588200.SS", 4.484)})
        (h,) = vp.holdings
        assert h.unrealized_pnl == pytest.approx(300 * (4.484 - 3.179))  # gain here
        assert h.unrealized_pnl > 0

    def test_no_mark_is_unpriced(self):
        vp = value_holdings([_h("017436", 743.64, 2.3533)], {})
        (h,) = vp.holdings
        assert h.price is None and h.market_value is None and h.unrealized_pnl is None
        assert h.price_source == "none"
        assert vp.totals[0].n_unpriced == 1 and vp.totals[0].n_priced == 0

    def test_unknown_cost_gives_market_value_but_no_pnl(self):
        vp = value_holdings([_h("X", 10, None, known=False)],
                            {"X": _mark("X", 5.0)})
        (h,) = vp.holdings
        assert h.market_value == pytest.approx(50.0)
        assert h.cost is None and h.unrealized_pnl is None and h.pnl_pct is None

    def test_price_source_live_reported(self):
        vp = value_holdings([_h("A", 1, 1.0)], {"A": _mark("A", 2.0, source="live")})
        assert vp.holdings[0].price_source == "live"


class TestTotals:
    def test_per_currency_with_cash(self):
        holdings = [_h("510300.SS", 800, 4.797, ccy="CNY"),
                    _h("NVDA", 10, 100.0, market=MarketScope.US, ccy="USD")]
        marks = {"510300.SS": _mark("510300.SS", 4.744, ccy="CNY"),
                 "NVDA": _mark("NVDA", 120.0, ccy="USD")}
        cash = [CashBalance("CNY", 35531.0, True), CashBalance("USD", 500.0, True)]
        vp = value_holdings(holdings, marks, cash)
        tot = {t.currency: t for t in vp.totals}
        assert tot["CNY"].cash == 35531.0
        assert tot["CNY"].market_value == pytest.approx(800 * 4.744 + 35531.0)
        assert tot["USD"].market_value == pytest.approx(10 * 120.0 + 500.0)
        assert tot["USD"].unrealized_pnl == pytest.approx(10 * (120.0 - 100.0))
        assert tot["USD"].pnl_pct == pytest.approx(0.2)

    def test_unpriced_excluded_from_totals(self):
        vp = value_holdings([_h("510300.SS", 100, 4.0), _h("017436", 100, 2.0)],
                            {"510300.SS": _mark("510300.SS", 5.0)},  # 017436 unpriced
                            [CashBalance("CNY", 1000.0, True)])
        (t,) = vp.totals
        assert t.n_priced == 1 and t.n_unpriced == 1
        assert t.holdings_value == pytest.approx(100 * 5.0)  # 017436 excluded
        assert t.market_value == pytest.approx(100 * 5.0 + 1000.0)


class TestMarksPersistence:
    @pytest.fixture()
    def journal(self, tmp_path):
        return PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")

    def test_set_get_mark_upsert(self, journal):
        journal.set_mark("510300.SS", 4.7, currency="CNY", source="csv", actor="g")
        assert journal.get_mark("510300.SS").price == 4.7
        journal.set_mark("510300.ss", 4.9, currency="CNY", source="live", actor="sys")
        m = journal.get_mark("510300.SS")  # upsert, case-insensitive
        assert m.price == 4.9 and m.source == "live"
        assert len(journal.get_marks()) == 1

    def test_get_missing_mark_none(self, journal):
        assert journal.get_mark("NOPE") is None

    def test_marks_do_not_affect_holdings(self, journal):
        """A mark is a valuation input, never a holding fact (boundary)."""
        journal.set_mark("510300.SS", 4.7, currency="CNY", source="manual", actor="g")
        a = journal.create_account(name="X", market_scope="CN", base_currency="CNY")
        assert journal.holdings(a.id).holdings == []  # no ghost holding from a mark
