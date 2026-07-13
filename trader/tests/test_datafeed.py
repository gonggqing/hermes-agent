"""Tests for swing_trader.datafeed (Loop.md §5.1, §8).

Fully deterministic, network-free (Loop.md §3): fake ticker objects are
injected via ``ticker_factory``; the default (real yfinance) path is
exercised only via a fake module planted in ``sys.modules``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pytest

from swing_trader.datafeed import (
    MARKET_PROXY_SYMBOL,
    DataFeedError,
    StubPaidFeed,
    YFinanceFeed,
)
from swing_trader.interfaces import Bar, NewsItem, Quote

UTC = timezone.utc


# --------------------------------------------------------------------------- fakes


class FakeFastInfo:
    """Attribute-style fast_info (like yfinance's FastInfo)."""

    def __init__(
        self,
        last_price: Optional[float] = None,
        bid: Optional[float] = None,
        ask: Optional[float] = None,
    ) -> None:
        self.last_price = last_price
        self.bid = bid
        self.ask = ask


class FakeTicker:
    def __init__(
        self,
        fast_info: Any = None,
        history_df: Optional[pd.DataFrame] = None,
        news: Any = None,
        fast_info_raises: bool = False,
    ) -> None:
        self._fast_info = fast_info
        self._fast_info_raises = fast_info_raises
        self._history_df = history_df
        self._news = news
        self.history_calls: list[dict[str, Any]] = []

    @property
    def fast_info(self) -> Any:
        if self._fast_info_raises:
            raise RuntimeError("fast_info unavailable")
        return self._fast_info

    def history(self, period: str = "1mo", interval: str = "1d") -> pd.DataFrame:
        self.history_calls.append({"period": period, "interval": interval})
        if self._history_df is None:
            return pd.DataFrame()
        return self._history_df

    @property
    def news(self) -> Any:
        return self._news


def make_feed(ticker: FakeTicker) -> tuple[YFinanceFeed, list[str]]:
    """Feed wired to a fake ticker; records symbols the factory was asked for."""
    requested: list[str] = []

    def factory(symbol: str) -> FakeTicker:
        requested.append(symbol)
        return ticker

    return YFinanceFeed(ticker_factory=factory), requested


def ohlcv_df(index: Any, start_price: float = 100.0) -> pd.DataFrame:
    n = len(index)
    closes = [start_price + i for i in range(n)]
    return pd.DataFrame(
        {
            "Open": [c - 1.0 for c in closes],
            "High": [c + 2.0 for c in closes],
            "Low": [c - 2.0 for c in closes],
            "Close": closes,
            "Volume": [1_000 + i for i in range(n)],
        },
        index=index,
    )


# --------------------------------------------------------------------------- get_quote


def test_get_quote_from_fast_info() -> None:
    ticker = FakeTicker(fast_info=FakeFastInfo(last_price=123.45, bid=123.4, ask=123.5))
    feed, requested = make_feed(ticker)

    quote = feed.get_quote("nvda")

    assert isinstance(quote, Quote)
    assert requested == ["NVDA"]
    assert quote.symbol == "NVDA"
    assert quote.last == 123.45
    assert quote.bid == 123.4
    assert quote.ask == 123.5
    assert quote.ts.tzinfo is not None
    assert quote.ts.utcoffset().total_seconds() == 0
    assert ticker.history_calls == []  # no fallback needed


def test_get_quote_fast_info_mapping_style() -> None:
    """New-style camelCase mapping access must also work."""
    ticker = FakeTicker(fast_info={"lastPrice": 55.0, "bid": 54.9, "ask": 55.1})
    feed, _ = make_feed(ticker)

    quote = feed.get_quote("AMD")

    assert quote.last == 55.0
    assert quote.bid == 54.9
    assert quote.ask == 55.1


def test_get_quote_falls_back_to_history_close() -> None:
    idx = pd.DatetimeIndex(
        [datetime(2026, 7, 9, tzinfo=UTC), datetime(2026, 7, 10, tzinfo=UTC)]
    )
    ticker = FakeTicker(fast_info_raises=True, history_df=ohlcv_df(idx, start_price=200.0))
    feed, _ = make_feed(ticker)

    quote = feed.get_quote("TSM")

    assert quote.last == 201.0  # last row close
    assert quote.ts == datetime(2026, 7, 10, tzinfo=UTC)
    assert quote.bid is None and quote.ask is None
    assert ticker.history_calls == [{"period": "5d", "interval": "1d"}]


def test_get_quote_nan_fast_info_falls_back() -> None:
    idx = pd.DatetimeIndex([datetime(2026, 7, 10, tzinfo=UTC)])
    ticker = FakeTicker(
        fast_info=FakeFastInfo(last_price=float("nan")),
        history_df=ohlcv_df(idx, start_price=50.0),
    )
    feed, _ = make_feed(ticker)

    quote = feed.get_quote("MU")

    assert quote.last == 50.0


def test_get_quote_nothing_available_raises() -> None:
    ticker = FakeTicker(fast_info=None, history_df=pd.DataFrame())
    feed, _ = make_feed(ticker)

    with pytest.raises(DataFeedError):
        feed.get_quote("NVDA")


# --------------------------------------------------------------------------- get_bars


def test_get_bars_ascending_utc_and_fields() -> None:
    idx = pd.DatetimeIndex(
        [datetime(2026, 7, 8), datetime(2026, 7, 9), datetime(2026, 7, 10)]
    ).tz_localize("America/New_York")
    ticker = FakeTicker(history_df=ohlcv_df(idx, start_price=100.0))
    feed, _ = make_feed(ticker)

    bars = feed.get_bars("nvda", timeframe="1d", limit=10)

    assert len(bars) == 3
    assert all(isinstance(b, Bar) for b in bars)
    assert all(b.symbol == "NVDA" for b in bars)
    assert [b.close for b in bars] == [100.0, 101.0, 102.0]  # ascending
    for bar in bars:
        assert bar.ts.tzinfo is not None
        assert bar.ts.utcoffset().total_seconds() == 0
    # 2026-07-08 00:00 ET == 04:00 UTC (EDT)
    assert bars[0].ts == datetime(2026, 7, 8, 4, 0, tzinfo=UTC)
    assert bars[0].open == 99.0
    assert bars[0].high == 102.0
    assert bars[0].low == 98.0
    assert bars[0].volume == 1000.0


def test_get_bars_naive_index_localized_utc() -> None:
    idx = pd.DatetimeIndex([datetime(2026, 7, 9), datetime(2026, 7, 10)])  # naive
    ticker = FakeTicker(history_df=ohlcv_df(idx))
    feed, _ = make_feed(ticker)

    bars = feed.get_bars("AVGO")

    assert bars[0].ts == datetime(2026, 7, 9, tzinfo=UTC)
    assert bars[1].ts == datetime(2026, 7, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    "timeframe,interval,period",
    [("5m", "5m", "1d"), ("30m", "30m", "1d"), ("1h", "1h", "1mo"),
     ("1d", "1d", "3mo"), ("1wk", "1wk", "1y"), ("1mo", "1mo", "1y")],
)
def test_get_bars_supports_all_chart_timeframes(timeframe, interval, period) -> None:
    # 1D/5D presets (intraday 5m/30m) + day/week/month presets map to the right
    # yfinance interval + a period large enough for the requested bar count.
    idx = pd.date_range("2026-06-01", periods=10, freq="D", tz="UTC")
    ticker = FakeTicker(history_df=ohlcv_df(idx, start_price=10.0))
    feed, _ = make_feed(ticker)

    feed.get_bars("NVDA", timeframe=timeframe, limit=10)

    assert ticker.history_calls[-1] == {"period": period, "interval": interval}


def test_get_bars_keeps_last_limit_rows() -> None:
    idx = pd.date_range("2026-06-01", periods=10, freq="D", tz="UTC")
    ticker = FakeTicker(history_df=ohlcv_df(idx, start_price=10.0))
    feed, _ = make_feed(ticker)

    bars = feed.get_bars("MSFT", timeframe="1d", limit=4)

    assert len(bars) == 4
    assert [b.close for b in bars] == [16.0, 17.0, 18.0, 19.0]  # LAST 4, ascending


def test_get_bars_sorts_descending_input_ascending() -> None:
    idx = pd.DatetimeIndex(
        [datetime(2026, 7, 10, tzinfo=UTC), datetime(2026, 7, 9, tzinfo=UTC)]
    )
    df = ohlcv_df(idx, start_price=300.0)  # closes: 300 @ Jul-10, 301 @ Jul-9
    ticker = FakeTicker(history_df=df)
    feed, _ = make_feed(ticker)

    bars = feed.get_bars("META")

    assert [b.ts for b in bars] == [
        datetime(2026, 7, 9, tzinfo=UTC),
        datetime(2026, 7, 10, tzinfo=UTC),
    ]
    assert [b.close for b in bars] == [301.0, 300.0]


@pytest.mark.parametrize("timeframe", ["1d", "1h", "1wk"])
def test_get_bars_timeframe_maps_to_yfinance_interval(timeframe: str) -> None:
    idx = pd.date_range("2026-07-01", periods=3, freq="D", tz="UTC")
    ticker = FakeTicker(history_df=ohlcv_df(idx))
    feed, _ = make_feed(ticker)

    feed.get_bars("SPY", timeframe=timeframe, limit=3)

    assert ticker.history_calls[0]["interval"] == timeframe


@pytest.mark.parametrize(
    ("timeframe", "limit", "expected_period"),
    [
        ("1d", 5, "3mo"),
        ("1d", 100, "6mo"),
        ("1d", 300, "2y"),
        ("1d", 5000, "max"),
        ("1h", 100, "1mo"),
        ("1h", 5000, "2y"),  # yfinance hourly history caps at ~730 days
        ("1wk", 100, "5y"),
    ],
)
def test_get_bars_period_scales_with_limit(
    timeframe: str, limit: int, expected_period: str
) -> None:
    idx = pd.date_range("2026-07-01", periods=3, freq="D", tz="UTC")
    ticker = FakeTicker(history_df=ohlcv_df(idx))
    feed, _ = make_feed(ticker)

    feed.get_bars("QQQ", timeframe=timeframe, limit=limit)

    assert ticker.history_calls[0]["period"] == expected_period


def test_get_bars_unknown_timeframe_raises_valueerror() -> None:
    feed, _ = make_feed(FakeTicker())

    with pytest.raises(ValueError, match="timeframe"):
        feed.get_bars("NVDA", timeframe="2h")  # genuinely unsupported interval


def test_get_bars_empty_history_raises() -> None:
    ticker = FakeTicker(history_df=pd.DataFrame())
    feed, _ = make_feed(ticker)

    with pytest.raises(DataFeedError):
        feed.get_bars("NVDA")


# --------------------------------------------------------------------------- get_news


def test_get_news_old_format() -> None:
    ticker = FakeTicker(
        news=[
            {
                "title": "NVDA beats estimates",
                "publisher": "Reuters",
                "link": "https://example.com/nvda",
                "providerPublishTime": 1783900800,  # epoch seconds
            }
        ]
    )
    feed, _ = make_feed(ticker)

    items = feed.get_news("nvda")

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, NewsItem)
    assert item.symbol == "NVDA"
    assert item.headline == "NVDA beats estimates"
    assert item.source == "Reuters"
    assert item.url == "https://example.com/nvda"
    assert item.ts == datetime.fromtimestamp(1783900800, tz=UTC)
    assert item.sentiment is None


