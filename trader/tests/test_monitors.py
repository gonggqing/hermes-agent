"""Tests for swing_trader.monitors (Loop.md §5.2, §3, §9, §11).

Fully deterministic: in-test fakes implement the DataFeed and
BrokerInterface ports — nothing ever touches the network (Loop.md §3).
Covers regime rule boundaries, breadth, ATR/ADV math against hand-computed
values, pool retagging, breaker tripping in both directions, JsonlSink
file naming/content, sentiment scoring, and DataFeedError skipping.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, BrokerInterface, DataFeed, NewsItem, Quote
from swing_trader.monitors import (
    AccountRiskMonitor,
    JsonlSink,
    MarketMonitor,
    NewsMonitor,
    PortfolioMonitor,
    SnapshotSink,
    score_headline,
)
from swing_trader.risk import LiquidityInfo, RiskParams
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    Fill,
    Mode,
    Order,
    Position,
    Role,
)

UTC = timezone.utc
T0 = datetime(2026, 7, 10, 14, 30, tzinfo=UTC)


# --------------------------------------------------------------------- fakes


class FakeFeed(DataFeed):
    """Deterministic in-memory DataFeed; raises DataFeedError on demand."""

    def __init__(
        self,
        bars: dict[str, list[Bar]] | None = None,
        quotes: dict[str, Quote] | None = None,
        news: dict[Optional[str], list[NewsItem]] | None = None,
        fail_bars: set[str] | None = None,
        fail_quotes: set[str] | None = None,
        fail_news: set[str] | None = None,
    ) -> None:
        self.bars = bars or {}
        self.quotes = quotes or {}
        self.news = news or {}
        self.fail_bars = fail_bars or set()
        self.fail_quotes = fail_quotes or set()
        self.fail_news = fail_news or set()

    def get_quote(self, symbol: str) -> Quote:
        if symbol in self.fail_quotes or symbol not in self.quotes:
            raise DataFeedError(f"no quote for {symbol}")
        return self.quotes[symbol]

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if symbol in self.fail_bars or symbol not in self.bars:
            raise DataFeedError(f"no bars for {symbol}")
        return self.bars[symbol][-limit:]

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        if symbol in self.fail_news:
            raise DataFeedError(f"no news for {symbol}")
        return self.news.get(symbol, [])[:limit]


class FakeBroker(BrokerInterface):
    """Deterministic in-memory broker exposing fixed account/positions."""

    def __init__(self, account: AccountSnapshot, positions: list[Position]) -> None:
        self.account = account
        self.positions = positions

    def get_account(self) -> AccountSnapshot:
        return self.account

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def place_order(self, order: Order):  # pragma: no cover - unused
        raise NotImplementedError

    def cancel_order(self, order_id: str) -> bool:  # pragma: no cover - unused
        return False

    def get_orders(self, active_only: bool = False) -> list[Order]:
        return []

    def get_fills(self) -> list[Fill]:
        return []


class ListSink:
    """SnapshotSink capturing writes in memory."""

    def __init__(self) -> None:
        self.writes: list[tuple[str, dict]] = []

    def write(self, kind: str, payload: dict) -> None:
        self.writes.append((kind, payload))


# ------------------------------------------------------------------- helpers


def flat_bars(symbol: str, closes: list[float], volume: float = 1_000_000.0) -> list[Bar]:
    """One bar per close; OHLC collapsed onto the close (TR = 0 friendly)."""
    return [
        Bar(
            symbol=symbol,
            ts=T0 + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=volume,
        )
        for i, c in enumerate(closes)
    ]


def make_account(
    equity: float = 10_000.0,
    cash: float = 5_000.0,
    drawdown_pct: float = 0.0,
) -> AccountSnapshot:
    return AccountSnapshot(
        ts=T0,
        mode=Mode.PAPER,
        equity=equity,
        cash=cash,
        drawdown_pct=drawdown_pct,
        breaker_state=BreakerState.NORMAL,  # brokers always report NORMAL
    )


def vix_quote(last: float) -> Quote:
    return Quote(symbol="^VIX", ts=T0, last=last)


def market_monitor(feed: FakeFeed, **kw) -> MarketMonitor:
    kw.setdefault("index_symbols", ["SPY"])
    kw.setdefault("breadth_symbols", [])
    return MarketMonitor(feed, **kw)


SPY_ABOVE = [100.0] * 49 + [110.0]  # sma50 = 100.2, last 110 -> above
SPY_BELOW = [100.0] * 59 + [90.0]  # sma50(last 50) = 99.8, last 90 -> below


# ------------------------------------------------------------------ JsonlSink


class TestJsonlSink:
    def test_writes_one_json_line_with_date_from_ts(self, tmp_path):
        sink = JsonlSink(tmp_path)
        payload = {"ts": "2026-07-10T15:30:00+00:00", "vix": 17.5}
        sink.write("market", payload)
        path = tmp_path / "market-20260710.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == payload

    def test_appends_lines_same_day(self, tmp_path):
        sink = JsonlSink(tmp_path)
        sink.write("market", {"ts": "2026-07-10T15:30:00+00:00", "n": 1})
        sink.write("market", {"ts": "2026-07-10T16:30:00+00:00", "n": 2})
        lines = (tmp_path / "market-20260710.jsonl").read_text().splitlines()
        assert [json.loads(ln)["n"] for ln in lines] == [1, 2]

    def test_filename_date_is_utc(self, tmp_path):
        sink = JsonlSink(tmp_path)
        # 20:00 -05:00 == 01:00 UTC on the NEXT day
        sink.write("news", {"ts": "2026-07-10T20:00:00-05:00"})
        assert (tmp_path / "news-20260711.jsonl").exists()

    def test_accepts_datetime_ts(self, tmp_path):
        sink = JsonlSink(tmp_path)
        sink.write("risk", {"ts": datetime(2026, 7, 9, 23, 0, tzinfo=UTC)})
        assert (tmp_path / "risk-20260709.jsonl").exists()

    def test_satisfies_snapshot_sink_protocol(self, tmp_path):
        assert isinstance(JsonlSink(tmp_path), SnapshotSink)


# -------------------------------------------------------------- score_headline


class TestScoreHeadline:
    def test_positive(self):
        assert score_headline("NVDA beats estimates and raises guidance") == 1.0

    def test_negative(self):
        assert score_headline("Company cuts outlook after regulator probe") == -1.0

    def test_mixed_is_zero(self):
        assert score_headline("Beats on revenue but cuts guidance") == 0.0

    def test_no_keywords_is_zero(self):
        assert score_headline("Company holds annual shareholder meeting") == 0.0

    def test_case_insensitive_and_whole_words_only(self):
        assert score_headline("UPGRADE announced") == 1.0
        # "Surgeon" must not match "surge"
        assert score_headline("Surgeon general issues report") == 0.0

    def test_bounded(self):
        assert -1.0 <= score_headline("miss miss miss beats") <= 1.0
        assert score_headline("miss miss miss beats") == pytest.approx(-0.5)


# ------------------------------------------------------------- MarketMonitor


class TestMarketMonitor:
    def test_risk_on_spy_above_sma50_and_low_vix(self):
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        snap = market_monitor(feed).poll()
        assert snap.risk_on_off == "risk_on"
        assert snap.vix == 15.0
        assert snap.ts.tzinfo is not None

    def test_vix_at_20_boundary_is_not_risk_on(self):
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(20.0)},
        )
        assert market_monitor(feed).poll().risk_on_off == "neutral"

    def test_risk_off_when_vix_above_28(self):
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(28.5)},
        )
        assert market_monitor(feed).poll().risk_on_off == "risk_off"

    def test_vix_at_28_boundary_is_not_risk_off(self):
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(28.0)},
        )
        assert market_monitor(feed).poll().risk_on_off == "neutral"

    def test_risk_off_when_spy_below_200dma(self):
        closes = [100.0] * 199 + [90.0]  # sma200 = 99.95, last 90 -> below
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", closes)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        assert market_monitor(feed).poll().risk_on_off == "risk_off"

    def test_200dma_falls_back_to_50dma_when_short_history(self):
        # only 60 bars; last 90 < sma50 99.8 -> risk_off via the fallback
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_BELOW)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        assert market_monitor(feed).poll().risk_on_off == "risk_off"

    def test_risk_off_wins_over_risk_on_on_conflict(self):
        # SPY above its 50dma with low VIX (risk_on true) BUT below its
        # 200dma (risk_off true) -> safety first: risk_off.
        closes = [120.0] * 150 + [80.0] * 49 + [100.0]
        # sma50 = (49*80 + 100)/50 = 80.4 < 100; sma200 = 110.1 > 100
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", closes)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        assert market_monitor(feed).poll().risk_on_off == "risk_off"

    def test_sma50_dist_pct_math(self):
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        snap = market_monitor(feed).poll()
        spy = snap.indices["SPY"]
        assert spy["last"] == 110.0
        # sma50 = (49*100 + 110)/50 = 100.2
        assert spy["sma50_dist_pct"] == pytest.approx((110.0 - 100.2) / 100.2 * 100.0)

    def test_breadth_counts_and_skips_datafeed_errors(self):
        feed = FakeFeed(
            bars={
                "SPY": flat_bars("SPY", SPY_ABOVE),
                "AAA": flat_bars("AAA", [100.0] * 49 + [110.0]),  # above
                "BBB": flat_bars("BBB", [100.0] * 49 + [90.0]),  # below
            },
            quotes={"^VIX": vix_quote(15.0)},
            fail_bars={"CCC"},
        )
        snap = market_monitor(feed, breadth_symbols=["AAA", "BBB", "CCC"]).poll()
        assert snap.breadth_pct_above_50dma == pytest.approx(50.0)

    def test_missing_vix_quote_means_neutral_not_crash(self):
        feed = FakeFeed(bars={"SPY": flat_bars("SPY", SPY_ABOVE)})
        snap = market_monitor(feed).poll()
        assert snap.vix is None
        assert snap.risk_on_off == "neutral"

    def test_failing_index_symbol_is_skipped(self):
        feed = FakeFeed(quotes={"^VIX": vix_quote(15.0)}, fail_bars={"SPY"})
        snap = market_monitor(feed).poll()
        assert "SPY" not in snap.indices
        assert snap.risk_on_off == "neutral"

    def test_persists_through_sink(self, tmp_path):
        sink = JsonlSink(tmp_path)
        feed = FakeFeed(
            bars={"SPY": flat_bars("SPY", SPY_ABOVE)},
            quotes={"^VIX": vix_quote(15.0)},
        )
        snap = market_monitor(feed, sink=sink).poll()
        files = list(tmp_path.glob("market-*.jsonl"))
        assert len(files) == 1
        assert files[0].name == f"market-{snap.ts:%Y%m%d}.jsonl"
        payload = json.loads(files[0].read_text().splitlines()[0])
        assert payload["risk_on_off"] == "risk_on"


# ---------------------------------------------------------- PortfolioMonitor

ATR_BARS = [
    # (open, high, low, close, volume); hand-computed: TR2=TR3=TR4=4 -> ATR=4
    Bar(symbol="NVDA", ts=T0, open=100, high=102, low=98, close=100, volume=1000),
    Bar(symbol="NVDA", ts=T0 + timedelta(days=1), open=101, high=104, low=100, close=103, volume=1000),
    Bar(symbol="NVDA", ts=T0 + timedelta(days=2), open=103, high=105, low=101, close=102, volume=1000),
    Bar(symbol="NVDA", ts=T0 + timedelta(days=3), open=102, high=103, low=99, close=100, volume=1000),
]


class TestPortfolioMonitor:
    def test_retags_spy_position_to_core(self):
        broker = FakeBroker(
            make_account(),
            [Position(symbol="SPY", qty=10, avg_px=90.0, mkt_px=100.0)],  # broker default: ROTATION
        )
        monitor = PortfolioMonitor(FakeFeed(), broker, symbols=[])
        snap = monitor.poll()
        assert snap.positions[0].pool is Role.CORE
        # broker's own object is never mutated
        assert broker.positions[0].pool is Role.ROTATION

    def test_unknown_symbol_falls_back_to_rotation(self):
        broker = FakeBroker(
            make_account(),
            [Position(symbol="ZZZZ", qty=5, avg_px=50.0, mkt_px=None)],
        )
        snap = PortfolioMonitor(FakeFeed(), broker, symbols=[]).poll()
        assert snap.positions[0].pool is Role.ROTATION

    def test_pool_exposure_pct_of_equity(self):
        broker = FakeBroker(
            make_account(equity=10_000.0),
            [
                Position(symbol="SPY", qty=10, avg_px=90.0, mkt_px=100.0),  # 1000 CORE
                Position(symbol="ZZZZ", qty=5, avg_px=50.0, mkt_px=None),  # 250 cost basis, ROTATION
            ],
        )
        snap = PortfolioMonitor(FakeFeed(), broker, symbols=[]).poll()
        assert snap.pool_exposure_pct[Role.CORE] == pytest.approx(10.0)
        assert snap.pool_exposure_pct[Role.ROTATION] == pytest.approx(2.5)

    def test_watch_state_atr_adv_sma_hand_computed(self):
        feed = FakeFeed(bars={"NVDA": ATR_BARS})
        broker = FakeBroker(make_account(), [])
        snap = PortfolioMonitor(feed, broker, symbols=["NVDA"]).poll()
        ws = snap.watch["NVDA"]
        assert ws.last == 100.0
        assert ws.atr_pct == pytest.approx(4.0)  # ATR 4 / close 100 * 100
        # ADV = mean(close*volume) = (100+103+102+100)*1000/4
        assert ws.avg_dollar_volume == pytest.approx(101_250.0)
        assert ws.sma20 == pytest.approx(101.25)
        assert ws.sma50 == pytest.approx(101.25)

    def test_atr_none_with_single_bar(self):
        feed = FakeFeed(bars={"NVDA": ATR_BARS[:1]})
        broker = FakeBroker(make_account(), [])
        snap = PortfolioMonitor(feed, broker, symbols=["NVDA"]).poll()
        assert snap.watch["NVDA"].atr_pct is None
        assert snap.watch["NVDA"].avg_dollar_volume == pytest.approx(100_000.0)

    def test_liquidity_for_feeds_risk_engine(self):
        feed = FakeFeed(bars={"NVDA": ATR_BARS})
        broker = FakeBroker(make_account(), [])
        monitor = PortfolioMonitor(feed, broker, symbols=["NVDA"])
        assert monitor.liquidity_for("NVDA") is None  # before any poll
        monitor.poll()
        info = monitor.liquidity_for("nvda")  # case-insensitive
        assert isinstance(info, LiquidityInfo)
        assert info.avg_dollar_volume == pytest.approx(101_250.0)
        assert info.atr_pct == pytest.approx(4.0)
        assert monitor.liquidity_for("AAPL") is None  # unknown -> no data, no trade

    def test_datafeed_error_symbols_skipped(self):
        feed = FakeFeed(bars={"NVDA": ATR_BARS}, fail_bars={"AMD"})
        broker = FakeBroker(make_account(), [])
        monitor = PortfolioMonitor(feed, broker, symbols=["NVDA", "AMD"])
        snap = monitor.poll()
        assert set(snap.watch) == {"NVDA"}
        assert monitor.liquidity_for("AMD") is None

    def test_persists_through_sink_with_serialized_pools(self, tmp_path):
        sink = JsonlSink(tmp_path)
        broker = FakeBroker(
            make_account(),
            [Position(symbol="SPY", qty=10, avg_px=90.0, mkt_px=100.0)],
        )
        PortfolioMonitor(FakeFeed(), broker, symbols=[], sink=sink).poll()
        files = list(tmp_path.glob("portfolio-*.jsonl"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text().splitlines()[0])
        assert payload["positions"][0]["pool"] == "core"
        assert payload["pool_exposure_pct"]["core"] == pytest.approx(10.0)


# -------------------------------------------------------------- NewsMonitor


def news_item(headline: str, symbol: Optional[str] = "NVDA") -> NewsItem:
    return NewsItem(symbol=symbol, ts=T0, headline=headline, source="test")


class TestNewsMonitor:
    def test_scores_items_and_averages_per_symbol(self):
        feed = FakeFeed(
            news={
                "NVDA": [
                    news_item("NVDA beats estimates and raises guidance"),
                    news_item("NVDA misses on data center revenue"),
                ]
            }
        )
        snap = NewsMonitor(feed).poll(symbols=["NVDA"])
        assert [i["sentiment"] for i in snap.items] == [1.0, -1.0]
        assert snap.per_symbol_sentiment["NVDA"] == pytest.approx(0.0)

    def test_failing_symbol_skipped(self):
        feed = FakeFeed(
            news={"NVDA": [news_item("NVDA surges to a record")]},
            fail_news={"AMD"},
        )
        snap = NewsMonitor(feed).poll(symbols=["NVDA", "AMD"])
        assert len(snap.items) == 1
        assert snap.per_symbol_sentiment == {"NVDA": 1.0}

    def test_market_wide_news_when_symbols_none(self):
        feed = FakeFeed(
            news={None: [news_item("Stocks surge to a record high", symbol=None)]}
        )
        snap = NewsMonitor(feed).poll()
        assert snap.per_symbol_sentiment == {"MARKET": 1.0}
        assert snap.items[0]["symbol"] is None

    def test_earnings_calendar_stub_returns_empty(self):
        assert NewsMonitor(FakeFeed()).get_earnings_calendar() == []

    def test_persists_json_serializable_items(self, tmp_path):
        sink = JsonlSink(tmp_path)
        feed = FakeFeed(news={"NVDA": [news_item("NVDA beats estimates")]})
        NewsMonitor(feed, sink=sink).poll(symbols=["NVDA"])
        files = list(tmp_path.glob("news-*.jsonl"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text().splitlines()[0])
        assert payload["items"][0]["headline"] == "NVDA beats estimates"
        assert payload["items"][0]["ts"] == T0.isoformat()


# -------------------------------------------------------- AccountRiskMonitor


class TestAccountRiskMonitor:
    def test_breaker_tripped_at_threshold(self):
        broker = FakeBroker(make_account(drawdown_pct=-4.0), [])
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert status.snapshot.breaker_state is BreakerState.TRIPPED
        # broker's snapshot object stays NORMAL (model_copy, no mutation)
        assert broker.account.breaker_state is BreakerState.NORMAL

    def test_breaker_normal_above_threshold(self):
        broker = FakeBroker(make_account(drawdown_pct=-3.99), [])
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert status.snapshot.breaker_state is BreakerState.NORMAL

    def test_breaker_tripped_below_threshold(self):
        broker = FakeBroker(make_account(drawdown_pct=-6.0), [])
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert status.snapshot.breaker_state is BreakerState.TRIPPED

    def test_loose_params_clamped_to_hard_cap(self):
        # params ask for -10% but the hard cap (-4%, Loop.md §3) must win
        broker = FakeBroker(make_account(drawdown_pct=-4.5), [])
        params = RiskParams(daily_drawdown_breaker_pct=-10.0)
        status = AccountRiskMonitor(broker, params).poll()
        assert status.snapshot.breaker_state is BreakerState.TRIPPED

    def test_tighter_params_respected(self):
        params = RiskParams(daily_drawdown_breaker_pct=-2.0)
        tripped = AccountRiskMonitor(
            FakeBroker(make_account(drawdown_pct=-2.0), []), params
        ).poll()
        assert tripped.snapshot.breaker_state is BreakerState.TRIPPED
        normal = AccountRiskMonitor(
            FakeBroker(make_account(drawdown_pct=-1.9), []), params
        ).poll()
        assert normal.snapshot.breaker_state is BreakerState.NORMAL

    def test_warns_on_pool_over_cap(self):
        # SPY -> CORE (retag); 7000/10000 = 70% > 60% CORE cap
        broker = FakeBroker(
            make_account(equity=10_000.0, cash=5_000.0),
            [Position(symbol="SPY", qty=70, avg_px=90.0, mkt_px=100.0)],
        )
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert any("core" in w for w in status.warnings)
        assert status.per_pool_exposure_pct[Role.CORE] == pytest.approx(70.0)

    def test_warns_on_low_cash(self):
        broker = FakeBroker(make_account(equity=10_000.0, cash=500.0), [])
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert any("cash" in w for w in status.warnings)

    def test_no_warnings_when_healthy(self):
        broker = FakeBroker(
            make_account(equity=10_000.0, cash=5_000.0),
            [Position(symbol="SPY", qty=10, avg_px=90.0, mkt_px=100.0)],
        )
        status = AccountRiskMonitor(broker, RiskParams()).poll()
        assert status.warnings == []
        assert status.snapshot.breaker_state is BreakerState.NORMAL

    def test_persists_through_sink(self, tmp_path):
        sink = JsonlSink(tmp_path)
        broker = FakeBroker(make_account(drawdown_pct=-5.0), [])
        AccountRiskMonitor(broker, RiskParams(), sink=sink).poll()
        files = list(tmp_path.glob("risk-*.jsonl"))
        assert len(files) == 1
        payload = json.loads(files[0].read_text().splitlines()[0])
        assert payload["snapshot"]["breaker_state"] == "TRIPPED"

    def test_in_memory_sink_receives_payload(self):
        sink = ListSink()
        broker = FakeBroker(make_account(), [])
        AccountRiskMonitor(broker, sink=sink).poll()
        assert len(sink.writes) == 1
        kind, payload = sink.writes[0]
        assert kind == "risk"
        assert payload["snapshot"]["equity"] == 10_000.0
