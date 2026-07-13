"""YFinanceFundamentals (Loop.md Phase 0.75 thrust A) — cached, mockable, fail-None."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swing_trader.analysis import FundamentalAgent
from swing_trader.fundamentals import YFinanceFundamentals

UTC = timezone.utc


class FakeTicker:
    def __init__(self, info):
        self._info = info

    def get_info(self):
        return self._info


def _factory(infos, calls):
    def make(symbol):
        calls.append(symbol)
        info = infos.get(symbol.upper())
        if isinstance(info, Exception):
            raise info
        return FakeTicker(info)
    return make


def test_maps_and_scales_fields():
    infos = {"NVDA": {"trailingPE": 60.0, "forwardPE": 35.0,
                      "revenueGrowth": 0.22, "grossMargins": 0.75,
                      "profitMargins": 0.5, "earningsGrowth": 0.3,
                      "marketCap": 3e12, "shortName": "NVIDIA"}}
    prov = YFinanceFundamentals(ticker_factory=_factory(infos, []))
    m = prov.get_metrics("nvda")
    assert m["pe"] == 60.0 and m["fwd_pe"] == 35.0
    assert m["rev_growth_pct"] == 22.0  # fraction -> percent
    assert m["gross_margin_pct"] == 75.0 and m["profit_margin_pct"] == 50.0
    assert m["earnings_growth_pct"] == 30.0 and m["name"] == "NVIDIA"


def test_caches_within_ttl():
    calls: list[str] = []
    infos = {"NVDA": {"trailingPE": 60.0, "revenueGrowth": 0.22}}
    prov = YFinanceFundamentals(ticker_factory=_factory(infos, calls))
    prov.get_metrics("NVDA")
    prov.get_metrics("NVDA")
    assert calls == ["NVDA"]  # second call served from cache


def test_cache_expires_after_ttl():
    calls: list[str] = []
    infos = {"NVDA": {"trailingPE": 60.0, "revenueGrowth": 0.22}}
    now = {"t": datetime(2026, 7, 13, tzinfo=UTC)}
    prov = YFinanceFundamentals(
        ticker_factory=_factory(infos, calls),
        cache_ttl_hours=24.0,
        clock=lambda: now["t"],
    )
    prov.get_metrics("NVDA")
    now["t"] += timedelta(hours=25)
    prov.get_metrics("NVDA")
    assert calls == ["NVDA", "NVDA"]  # re-fetched after TTL


def test_fail_none_on_error_empty_or_no_numeric():
    # factory raises
    prov = YFinanceFundamentals(ticker_factory=_factory({"X": RuntimeError("net")}, []))
    assert prov.get_metrics("X") is None
    # empty info
    prov = YFinanceFundamentals(ticker_factory=_factory({"X": {}}, []))
    assert prov.get_metrics("X") is None
    # info with no usable numeric metric (only a name)
    prov = YFinanceFundamentals(ticker_factory=_factory({"X": {"shortName": "X"}}, []))
    assert prov.get_metrics("X") is None


def test_negative_result_is_cached():
    calls: list[str] = []
    prov = YFinanceFundamentals(ticker_factory=_factory({"X": {}}, calls))
    assert prov.get_metrics("X") is None
    assert prov.get_metrics("X") is None
    assert calls == ["X"]  # bad symbol not re-fetched within TTL


def test_feeds_fundamental_agent():
    infos = {"NVDA": {"forwardPE": 30.0, "revenueGrowth": 0.25}}
    prov = YFinanceFundamentals(ticker_factory=_factory(infos, []))
    sig = FundamentalAgent(prov).analyze("NVDA")
    assert sig is not None and sig.source_agent == "fundamental"
    assert sig.direction.value == "long"  # growth 25% > 15, fwd_pe 30 < 40
