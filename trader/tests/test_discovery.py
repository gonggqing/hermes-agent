"""Phase 0.95 dynamic-discovery invariants: deterministic and research-only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from swing_trader.discovery import (
    DiscoveryEvidence,
    DiscoverySeed,
    EastmoneyMarketUniverse,
    EvidenceKind,
    MarketDiscoveryScanner,
    JsonDiscoveryUniverse,
    KnowledgeDiscoveryUniverse,
    SinaIndustryUniverse,
    DiscoveryTheme,
    CompositeDiscoveryUniverse,
    StaticDiscoveryUniverse,
)
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote

UTC = timezone.utc
NOW = datetime(2026, 7, 15, 20, 0, tzinfo=UTC)


class FakeFeed(DataFeed):
    def __init__(self, bars, news=None):
        self.bars = bars
        self.news = news or {}

    def get_quote(self, symbol: str) -> Quote:
        raise AssertionError("scanner does not need quotes")

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100):
        return self.bars[symbol][-limit:]

    def get_news(self, symbol=None, limit=20):
        return self.news.get(symbol, [])[:limit]


def bars(symbol: str, *, trend=0.012, volume_ratio=2.5, adv_volume=2_000_000):
    start = NOW - timedelta(days=79)
    rows = []
    px = 20.0
    for idx in range(80):
        px *= 1.0 + trend
        volume = adv_volume * (volume_ratio if idx == 79 else 1.0)
        rows.append(Bar(symbol=symbol, ts=start + timedelta(days=idx),
                        open=px * .99, high=px * 1.01, low=px * .98,
                        close=px, volume=volume))
    return rows


def seed(symbol="NEW", *, resolved=True, observed=NOW, market="US"):
    return DiscoverySeed(
        symbol=symbol, display_name=f"{symbol} Corp", market=market,
        exchange="NASDAQ" if market == "US" else "SEHK", currency="USD" if market == "US" else "HKD",
        theme="robotics", component="precision motion", relationship="supplies motion systems",
        instrument_resolved=resolved,
        evidence=[DiscoveryEvidence(
            kind=EvidenceKind.SUPPLY_CHAIN, source="issuer", url="https://example.com/evidence",
            observed_at=observed, summary="issuer describes precision-motion products", confidence=.9,
        )],
    )


def scanner(seeds, feed, **kwargs):
    return MarketDiscoveryScanner(
        feed, StaticDiscoveryUniverse(seeds), benchmark_symbol="SPY",
        min_adv=1_000_000, min_score=35, clock=lambda: NOW, **kwargs,
    )


def test_identical_inputs_produce_identical_ranked_pool():
    feed = FakeFeed(
        {"SPY": bars("SPY", trend=.002), "AAA": bars("AAA"), "BBB": bars("BBB", trend=.008)},
        {"AAA": [NewsItem("AAA", NOW - timedelta(days=1), "large contract order", "wire", "https://n/1", .5)]},
    )
    scan = scanner([seed("BBB"), seed("AAA")], feed)
    first = scan.scan("US").model_dump(mode="json")
    second = scan.scan("US").model_dump(mode="json")
    assert first == second
    assert [row["symbol"] for row in first["candidates"]] == ["AAA", "BBB"]


def test_unresolved_or_stale_evidence_never_reaches_pool():
    feed = FakeFeed({"SPY": bars("SPY"), "BAD": bars("BAD"), "OLD": bars("OLD")})
    old = NOW - timedelta(days=800)
    pool = scanner([seed("BAD", resolved=False), seed("OLD", observed=old)], feed).scan("US")
    assert pool.candidates == []
    assert {row.symbol: row.reason for row in pool.rejected} == {
        "BAD": "instrument unresolved",
        "OLD": "all relationship evidence is stale",
    }


def test_liquidity_and_stale_market_data_fail_closed():
    illiquid = bars("ILLIQ", adv_volume=100)
    stale = bars("STALE")
    for row in stale:
        row.ts -= timedelta(days=20)
    feed = FakeFeed({"SPY": bars("SPY"), "ILLIQ": illiquid, "STALE": stale})
    pool = scanner([seed("ILLIQ"), seed("STALE")], feed).scan("US")
    assert pool.candidates == []
    assert any("ADV20" in row.reason for row in pool.rejected)
    assert any("days old" in row.reason for row in pool.rejected)


def test_cross_market_seed_is_rejected_not_silently_merged():
    feed = FakeFeed({"SPY": bars("SPY"), "0700.HK": bars("0700.HK")})
    pool = scanner([seed("0700.HK", market="HK")], feed).scan("US")
    assert pool.candidates == []
    # Provider filters by market, so it is absent rather than contaminated.
    assert pool.source_count == 0


def test_domain_has_no_execution_authority_types():
    import swing_trader.discovery as module

    exported = set(module.__all__)
    assert not exported.intersection({
        "Order", "CandidateOrder", "RiskEngine", "ConfirmationService",
        "ExecutionEngine", "BrokerInterface", "place_order", "approve_candidate",
    })


def test_json_universe_revalidates_and_fails_closed(tmp_path):
    path = tmp_path / "discovery-seeds.json"
    path.write_text(seed("AAA").model_dump_json(), encoding="utf-8")
    assert JsonDiscoveryUniverse(path).seeds("US", NOW) == []  # object, not array
    path.write_text("[" + seed("AAA").model_dump_json() + "]", encoding="utf-8")
    assert [row.symbol for row in JsonDiscoveryUniverse(path).seeds("US", NOW)] == ["AAA"]
    path.write_text("not-json", encoding="utf-8")
    assert JsonDiscoveryUniverse(path).seeds("US", NOW) == []


class FakeKnowledge:
    def search(self, query, k=20):
        assert "supply chain" in query and k == 20
        doc = SimpleNamespace(
            symbols=["NEW", "BAD"], source_url="https://issuer.example/report",
            publisher="issuer", retrieved_at=NOW,
            title="New capacity and precision-motion orders", doc_type="filing",
        )
        return [{"document": doc, "snippet": "NEW supplies validated motion systems", "score": .82}]


class FakeInstrumentSearch:
    def search(self, query, market=None, limit=10):
        from swing_trader.instruments import InstrumentMatch, InstrumentSearchResult, SecurityType

        matches = []
        if query == "NEW":
            matches.append(InstrumentMatch(
                canonical_symbol="NEW", display_name="New Motion", market=market,
                exchange="NASDAQ", currency="USD", security_type=SecurityType.STOCK,
            ))
        return InstrumentSearchResult(matches=matches)


def test_knowledge_universe_discovers_only_exact_resolved_provenance():
    provider = KnowledgeDiscoveryUniverse(
        FakeKnowledge(), FakeInstrumentSearch(),
        [DiscoveryTheme(name="robotics", query="robotics supply chain")],
    )
    rows = provider.seeds("US", NOW)
    assert [row.symbol for row in rows] == ["NEW"]
    assert rows[0].instrument_resolved is True
    assert rows[0].evidence[0].url == "https://issuer.example/report"
    assert rows[0].evidence[0].kind is EvidenceKind.FILING


def test_composite_universe_uses_explicit_file_override_first(tmp_path):
    path = tmp_path / "seeds.json"
    path.write_text("[" + seed("NEW").model_dump_json() + "]", encoding="utf-8")
    provider = CompositeDiscoveryUniverse([
        JsonDiscoveryUniverse(path),
        KnowledgeDiscoveryUniverse(
            FakeKnowledge(), FakeInstrumentSearch(),
            [DiscoveryTheme(name="knowledge", query="robotics supply chain")],
        ),
    ])
    rows = provider.seeds("US", NOW)
    assert len(rows) == 1 and rows[0].theme == "robotics"


def test_eastmoney_market_universe_is_dynamic_cross_industry_and_resolved():
    rows = [
        {"f12": "300308", "f14": "中际旭创", "f2": 100, "f3": 2,
         "f6": 20_000_000_000, "f8": 3, "f24": 10, "f62": 1_000_000_000,
         "f100": "通信设备"},
        {"f12": "600519", "f14": "贵州茅台", "f2": 1300, "f3": -1,
         "f6": 10_000_000_000, "f8": 1, "f24": 4, "f62": -200_000_000,
         "f100": "酿酒行业"},
        {"f12": "000001", "f14": "平安银行", "f2": 12, "f3": .5,
         "f6": 8_000_000_000, "f8": 2, "f24": 2, "f62": 100_000_000,
         "f100": "银行"},
    ]
    provider = EastmoneyMarketUniverse(
        fetch=lambda url, timeout: {"data": {"diff": rows}},
        max_seeds=3,
        max_per_industry=1,
    )
    seeds = provider.seeds("CN", NOW)
    assert [row.symbol for row in seeds] == ["300308.SZ", "600519.SS", "000001.SZ"]
    assert {row.theme for row in seeds} == {"A股/通信设备", "A股/酿酒行业", "A股/银行"}
    assert all(row.evidence[0].kind is EvidenceKind.MARKET_SCREEN for row in seeds)
    assert provider.seeds("HK", NOW) == []


def test_sina_fallback_selects_live_leader_per_industry_not_fixed_tickers():
    rows = {
        "new_jxhy": [{
            "code": "300308", "name": "中际旭创", "trade": "100",
            "amount": "20000000000", "changepercent": "2.5",
        }],
        "new_ylqx": [{
            "code": "600055", "name": "万东医疗", "trade": "20",
            "amount": "3000000000", "changepercent": "-0.8",
        }],
    }

    def fetch(url, timeout):
        del timeout
        node = url.split("node=")[1].split("&")[0]
        return rows[node]

    provider = SinaIndustryUniverse(
        fetch=fetch,
        industries=(("机械行业", "new_jxhy"), ("医疗器械", "new_ylqx")),
    )
    seeds = provider.seeds("CN", NOW)
    assert [row.symbol for row in seeds] == ["300308.SZ", "600055.SS"]
    assert [row.theme for row in seeds] == ["A股/机械行业", "A股/医疗器械"]
    assert all(row.theme_verified is False for row in seeds)
    assert all("不代表公司主营归属" in row.relationship for row in seeds)
    assert all(row.evidence[0].source == "新浪财经行业行情" for row in seeds)
    assert provider.seeds("US", NOW) == []


def test_empty_universe_is_reported_as_unavailable_not_no_opportunity():
    feed = FakeFeed({"SPY": bars("SPY")})
    pool = scanner([], feed).scan("US")
    assert pool.status == "unavailable"
    assert pool.source_count == 0
    assert "not evidence" in pool.notes[0]
