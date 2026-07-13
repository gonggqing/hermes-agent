"""Tests for swing_trader.backtest (Loop.md §9, backlog 16)."""

from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.backtest import (
    BacktestConfig,
    Backtester,
    ReplayFeed,
    WalkForwardBacktester,
    WalkForwardConfig,
)
from swing_trader.datafeed import DataFeedError
from swing_trader.decision import DecisionParams
from swing_trader.interfaces import Bar
from swing_trader.ledger import Ledger

T0 = datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc)  # 16:00 ET close stamps


def make_bars(symbol: str, n: int, drift: float = 0.004,
              wobble: float = 0.012, base: float = 100.0,
              volume: float = 5_000_000.0) -> list[Bar]:
    """Deterministic trending series with pullbacks (keeps RSI < 70)."""
    bars: list[Bar] = []
    for i in range(n):
        trend = base * (1 + drift) ** i
        cycle = [0.0, 1.0, -1.0, 0.5, -0.8][i % 5]
        close = trend * (1 + wobble * cycle / 2)
        prev = bars[-1].close if bars else close
        o = prev
        hi = max(o, close) * 1.004
        lo = min(o, close) * 0.996
        bars.append(Bar(symbol=symbol, ts=T0 + timedelta(days=i),
                        open=o, high=hi, low=lo, close=close, volume=volume))
    return bars


@pytest.fixture()
def uptrend():
    return {"NVDA": make_bars("NVDA", 140), "SPY": make_bars("SPY", 140, drift=0.002)}


def ledger_at(tmp_path, name: str) -> Ledger:
    return Ledger(url=f"sqlite:///{tmp_path/name}")


class TestReplayFeed:
    def test_cursor_limits_visibility(self, uptrend):
        feed = ReplayFeed(uptrend)
        feed.set_today(10)
        bars = feed.get_bars("NVDA", "1d", limit=100)
        assert len(bars) == 11
        assert bars[-1].ts == uptrend["NVDA"][10].ts

    def test_no_look_ahead(self, uptrend):
        feed = ReplayFeed(uptrend)
        feed.set_today(50)
        bars = feed.get_bars("NVDA", "1d", limit=200)
        future = uptrend["NVDA"][51].ts
        assert all(b.ts < future for b in bars)

    def test_quote_is_as_of_close(self, uptrend):
        feed = ReplayFeed(uptrend)
        feed.set_today(7)
        assert feed.get_quote("nvda").last == pytest.approx(uptrend["NVDA"][7].close)

    def test_errors(self, uptrend):
        feed = ReplayFeed(uptrend)
        with pytest.raises(DataFeedError):
            feed.get_bars("ZZZZ")
        with pytest.raises(ValueError):
            feed.get_bars("NVDA", "1h")
        feed.set_today(9_999)
        with pytest.raises(DataFeedError):
            feed.get_quote("NVDA")
        assert feed.get_news() == []


