"""Tests for the Phase 0.9 #41 SGE domestic-gold provider (real 国内金价).

Parses Sina's free SGE line; network is injected so tests never hit it; every
failure returns None (fail-closed — chart keeps its derived AU9999).
"""

from __future__ import annotations

from datetime import datetime, timezone

from swing_trader.sge_gold import (
    CachedGoldProvider,
    FakeGoldProvider,
    GoldProvider,
    GoldQuote,
    SinaSgeGold,
)

# var hq_str_gds_AU9999="Au99.99,29.98,880.00,885.00,878.00,882.50,...,15:30:00,...";
_OK = ('var hq_str_gds_AU9999="Au99.99,29.98,880.00,885.00,878.00,882.50,'
       '881.00,882.50,100,2026-07-13,15:30:00";')


class TestSinaParse:
    def _p(self, payload):
        return SinaSgeGold(http_get=lambda url, headers, timeout: payload)

    def test_conforms_to_port(self):
        assert isinstance(self._p(_OK), GoldProvider)

    def test_parses_price_in_yuan_per_gram(self):
        q = self._p(_OK).get_spot("AU9999")
        assert q is not None
        assert q.symbol == "AU9999" and 100 <= q.price <= 2000
        assert q.source == "sina-sge" and q.as_of.tzinfo is not None

    def test_unknown_symbol_none(self):
        assert self._p(_OK).get_spot("XAUUSD") is None

    def test_network_failure_none(self):
        def boom(url, headers, timeout):
            raise OSError("403")
        assert SinaSgeGold(http_get=boom).get_spot() is None

    def test_garbage_none(self):
        assert self._p("nonsense").get_spot() is None
        assert self._p('var hq_str_gds_AU9999="";').get_spot() is None
        # no field in the ¥/gram band
        assert self._p('var hq_str_gds_AU9999="Au99.99,0.1,0.2";').get_spot() is None

    def test_none_payload(self):
        assert self._p(None).get_spot() is None


class TestFakeAndCache:
    def test_fake(self):
        p = FakeGoldProvider({"AU9999": 882.5})
        assert p.get_spot("au9999").price == 882.5
        assert p.get_spot("AUTD") is None

    def test_cache(self):
        calls = {"n": 0}

        class Counting:
            def get_spot(self, symbol="AU9999"):
                calls["n"] += 1
                return GoldQuote("AU9999", 880.0, datetime(2026, 7, 13, tzinfo=timezone.utc), "x")

        t = [datetime(2026, 7, 13, 12, tzinfo=timezone.utc)]
        cached = CachedGoldProvider(Counting(), ttl_s=600, clock=lambda: t[0])
        cached.get_spot()
        cached.get_spot()
        assert calls["n"] == 1
