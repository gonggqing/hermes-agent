"""On-demand finance endpoints for the conversational agent (Loop.md P0.75-B1):
/v1/quote, /v1/bars, /v1/analyze. READ/ANALYSIS-ONLY — no order/approve here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi.testclient import TestClient

from swing_trader.analysis import StaticFundamentals
from swing_trader.api import FinanceRuntime, create_app
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.ledger import Ledger
from swing_trader.schemas import Mode

UTC = timezone.utc
T0 = datetime(2026, 3, 1, tzinfo=UTC)


class FakeFeed(DataFeed):
    def __init__(self, bars=None, quotes=None, news=None):
        self.bars = bars or {}
        self.quotes = quotes or {}
        self.news = news or {}

    def get_quote(self, symbol: str) -> Quote:
        if symbol not in self.quotes:
            raise DataFeedError(f"no quote for {symbol}")
        return self.quotes[symbol]

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100):
        if symbol not in self.bars:
            raise DataFeedError(f"no bars for {symbol}")
        return self.bars[symbol][-limit:]

    def get_news(self, symbol: Optional[str] = None, limit: int = 20):
        return self.news.get(symbol, [])[:limit]


def _rising(symbol, n=90, start=100.0, step=0.5):
    return [Bar(symbol=symbol, ts=T0 + timedelta(days=i), open=c - 0.2,
                high=c + 0.3, low=c - 0.3, close=c, volume=5_000_000.0)
            for i, c in enumerate(start + step * i for i in range(n))]


def _client(feed=None, fundamentals=None):
    rt = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER,
                        feed=feed, fundamentals=fundamentals)
    return TestClient(create_app(rt))


def _feed():
    return FakeFeed(
        bars={"NVDA": _rising("NVDA")},
        quotes={"NVDA": Quote(symbol="NVDA", ts=T0, last=145.0, volume=1e6)},
        news={"NVDA": [NewsItem(symbol="NVDA", ts=T0, headline="NVDA beats and surges",
                                source="Reuters", url="https://x/1")]},
    )


def test_quote_returns_price_and_note():
    c = _client(feed=_feed())
    r = c.get("/v1/quote", params={"symbol": "NVDA"})
    assert r.status_code == 200
    d = r.json()
    assert d["symbol"] == "NVDA" and d["last"] == 145.0
    assert "yahoo finance" in d["note"].lower()


def test_bars_returns_ohlcv_kline():
    c = _client(feed=_feed())
    r = c.get("/v1/bars", params={"symbol": "NVDA", "limit": 30})
    assert r.status_code == 200
    d = r.json()
    assert d["symbol"] == "NVDA" and len(d["bars"]) == 30
    b = d["bars"][0]
    assert {"ts", "open", "high", "low", "close", "volume"} <= set(b)


def test_analyze_synthesizes_multi_agent_verdict():
    fundamentals = StaticFundamentals({"NVDA": {"rev_growth_pct": 25.0, "fwd_pe": 30.0}})
    c = _client(feed=_feed(), fundamentals=fundamentals)
    r = c.get("/v1/analyze", params={"symbol": "NVDA"})
    assert r.status_code == 200
    d = r.json()
    assert d["symbol"] == "NVDA" and d["last"] == 145.0
    agents = {s["source_agent"] for s in d["signals"]}
    assert "technical" in agents and "fundamental" in agents
    assert d["verdict"] is not None  # debate synthesized a verdict


def test_analyze_without_fundamentals_still_works():
    c = _client(feed=_feed())  # no fundamentals provider
    r = c.get("/v1/analyze", params={"symbol": "NVDA"})
    assert r.status_code == 200
    agents = {s["source_agent"] for s in r.json()["signals"]}
    assert "technical" in agents and "fundamental" not in agents


def test_endpoints_503_when_feed_idle():
    c = _client(feed=None)
    for path in ("/v1/quote", "/v1/bars", "/v1/analyze"):
        assert c.get(path, params={"symbol": "NVDA"}).status_code == 503


def test_quote_404_for_unknown_symbol():
    c = _client(feed=FakeFeed())  # empty feed
    assert c.get("/v1/quote", params={"symbol": "ZZZZ"}).status_code == 404
    assert c.get("/v1/bars", params={"symbol": "ZZZZ"}).status_code == 404