class TestBacktester:
    def test_uptrend_produces_trades(self, uptrend, tmp_path):
        result = Backtester().run(uptrend, 60, 139, ledger_at(tmp_path, "a.db"))
        assert result.n_days == 79
        assert result.stats.n_closed + len([t for t in result.trades if t.is_open]) >= 1
        assert result.equity_curve
        assert result.final_equity != BacktestConfig().starting_cash

    def test_deterministic(self, uptrend, tmp_path):
        r1 = Backtester().run(uptrend, 60, 100, ledger_at(tmp_path, "d1.db"))
        r2 = Backtester().run(uptrend, 60, 100, ledger_at(tmp_path, "d2.db"))
        assert [e for _, e in r1.equity_curve] == [e for _, e in r2.equity_curve]

    def test_orders_fill_on_next_bar(self, uptrend, tmp_path):
        ledger = ledger_at(tmp_path, "n.db")
        Backtester().run(uptrend, 60, 100, ledger)
        fills = ledger.get_fills("paper")
        assert fills
        bar_ts = {b.ts for b in uptrend["NVDA"]} | {b.ts for b in uptrend["SPY"]}
        decision_days = {uptrend["NVDA"][i].ts for i in range(60, 100)}
        for f in fills:
            assert f.ts in bar_ts
            # a fill can never carry the ts of the day it was DECIDED
            # (it fills on the NEXT bar) — spot-check the first decision day
        first_fill = min(fills, key=lambda f: f.ts)
        assert first_fill.ts > uptrend["NVDA"][60].ts

    def test_everything_is_paper_mode(self, uptrend, tmp_path):
        ledger = ledger_at(tmp_path, "m.db")
        Backtester().run(uptrend, 60, 90, ledger)
        assert ledger.get_trades("live") == []
        assert ledger.get_orders(mode="live") == []
        assert ledger.get_fills("live") == []
        assert ledger.get_snapshots("live") == []

    def test_bad_indices_rejected(self, uptrend, tmp_path):
        with pytest.raises(ValueError, match="look-ahead"):
            Backtester().run(uptrend, 60, 140, ledger_at(tmp_path, "x.db"))
        with pytest.raises(ValueError, match="look-ahead"):
            Backtester().run(uptrend, 90, 90, ledger_at(tmp_path, "y.db"))

    def test_tiny_equity_vetoed_by_risk_engine(self, uptrend, tmp_path):
        cfg = BacktestConfig(starting_cash=50.0)
        result = Backtester(cfg).run(uptrend, 60, 90, ledger_at(tmp_path, "t.db"))
        assert result.stats.n_closed == 0
        assert not result.trades

    def test_risk_off_regime_blocks_entries(self, tmp_path):
        # SPY collapsing below its 50dma -> risk_off -> no NVDA entries
        nvda = make_bars("NVDA", 140)
        spy = make_bars("SPY", 90, drift=0.002)
        crash = make_bars("SPY", 50, drift=-0.02, base=spy[-1].close)
        for i, b in enumerate(crash):
            crash[i] = Bar(symbol="SPY", ts=spy[-1].ts + timedelta(days=i + 1),
                           open=b.open, high=b.high, low=b.low, close=b.close,
                           volume=b.volume)
        data = {"NVDA": nvda, "SPY": spy + crash}
        ledger = ledger_at(tmp_path, "r.db")
        result = Backtester().run(data, 110, 139, ledger)  # deep in the crash
        assert result.stats.n_closed == 0 and not result.trades


class TestWalkForward:
    def test_folds_do_not_overlap(self, uptrend, tmp_path):
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return ledger_at(tmp_path, f"wf{counter['n']}.db")

        wf = WalkForwardBacktester(
            wf=WalkForwardConfig(train_days=30, test_days=20)
        )
        result = wf.run(uptrend, factory)
        assert len(result.folds) >= 2
        # test windows advance by test_days: fold i covers
        # [warmup + train + i*test, +test) — assert equity curves disjoint
        seen = set()
        for fold in result.folds:
            ts = {t for t, _ in fold.test_equity_curve}
            assert not (ts & seen)
            seen |= ts
            # Phase 0.95: folds carry their OOS window + closed trades so the
            # regime analyzer can bucket them.
            lo, hi = fold.test_window
            assert hi - lo == 20  # test_days
            assert all(not t.is_open for t in fold.test_trades)
            assert len(fold.test_trades) == fold.test_stats.n_closed
        # windows advance monotonically and don't overlap
        windows = [f.test_window for f in result.folds]
        assert windows == sorted(windows)

    def test_param_selection_prefers_trading_params(self, uptrend, tmp_path):
        counter = {"n": 0}

        def factory():
            counter["n"] += 1
            return ledger_at(tmp_path, f"ps{counter['n']}.db")

        never_trades = DecisionParams(min_entry_confidence=0.99)
        wf = WalkForwardBacktester(
            wf=WalkForwardConfig(train_days=40, test_days=20,
                                 param_grid=(never_trades, DecisionParams())),
        )
        result = wf.run(uptrend, factory)
        assert result.folds
        # the grid index 1 (default params) actually trades on train windows
        assert all(f.chosen_params_index == 1 for f in result.folds)

    def test_combined_stats_are_oos_only(self, uptrend, tmp_path):
        counter = {"n": 0}
        ledgers: list[Ledger] = []

        def factory():
            counter["n"] += 1
            led = ledger_at(tmp_path, f"c{counter['n']}.db")
            ledgers.append(led)
            return led

        wf = WalkForwardBacktester(
            wf=WalkForwardConfig(train_days=30, test_days=20)
        )
        result = wf.run(uptrend, factory)
        test_closed = sum(f.test_stats.n_closed for f in result.folds)
        assert result.combined_test_stats.n_closed == test_closed
        train_closed = sum(f.train_stats.n_closed for f in result.folds)
        # sanity: training DID trade, and none of it leaked into combined
        assert train_closed > 0
        assert result.combined_test_stats.n_closed < train_closed + test_closed
