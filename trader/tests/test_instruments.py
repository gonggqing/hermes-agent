"""Tests for Phase 0.9 instrument search (Loop.md P0.9 backlog #3).

Symbol normalization (US/HK/CN), partial-code and name/alias search, security-
type disambiguation (stock/ETF/fund), same-name disambiguation by exchange, the
TTL cache, and — critically — an explicit ``degraded`` state on provider
failure (never a silent empty). All offline; the fake provider is deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.instruments import (
    CachedInstrumentSearch,
    InstrumentMatch,
    InstrumentSearchProvider,
    StaticInstrumentProvider,
    normalize_symbol,
)
from swing_trader.portfolio import MarketScope, SecurityType


# ---------------------------------------------------------- normalization


class TestNormalize:
    @pytest.mark.parametrize("raw,out", [
        ("nvda", "NVDA"), ("NVDA", "NVDA"), ("brk.b", "BRK"),
    ])
    def test_us(self, raw, out):
        assert normalize_symbol(raw, MarketScope.US) == out

    @pytest.mark.parametrize("raw,out", [
        ("700", "0700.HK"), ("0700", "0700.HK"), ("0700.hk", "0700.HK"),
        ("9988", "9988.HK"),
    ])
    def test_hk_pads_to_four_digits(self, raw, out):
        assert normalize_symbol(raw, MarketScope.HK) == out

    @pytest.mark.parametrize("raw,out", [
        ("600519", "600519.SS"),  # Shanghai (6…)
        ("510300", "510300.SS"),  # Shanghai fund (5…)
        ("000001", "000001.SZ"),  # Shenzhen (0…)
        ("300750", "300750.SZ"),  # ChiNext (3…)
        ("600519.ss", "600519.SS"),  # already-suffixed idempotent
    ])
    def test_cn_shanghai_vs_shenzhen(self, raw, out):
        assert normalize_symbol(raw, MarketScope.CN) == out

    def test_string_market_accepted(self):
        assert normalize_symbol("700", "HK") == "0700.HK"

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="empty"):
            normalize_symbol("  ", MarketScope.US)


# ------------------------------------------------------- static provider


class TestStaticProvider:
    @pytest.fixture()
    def provider(self):
        return StaticInstrumentProvider()

    def test_conforms_to_port(self, provider):
        assert isinstance(provider, InstrumentSearchProvider)

    def test_partial_ticker(self, provider):
        syms = [m.canonical_symbol for m in provider.search("NV")]
        assert "NVDA" in syms

    @pytest.mark.parametrize("q,expect", [
        ("070", "0700.HK"),   # partial HK code
        ("6005", "600519.SS"),  # partial Shanghai code
        ("0000", "000001.SZ"),  # partial Shenzhen code
    ])
    def test_partial_code(self, provider, q, expect):
        assert expect in [m.canonical_symbol for m in provider.search(q)]

    @pytest.mark.parametrize("q,expect", [
        ("NVIDIA", "NVDA"),
        ("腾讯", "0700.HK"),
        ("贵州茅台", "600519.SS"),
        ("沪深300", "510300.SS"),
    ])
    def test_name_and_alias_search(self, provider, q, expect):
        assert expect in [m.canonical_symbol for m in provider.search(q)]

    def test_exact_symbol_ranks_first(self, provider):
        assert provider.search("NVDA")[0].canonical_symbol == "NVDA"

    def test_market_filter(self, provider):
        hk = provider.search("0", market=MarketScope.HK)
        assert hk and all(m.market is MarketScope.HK for m in hk)

    def test_result_carries_exchange_currency_type(self, provider):
        (m,) = [x for x in provider.search("600519") if x.canonical_symbol == "600519.SS"]
        assert m.exchange == "SSE" and m.currency == "CNY"
        assert m.security_type is SecurityType.STOCK
        assert m.display_name == "Kweichow Moutai"

    def test_etf_vs_stock_distinguished(self, provider):
        spy = provider.search("SPY")[0]
        assert spy.security_type is SecurityType.ETF
        nvda = provider.search("NVDA")[0]
        assert nvda.security_type is SecurityType.STOCK

    def test_limit_respected(self, provider):
        assert len(provider.search("0", limit=2)) <= 2

    def test_blank_query_empty(self, provider):
        assert provider.search("   ") == []

    def test_no_match_empty(self, provider):
        assert provider.search("ZZZZZZ") == []


# --------------------------------------------------------------- cache


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t = self.t + timedelta(seconds=secs)


class _CountingProvider:
    def __init__(self, matches):
        self.matches = matches
        self.calls = 0

    def search(self, query, *, market=None, limit=10):
        self.calls += 1
        return list(self.matches)


class _FailingProvider:
    def search(self, query, *, market=None, limit=10):
        raise RuntimeError("upstream search down")


def _match():
    return InstrumentMatch(canonical_symbol="NVDA", display_name="NVIDIA Corp",
                           market=MarketScope.US, exchange="NASDAQ",
                           currency="USD", security_type=SecurityType.STOCK)


class TestCache:
    def test_hit_within_ttl_reuses(self):
        clk = _Clock(datetime(2026, 7, 1, tzinfo=timezone.utc))
        inner = _CountingProvider([_match()])
        cached = CachedInstrumentSearch(inner, ttl_s=300, clock=clk)
        r1 = cached.search("NV")
        r2 = cached.search("NV")
        assert inner.calls == 1  # second served from cache
        assert r1.source == "live" and r2.source == "cache"
        assert r2.degraded is False

    def test_expiry_refetches(self):
        clk = _Clock(datetime(2026, 7, 1, tzinfo=timezone.utc))
        inner = _CountingProvider([_match()])
        cached = CachedInstrumentSearch(inner, ttl_s=60, clock=clk)
        cached.search("NV")
        clk.advance(61)
        cached.search("NV")
        assert inner.calls == 2

    def test_failure_degraded_not_silent_empty(self):
        cached = CachedInstrumentSearch(_FailingProvider(), ttl_s=60)
        res = cached.search("NV")
        assert res.degraded is True
        assert res.source == "unavailable"
        assert res.matches == []

    def test_failure_serves_stale_flagged_degraded(self):
        clk = _Clock(datetime(2026, 7, 1, tzinfo=timezone.utc))

        class Flaky:
            def __init__(self): self.n = 0
            def search(self, query, *, market=None, limit=10):
                self.n += 1
                if self.n == 1:
                    return [_match()]
                raise RuntimeError("down")

        cached = CachedInstrumentSearch(Flaky(), ttl_s=10, clock=clk)
        cached.search("NV")           # populates cache
        clk.advance(11)               # expire
        res = cached.search("NV")     # inner fails → stale served, flagged
        assert res.degraded is True and res.source == "stale"
        assert res.matches[0].canonical_symbol == "NVDA"