def test_get_news_new_format() -> None:
    ticker = FakeTicker(
        news=[
            {
                "content": {
                    "title": "Chip capex hits record",
                    "pubDate": "2026-07-10T14:30:00Z",
                    "provider": {"displayName": "Bloomberg"},
                    "canonicalUrl": {"url": "https://example.com/capex"},
                }
            }
        ]
    )
    feed, _ = make_feed(ticker)

    items = feed.get_news("TSM")

    assert len(items) == 1
    item = items[0]
    assert item.headline == "Chip capex hits record"
    assert item.source == "Bloomberg"
    assert item.url == "https://example.com/capex"
    assert item.ts == datetime(2026, 7, 10, 14, 30, tzinfo=UTC)
    assert item.sentiment is None


def test_get_news_skips_malformed_items() -> None:
    ticker = FakeTicker(
        news=[
            "not-a-dict",
            {"title": "no timestamp"},  # old style, missing epoch
            {"title": "bad epoch", "providerPublishTime": "yesterday"},
            {"content": {"pubDate": "2026-07-10T00:00:00Z"}},  # new style, no title
            {"content": {"title": "bad date", "pubDate": "not-a-date"}},
            {"content": None},
            {
                "title": "the only good one",
                "providerPublishTime": 1783900800,
            },
        ]
    )
    feed, _ = make_feed(ticker)

    items = feed.get_news("NVDA")

    assert [i.headline for i in items] == ["the only good one"]


