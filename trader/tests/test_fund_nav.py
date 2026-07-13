"""Tests for the Phase 0.9 #41 fund NAV provider (场外基金 net-asset-value).

Parses the free 天天基金 (fundgz) real-time estimate; network is injected so tests
never hit it; every failure returns None (fail-closed, never a guessed price).
"""

from __future__ import annotations

from datetime import datetime, timezone

from swing_trader.fund_nav import (
    CachedNavProvider,
    EastmoneyFundNav,
    FakeNavProvider,
    NavProvider,
    NavQuote,
    is_fund_code,
)

# a realistic fundgz payload (jsonpgz wrapper)
_OK = ('jsonpgz({"fundcode":"017436","name":"华宝纳斯达克精选股票发起式(QDII)A",'
       '"jzrq":"2026-07-11","dwjz":"2.3400","gsz":"2.2528","gszzl":"-3.72",'
       '"gztime":"2026-07-13 15:00"});')


class TestIsFundCode:
    def test_bare_six_digit(self):
        assert is_fund_code("017436") and is_fund_code("378546")

    def test_exchange_suffix_not_fund(self):
        assert not is_fund_code("510300.SS") and not is_fund_code("NVDA")

    def test_wrong_length(self):
        assert not is_fund_code("1743") and not is_fund_code("1234567")


class TestEastmoneyParse:
    def _provider(self, payload):
        return EastmoneyFundNav(http_get=lambda url, timeout: payload)

    def test_conforms_to_port(self):
        assert isinstance(self._provider(_OK), NavProvider)

    def test_parses_estimate_gsz(self):
        q = self._provider(_OK).get_nav("017436")
        assert q is not None
        assert q.symbol == "017436" and q.price == 2.2528
        assert q.source == "eastmoney-estimate"
        assert "纳斯达克" in q.name
        assert q.as_of.tzinfo is not None and q.as_of.year == 2026

    def test_falls_back_to_dwjz_when_no_estimate(self):
        payload = ('jsonpgz({"fundcode":"017436","name":"x","dwjz":"2.34",'
                   '"gsz":"","gztime":"2026-07-13 15:00"});')
        q = EastmoneyFundNav(http_get=lambda u, t: payload).get_nav("017436")
        assert q.price == 2.34 and q.source == "eastmoney-nav"

    def test_only_fund_codes(self):
        p = self._provider(_OK)
        assert p.get_nav("510300.SS") is None and p.get_nav("NVDA") is None

    def test_network_failure_returns_none(self):
        def boom(url, timeout):
            raise OSError("connection refused")
        assert EastmoneyFundNav(http_get=boom).get_nav("017436") is None

    def test_garbage_payload_none(self):
        assert self._provider("not jsonp at all").get_nav("017436") is None
        assert self._provider('jsonpgz({"gsz":"abc"});').get_nav("017436") is None
        assert self._provider('jsonpgz({"gsz":"0","dwjz":"0"});').get_nav("017436") is None

    def test_empty_payload_none(self):
        assert self._provider(None).get_nav("017436") is None


class TestFakeAndCache:
    def test_fake_price(self):
        p = FakeNavProvider({"017436": 2.25})
        assert p.get_nav("017436").price == 2.25
        assert p.get_nav("999999") is None

    def test_fake_navquote_passthrough(self):
        nq = NavQuote("017436", 2.25, "n", datetime(2026, 7, 13, tzinfo=timezone.utc), "x")
        assert FakeNavProvider({"017436": nq}).get_nav("017436") is nq

    def test_cache_hits(self):
        calls = {"n": 0}

        class Counting:
            def get_nav(self, code):
                calls["n"] += 1
                return NavQuote(code, 1.0, "", datetime(2026, 7, 13, tzinfo=timezone.utc), "x")

        t = [datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)]
        cached = CachedNavProvider(Counting(), ttl_s=600, clock=lambda: t[0])
        cached.get_nav("017436")
        cached.get_nav("017436")
        assert calls["n"] == 1  # second served from cache

    def test_cache_caches_none(self):
        calls = {"n": 0}

        class Missing:
            def get_nav(self, code):
                calls["n"] += 1
                return None

        cached = CachedNavProvider(Missing(), ttl_s=600)
        cached.get_nav("017436")
        cached.get_nav("017436")
        assert calls["n"] == 1  # a miss is cached too
