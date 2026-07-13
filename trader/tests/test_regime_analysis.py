"""Tests for regime-segmented walk-forward analysis (Loop.md Phase 0.95).

Verifies the regime classifier and that walk-forward OOS folds are bucketed by
their test-window regime, re-aggregated per regime, and that the coverage gate
(traded across ≥2 regimes) behaves correctly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swing_trader.backtest import WalkForwardFold, WalkForwardResult
from swing_trader.interfaces import Bar
from swing_trader.ledger import Mode, TradeRecord, TradeStats
from swing_trader.regime_analysis import (
    BEAR,
    BULL,
    CHOP,
    UNKNOWN,
    classify_window_regime,
    regime_report,
)

T0 = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _bars(symbol, closes):
    return [Bar(symbol=symbol, ts=T0 + timedelta(days=i), open=c, high=c * 1.01,
                low=c * 0.99, close=c, volume=1e6) for i, c in enumerate(closes)]


def _trade(pnl, hold=3.0):
    return TradeRecord(
        id=f"t{pnl}", mode=Mode.PAPER, symbol="NVDA", qty=1, entry_order_id="e",
        exit_order_id="x", entry_px=100.0, exit_px=100.0 + pnl, pnl=pnl,
        r_multiple=None, hold_days=hold, rationale="", entry_ts=T0,
        exit_ts=T0 + timedelta(days=hold), is_open=False, risk_per_share=1.0,
        entry_commission=0.0, exit_commission=0.0)


def _fold(idx, window, trades, curve=None):
    empty = TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0)
    return WalkForwardFold(
        fold_index=idx, chosen_params_index=0, train_stats=empty, test_stats=empty,
        test_equity_curve=curve or [], test_window=window, test_trades=trades)


class TestClassify:
    def test_bull(self):
        assert classify_window_regime(_bars("SPY", [100, 103, 106])) == BULL

    def test_bear(self):
        assert classify_window_regime(_bars("SPY", [100, 97, 94])) == BEAR

    def test_chop(self):
        assert classify_window_regime(_bars("SPY", [100, 100.5, 100.2])) == CHOP

    def test_unknown_on_empty_or_single(self):
        assert classify_window_regime([]) == UNKNOWN
        assert classify_window_regime(_bars("SPY", [100])) == UNKNOWN

    def test_thresholds_configurable(self):
        bars = _bars("SPY", [100, 101])  # +1%
        assert classify_window_regime(bars) == CHOP  # default bull_pct=2
        assert classify_window_regime(bars, bull_pct=0.5) == BULL


class TestRegimeReport:
    def _data(self):
        # SPY: fold-0 window [0,3) rises (bull), fold-1 window [3,6) falls (bear).
        return {"SPY": _bars("SPY", [100, 103, 106, 106, 102, 98])}

    def test_buckets_by_test_window_regime(self):
        data = self._data()
        wf = WalkForwardResult(
            folds=[
                _fold(0, (0, 3), [_trade(10), _trade(-4)]),   # bull
                _fold(1, (3, 6), [_trade(6)]),                # bear
            ],
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report(data, wf)
        by = {s.regime: s for s in rep.segments}
        assert set(by) == {BULL, BEAR}
        assert by[BULL].stats.n_closed == 2 and by[BULL].stats.total_pnl == 6
        assert by[BEAR].stats.n_closed == 1 and by[BEAR].stats.total_pnl == 6
        assert by[BULL].mean_benchmark_return_pct > 0
        assert by[BEAR].mean_benchmark_return_pct < 0

    def test_coverage_and_passes(self):
        data = self._data()
        wf = WalkForwardResult(
            folds=[_fold(0, (0, 3), [_trade(10)]), _fold(1, (3, 6), [_trade(5)])],
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report(data, wf)
        assert rep.n_regimes_covered == 2
        assert set(rep.regimes_traded) == {BULL, BEAR}
        assert rep.passes(min_regimes=2) is True
        assert rep.passes(min_regimes=3) is False

    def test_single_regime_fails_coverage(self):
        data = {"SPY": _bars("SPY", [100, 103, 106])}
        wf = WalkForwardResult(
            folds=[_fold(0, (0, 3), [_trade(10)])],
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report(data, wf)
        assert rep.n_regimes_covered == 1
        assert rep.passes(min_regimes=2) is False

    def test_require_all_nonneg(self):
        # bull profitable, bear losing → strict gate fails, coverage gate passes.
        data = self._data()
        wf = WalkForwardResult(
            folds=[_fold(0, (0, 3), [_trade(10)]), _fold(1, (3, 6), [_trade(-8)])],
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report(data, wf)
        assert rep.passes(min_regimes=2) is True
        assert rep.passes(min_regimes=2, require_all_nonneg=True) is False
        assert rep.profitable_regimes() == [BULL]

    def test_folds_with_no_trades_dont_count(self):
        data = self._data()
        wf = WalkForwardResult(
            folds=[_fold(0, (0, 3), [_trade(5)]), _fold(1, (3, 6), [])],  # bear empty
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report(data, wf)
        assert rep.regimes_traded == [BULL]  # bear had folds but no trades

    def test_no_benchmark_is_unknown(self):
        wf = WalkForwardResult(
            folds=[_fold(0, (0, 3), [_trade(5)])],
            combined_test_stats=TradeStats(0, 0, 0.0, None, None, None, 0.0, 0.0, None, 0.0),
        )
        rep = regime_report({}, wf)  # no SPY
        assert rep.n_regimes_covered == 0
        assert rep.segments[0].regime == UNKNOWN