def test_get_news_none_symbol_uses_market_proxy() -> None:
    ticker = FakeTicker(
        news=[{"title": "Fed holds rates", "providerPublishTime": 1783900800}]
    )
    feed, requested = make_feed(ticker)

    items = feed.get_news(None)

    assert requested == [MARKET_PROXY_SYMBOL] == ["SPY"]
    assert len(items) == 1
    assert items[0].symbol is None  # market-wide, not tagged SPY


def test_get_news_respects_limit_and_empty() -> None:
    many = [
        {"title": f"headline {i}", "providerPublishTime": 1783900800 + i}
        for i in range(10)
    ]
    feed, _ = make_feed(FakeTicker(news=many))
    assert len(feed.get_news("NVDA", limit=3)) == 3

    feed_empty, _ = make_feed(FakeTicker(news=None))
    assert feed_empty.get_news("NVDA") == []


def test_get_news_naive_pubdate_assumed_utc() -> None:
    ticker = FakeTicker(
        news=[{"content": {"title": "naive ts", "pubDate": "2026-07-10T09:00:00"}}]
    )
    feed, _ = make_feed(ticker)

    items = feed.get_news("NVDA")

    assert items[0].ts == datetime(2026, 7, 10, 9, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- lazy import


def test_module_import_does_not_import_yfinance() -> None:
    """Importing swing_trader.datafeed must not import yfinance (checked in a
    clean interpreter so this test is order-independent and network-free)."""
    code = (
        "import sys\n"
        "import swing_trader.datafeed\n"
        "sys.exit(1 if 'yfinance' in sys.modules else 0)\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, env=env, text=True
    )
    assert result.returncode == 0, result.stderr


def test_default_factory_lazily_uses_yfinance_ticker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no factory injected, the feed imports yfinance on first use and
    calls yfinance.Ticker (a fake module here, so no network)."""
    ticker = FakeTicker(fast_info=FakeFastInfo(last_price=42.0))
    requested: list[str] = []

    def fake_ticker_ctor(symbol: str) -> FakeTicker:
        requested.append(symbol)
        return ticker

    fake_yf = types.ModuleType("yfinance")
    fake_yf.Ticker = fake_ticker_ctor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf)

    feed = YFinanceFeed()
    quote = feed.get_quote("nvda")

    assert requested == ["NVDA"]
    assert quote.last == 42.0


# --------------------------------------------------------------------------- paid stub


def test_stub_paid_feed_all_methods_raise() -> None:
    stub = StubPaidFeed()

    with pytest.raises(NotImplementedError, match=r"TODO\(Phase 1\+\)"):
        stub.get_quote("NVDA")
    with pytest.raises(NotImplementedError, match="Polygon/Alpaca/IBKR"):
        stub.get_bars("NVDA", timeframe="1d", limit=10)
    with pytest.raises(NotImplementedError, match=r"TODO\(Phase 1\+\)"):
        stub.get_news(None)
