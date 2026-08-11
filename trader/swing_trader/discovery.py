"""Deterministic, research-only market discovery (Loop.md Phase 0.95).

The scanner broadens the research pool without broadening trading authority.
It accepts a provenance-bearing universe from a provider, validates every
instrument/evidence edge, computes replayable market/news features and returns
a ranked :class:`DiscoveryPool`.  It has no dependency on CandidateOrder,
RiskEngine, ConfirmationService, ExecutionEngine or a broker.

An LLM may help an upstream provider describe a theme, but an LLM-only ticker
is rejected here: each seed must already be instrument-resolved and carry at
least one dated URL-backed evidence item.  This keeps discovery auditable and
makes identical inputs produce identical rankings.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from enum import Enum
import json
from pathlib import Path
from typing import Any, Callable, Optional, Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field, field_validator

from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed, NewsItem

__all__ = [
    "DiscoveryCandidate",
    "DiscoveryEvidence",
    "DiscoveryFeatures",
    "DiscoveryPool",
    "DiscoverySeed",
    "DiscoveryTheme",
    "DiscoveryUniverseProvider",
    "EvidenceKind",
    "MarketDiscoveryScanner",
    "EastmoneyMarketUniverse",
    "SinaIndustryUniverse",
    "JsonDiscoveryUniverse",
    "KnowledgeDiscoveryUniverse",
    "CompositeDiscoveryUniverse",
    "RejectedDiscovery",
    "StaticDiscoveryUniverse",
]


class EvidenceKind(str, Enum):
    COMPANY = "company"
    FILING = "filing"
    SUPPLY_CHAIN = "supply_chain"
    ETF_CONSTITUENT = "etf_constituent"
    EARNINGS = "earnings"
    NEWS = "news"
    MARKET_SCREEN = "market_screen"


class DiscoveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: EvidenceKind
    source: str = Field(min_length=1, max_length=120)
    url: str = Field(pattern=r"^https?://")
    observed_at: datetime
    summary: str = Field(min_length=1, max_length=500)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


class DiscoverySeed(BaseModel):
    """One resolved instrument-to-theme/component relationship."""

    model_config = ConfigDict(frozen=True)

    symbol: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=160)
    market: str = Field(pattern=r"^(US|HK|CN)$")
    exchange: str = Field(min_length=1, max_length=32)
    currency: str = Field(min_length=3, max_length=3)
    theme: str = Field(min_length=1, max_length=120)
    # ``False`` means the label is only the upstream screener bucket used to
    # diversify coverage, not a verified statement about the issuer's actual
    # industry. Sina's legacy nodes are useful for breadth but contain known
    # stale classifications (for example mining companies in retail buckets).
    theme_verified: bool = True
    component: str = Field(min_length=1, max_length=160)
    relationship: str = Field(min_length=1, max_length=500)
    instrument_resolved: bool = True
    evidence: list[DiscoveryEvidence] = Field(min_length=1)


class DiscoveryTheme(BaseModel):
    """A broad research direction, never a preselected company/component."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, max_length=120)
    query: str = Field(min_length=3, max_length=500)


class DiscoveryFeatures(BaseModel):
    adv20: float
    volume_ratio: float
    trend_20d_pct: float
    relative_strength_20d_pct: float
    news_heat: float
    event_score: float
    moat_score: float
    etf_change_score: float
    # Strength of the broad-market screen that surfaced this symbol.  This is
    # deliberately separate from ``moat_score``: turnover/flow can prioritize
    # research, but it is not evidence of a durable competitive advantage.
    screen_score: float = 0.0


class DiscoveryCandidate(BaseModel):
    symbol: str
    display_name: str
    market: str
    exchange: str
    currency: str
    theme: str
    theme_verified: bool = True
    component: str
    relationship: str
    score: float = Field(ge=0.0, le=100.0)
    rank: int = Field(ge=1)
    reasons: list[str] = Field(default_factory=list)
    features: DiscoveryFeatures
    evidence: list[DiscoveryEvidence]


class RejectedDiscovery(BaseModel):
    symbol: str
    reason: str


class DiscoveryPool(BaseModel):
    market: str
    as_of: datetime
    candidates: list[DiscoveryCandidate] = Field(default_factory=list)
    rejected: list[RejectedDiscovery] = Field(default_factory=list)
    source_count: int = 0
    status: str = "complete"
    notes: list[str] = Field(default_factory=list)


class DiscoveryUniverseProvider(Protocol):
    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]: ...


class StaticDiscoveryUniverse:
    """Deterministic provider used by the bundled evidence catalog and tests."""

    def __init__(self, seeds: list[DiscoverySeed]) -> None:
        self._seeds = list(seeds)

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        del as_of
        key = market.upper()
        return [seed for seed in self._seeds if seed.market == key]


class JsonDiscoveryUniverse:
    """Load evidence-bearing seeds generated by an external broad screener.

    The scanner deliberately does not hard-code a list of fashionable tickers.
    A market-data/filing/news collector writes a JSON array to this file; every
    read is re-validated through :class:`DiscoverySeed`, so malformed or
    LLM-only guesses fail closed. Missing/invalid files yield an empty universe.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        del as_of
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            rows = payload.get("seeds", []) if isinstance(payload, dict) else payload
            parsed = [DiscoverySeed.model_validate(row) for row in rows]
        except (OSError, ValueError, TypeError):
            return []
        key = market.upper()
        return [seed for seed in parsed if seed.market == key]


_EASTMONEY_MARKET_URL = "https://push2.eastmoney.com/api/qt/clist/get"
_EASTMONEY_FIELDS = "f2,f3,f6,f8,f10,f12,f14,f20,f21,f24,f62,f100"


def _default_eastmoney_market_fetch(url: str, timeout: float) -> dict:
    request = Request(url, headers={"User-Agent": "hermes-finance/1.0"})
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 fixed host
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # one bounded retry for transient public-feed failures
            last_error = exc
    assert last_error is not None
    raise last_error


class EastmoneyMarketUniverse:
    """Dynamic, cross-industry A-share research universe.

    The old discovery path depended on a hand-written JSON file or symbols
    already mentioned in the knowledge base.  In production neither source
    was populated, so ``source_count`` stayed at zero forever.  This provider
    starts from Eastmoney's live all-A-share turnover ranking, then applies an
    industry cap.  It therefore changes with the tape and cannot collapse into
    the same technology watchlist every day.

    The rows are *research seeds*, not recommendations.  They still have to
    pass the downstream bar freshness, liquidity, trend, relative-strength
    and evidence-bound ranking before appearing in ``DiscoveryPool``.
    """

    _A_SHARE_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"

    def __init__(
        self,
        fetch: Optional[Callable[[str, float], dict]] = None,
        *,
        timeout: float = 10.0,
        fetch_size: int = 120,
        max_seeds: int = 48,
        max_per_industry: int = 4,
    ) -> None:
        self._fetch = fetch or _default_eastmoney_market_fetch
        self.timeout = timeout
        self.fetch_size = max(20, min(fetch_size, 300))
        self.max_seeds = max(1, max_seeds)
        self.max_per_industry = max(1, max_per_industry)

    def _url(self) -> str:
        query = urlencode(
            {
                "pn": 1,
                "pz": self.fetch_size,
                "po": 1,
                "np": 1,
                "fltt": 2,
                "invt": 2,
                "fid": "f6",  # turnover: liquid names across active industries
                "fs": self._A_SHARE_FILTER,
                "fields": _EASTMONEY_FIELDS,
            }
        )
        return f"{_EASTMONEY_MARKET_URL}?{query}"

    @staticmethod
    def _number(row: dict, key: str) -> float:
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            return 0.0
        return value if value == value else 0.0

    @staticmethod
    def _symbol(code: str) -> tuple[str, str] | None:
        if not (code.isdigit() and len(code) == 6):
            return None
        if code.startswith(("5", "6", "9")):
            return f"{code}.SS", "SSE"
        if code.startswith(("0", "1", "2", "3")):
            return f"{code}.SZ", "SZSE"
        return None

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        if market.upper() != "CN":
            return []
        url = self._url()
        try:
            payload = self._fetch(url, self.timeout)
            rows = payload.get("data", {}).get("diff", [])
        except Exception:
            return []
        if not isinstance(rows, list):
            return []

        selected: list[tuple[int, dict, str, str, str]] = []
        industry_counts: dict[str, int] = {}
        for rank, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                continue
            code = str(row.get("f12") or "").strip()
            name = str(row.get("f14") or "").strip()
            resolved = self._symbol(code)
            if resolved is None or not name or "ST" in name.upper() or "退" in name:
                continue
            if self._number(row, "f2") <= 0 or self._number(row, "f6") <= 0:
                continue
            industry = str(row.get("f100") or "其他").strip() or "其他"
            if industry_counts.get(industry, 0) >= self.max_per_industry:
                continue
            industry_counts[industry] = industry_counts.get(industry, 0) + 1
            symbol, exchange = resolved
            selected.append((rank, row, industry, symbol, exchange))
            if len(selected) >= self.max_seeds:
                break

        total = max(1, len(rows))
        seeds: list[DiscoverySeed] = []
        for rank, row, industry, symbol, exchange in selected:
            turnover = self._number(row, "f6")
            daily = self._number(row, "f3")
            medium = self._number(row, "f24")
            turnover_rate = self._number(row, "f8")
            flow = self._number(row, "f62")
            confidence = _clamp(0.3 + 0.5 * (1.0 - (rank - 1) / total))
            relationship = (
                f"A股全市场成交额动态样本；成交额 {turnover / 1e8:.1f} 亿元，"
                f"当日涨跌 {daily:+.1f}%，中期涨跌 {medium:+.1f}%，"
                f"换手率 {turnover_rate:.1f}%，主力净流入 {flow / 1e8:+.1f} 亿元"
            )
            seeds.append(
                DiscoverySeed(
                    symbol=symbol,
                    display_name=str(row.get("f14") or symbol).strip(),
                    market="CN",
                    exchange=exchange,
                    currency="CNY",
                    theme=f"A股/{industry}",
                    component=industry,
                    relationship=relationship,
                    instrument_resolved=True,
                    evidence=[
                        DiscoveryEvidence(
                            kind=EvidenceKind.MARKET_SCREEN,
                            source="东方财富全市场行情",
                            url=url,
                            observed_at=as_of,
                            summary=relationship,
                            confidence=confidence,
                        )
                    ],
                )
            )
        return seeds


_SINA_MARKET_URL = (
    "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "Market_Center.getHQNodeData"
)


def _default_sina_market_fetch(url: str, timeout: float) -> list[dict]:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; hermes-finance/1.0)",
            "Referer": "https://finance.sina.com.cn/",
        },
    )
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            with urlopen(request, timeout=timeout) as response:  # noqa: S310 fixed host
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                return payload if isinstance(payload, list) else []
        except Exception as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


class SinaIndustryUniverse:
    """Dynamic cross-industry fallback for the mainland discovery pool.

    Eastmoney occasionally closes the connection from Docker/proxy networks.
    Sina exposes a separate public industry ranking.  We query one liquid
    leader from each broad industry concurrently, so the fallback fixes the
    *industry frame* while the securities rotate with current turnover; it is
    not another permanent ticker watchlist.
    """

    _INDUSTRIES = (
        ("传媒娱乐", "new_cmyl"),
        ("电力行业", "new_dlhy"),
        ("电器行业", "new_dqhy"),
        ("电子器件", "new_dzqj"),
        ("电子信息", "new_dzxx"),
        ("发电设备", "new_fdsb"),
        ("飞机制造", "new_fjzz"),
        ("化工行业", "new_hghy"),
        ("环保行业", "new_hbhy"),
        ("机械行业", "new_jxhy"),
        ("建筑建材", "new_jzjc"),
        ("交通运输", "new_jtys"),
        ("煤炭行业", "new_mthy"),
        ("农林牧渔", "new_nlmy"),
        ("汽车制造", "new_qczz"),
        ("商业百货", "new_sybh"),
        ("食品行业", "new_sphy"),
        ("医疗器械", "new_ylqx"),
        ("仪器仪表", "new_yqyb"),
        ("石油行业", "new_syhy"),
        ("金融行业", "new_jrhy"),
        ("生物制药", "new_swzz"),
        ("有色金属", "new_ysjs"),
    )

    def __init__(
        self,
        fetch: Optional[Callable[[str, float], list[dict]]] = None,
        *,
        timeout: float = 8.0,
        max_workers: int = 6,
        industries: Optional[tuple[tuple[str, str], ...]] = None,
    ) -> None:
        self._fetch = fetch or _default_sina_market_fetch
        self.timeout = timeout
        self.max_workers = max(1, min(max_workers, 8))
        self.industries = industries or self._INDUSTRIES

    @staticmethod
    def _url(node: str) -> str:
        query = urlencode(
            {
                "page": 1,
                "num": 3,
                "sort": "amount",
                "asc": 0,
                "node": node,
                "symbol": "",
            }
        )
        return f"{_SINA_MARKET_URL}?{query}"

    def _industry_seed(
        self, item: tuple[str, str], as_of: datetime
    ) -> DiscoverySeed | None:
        industry, node = item
        url = self._url(node)
        try:
            rows = self._fetch(url, self.timeout)
        except Exception:
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code") or "").strip()
            name = str(row.get("name") or "").strip()
            resolved = EastmoneyMarketUniverse._symbol(code)
            if resolved is None or not name or "ST" in name.upper() or "退" in name:
                continue
            try:
                price = float(row.get("trade") or 0)
                turnover = float(row.get("amount") or 0)
                daily = float(row.get("changepercent") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or turnover <= 0:
                continue
            symbol, exchange = resolved
            relationship = (
                f"新浪旧版{industry}节点的成交额动态样本；该节点仅用于分散抽样，"
                "未经交易所/上市公司协会行业分类交叉验证，不代表公司主营归属；"
                f"成交额 {turnover / 1e8:.1f} 亿元，当日涨跌 {daily:+.1f}%"
            )
            return DiscoverySeed(
                symbol=symbol,
                display_name=name,
                market="CN",
                exchange=exchange,
                currency="CNY",
                theme=f"A股/{industry}",
                theme_verified=False,
                component=industry,
                relationship=relationship,
                evidence=[DiscoveryEvidence(
                    kind=EvidenceKind.MARKET_SCREEN,
                    source="新浪财经行业行情",
                    url=url,
                    observed_at=as_of,
                    summary=relationship,
                    confidence=0.62,
                )],
            )
        return None

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        if market.upper() != "CN":
            return []
        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            rows = list(pool.map(
                lambda item: self._industry_seed(item, as_of), self.industries
            ))
        return [row for row in rows if row is not None]


class CompositeDiscoveryUniverse:
    """Union sources deterministically with earlier-provider precedence."""

    def __init__(self, providers: list[DiscoveryUniverseProvider]) -> None:
        self.providers = list(providers)

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        rows: list[DiscoverySeed] = []
        seen: set[str] = set()
        for provider in self.providers:
            try:
                supplied = provider.seeds(market, as_of)
            except Exception:
                continue
            for row in sorted(supplied, key=lambda item: (item.symbol, item.theme)):
                if row.symbol in seen:
                    continue
                seen.add(row.symbol)
                rows.append(row)
        return sorted(rows, key=lambda row: (row.symbol, row.theme))


class KnowledgeDiscoveryUniverse:
    """Discover instruments already evidenced in the dated knowledge base.

    Semantic retrieval broadens beyond the static watchlist using high-level
    themes. A hit becomes a seed only when the document has source provenance
    and its symbol resolves exactly in the requested market. The relationship
    text comes from the stored source document, not an LLM guess.
    """

    def __init__(
        self,
        knowledge: Any,
        instrument_search: Any,
        themes: list[DiscoveryTheme],
        *,
        hits_per_theme: int = 20,
    ) -> None:
        self.knowledge = knowledge
        self.instrument_search = instrument_search
        self.themes = list(themes)
        self.hits_per_theme = hits_per_theme

    def seeds(self, market: str, as_of: datetime) -> list[DiscoverySeed]:
        if self.knowledge is None or self.instrument_search is None:
            return []
        best: dict[str, tuple[float, DiscoverySeed]] = {}
        for theme in sorted(self.themes, key=lambda row: (row.name, row.query)):
            try:
                hits = self.knowledge.search(theme.query, k=self.hits_per_theme)
            except Exception:
                continue
            for hit in hits:
                doc = hit.get("document")
                if doc is None or not getattr(doc, "source_url", ""):
                    continue
                score = float(hit.get("score") or 0.0)
                for raw_symbol in sorted(getattr(doc, "symbols", []) or []):
                    resolved = self._resolve(str(raw_symbol), market)
                    if resolved is None:
                        continue
                    evidence = DiscoveryEvidence(
                        kind=self._evidence_kind(getattr(doc, "doc_type", "")),
                        source=str(getattr(doc, "publisher", "unknown")),
                        url=str(doc.source_url),
                        observed_at=getattr(doc, "retrieved_at", as_of),
                        summary=str(getattr(doc, "title", "") or hit.get("snippet") or "")[:500],
                        confidence=_clamp(score),
                    )
                    seed = DiscoverySeed(
                        symbol=resolved.canonical_symbol,
                        display_name=resolved.display_name,
                        market=market.upper(),
                        exchange=resolved.exchange,
                        currency=resolved.currency,
                        theme=theme.name,
                        component="evidence-backed supply-chain relationship",
                        relationship=str(hit.get("snippet") or evidence.summary)[:500],
                        instrument_resolved=True,
                        evidence=[evidence],
                    )
                    current = best.get(seed.symbol)
                    if current is None or (score, theme.name) > (current[0], current[1].theme):
                        best[seed.symbol] = (score, seed)
        return [best[symbol][1] for symbol in sorted(best)]

    def _resolve(self, symbol: str, market: str):
        from swing_trader.instruments import MarketScope

        try:
            result = self.instrument_search.search(
                symbol, market=MarketScope(market.upper()), limit=10
            )
        except Exception:
            return None
        matches = getattr(result, "matches", result)
        wanted = symbol.strip().upper()
        return next(
            (row for row in matches if row.canonical_symbol.upper() == wanted),
            None,
        )

    @staticmethod
    def _evidence_kind(doc_type: Any) -> EvidenceKind:
        value = str(getattr(doc_type, "value", doc_type)).lower()
        if "filing" in value:
            return EvidenceKind.FILING
        if "earning" in value or "report" in value:
            return EvidenceKind.EARNINGS
        return EvidenceKind.NEWS


_EVENT_TERMS = (
    "earnings",
    "guidance",
    "contract",
    "order",
    "capex",
    "capacity",
    "launch",
    "approval",
    "partnership",
    "backlog",
    "产能",
    "订单",
    "财报",
    "指引",
    "获批",
    "合作",
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _return_20d(bars: list[Bar]) -> float:
    start = bars[-21].close
    return 0.0 if start <= 0 else (bars[-1].close / start - 1.0) * 100.0


def _adv20(bars: list[Bar]) -> float:
    tail = bars[-20:]
    return sum(bar.close * bar.volume for bar in tail) / len(tail)


def _volume_ratio(bars: list[Bar]) -> float:
    history = bars[-21:-1]
    baseline = sum(bar.volume for bar in history) / len(history)
    return 0.0 if baseline <= 0 else bars[-1].volume / baseline


def _recent_news(items: list[NewsItem], now: datetime) -> list[NewsItem]:
    cutoff = now - timedelta(days=14)
    return sorted(
        [item for item in items if item.ts >= cutoff],
        key=lambda item: (item.ts, item.headline, item.url),
        reverse=True,
    )


class MarketDiscoveryScanner:
    """Replayable broad-screen → validation → ranking research funnel."""

    def __init__(
        self,
        feed: DataFeed,
        universe: DiscoveryUniverseProvider,
        *,
        benchmark_symbol: str,
        min_adv: float,
        min_score: float = 45.0,
        max_candidates: int = 12,
        max_bar_age_days: int = 10,
        max_evidence_age_days: int = 730,
        instrument_validator: Optional[Callable[[DiscoverySeed], bool]] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.feed = feed
        self.universe = universe
        self.benchmark_symbol = benchmark_symbol
        self.min_adv = min_adv
        self.min_score = min_score
        self.max_candidates = max_candidates
        self.max_bar_age_days = max_bar_age_days
        self.max_evidence_age_days = max_evidence_age_days
        self.instrument_validator = instrument_validator or (lambda seed: seed.instrument_resolved)
        self.clock = clock

    def scan(self, market: str) -> DiscoveryPool:
        now = self.clock()
        market = market.upper()
        rejected: list[RejectedDiscovery] = []
        accepted: list[DiscoveryCandidate] = []
        benchmark_return = self._benchmark_return()
        seeds = sorted(self.universe.seeds(market, now), key=lambda s: (s.symbol, s.theme))
        if not seeds:
            return DiscoveryPool(
                market=market,
                as_of=now,
                source_count=0,
                status="unavailable",
                notes=[
                    "discovery universe produced no symbols; this is a source outage "
                    "or missing universe, not evidence that no opportunities exist"
                ],
        )

        seen: set[str] = set()
        for seed in seeds:
            if seed.symbol in seen:
                rejected.append(
                    RejectedDiscovery(
                    symbol=seed.symbol, reason="duplicate symbol in discovery universe"
                    )
                )
                continue
            seen.add(seed.symbol)
            reason = self._validate_seed(seed, market, now)
            if reason:
                rejected.append(RejectedDiscovery(symbol=seed.symbol, reason=reason))
                continue
            try:
                bars = self.feed.get_bars(seed.symbol, "1d", limit=80)
            except (DataFeedError, ValueError) as exc:
                rejected.append(
                    RejectedDiscovery(symbol=seed.symbol, reason=f"market data unavailable: {exc}")
                )
                continue
            if len(bars) < 21:
                rejected.append(
                    RejectedDiscovery(symbol=seed.symbol, reason="fewer than 21 daily bars")
                )
                continue
            bars = sorted(bars, key=lambda bar: bar.ts)
            bar_age = (now - bars[-1].ts).total_seconds() / 86400.0
            if bar_age > self.max_bar_age_days:
                rejected.append(
                    RejectedDiscovery(
                    symbol=seed.symbol, reason=f"last bar is {bar_age:.1f} days old"
                    )
                )
                continue
            adv = _adv20(bars)
            if adv < self.min_adv:
                rejected.append(
                    RejectedDiscovery(
                    symbol=seed.symbol,
                    reason=f"ADV20 {adv:.0f} below minimum {self.min_adv:.0f}",
                    )
                )
                continue
            try:
                news = self.feed.get_news(seed.symbol, limit=20)
            except (DataFeedError, ValueError):
                news = []
            recent = _recent_news(news, now)
            features = self._features(seed, bars, recent, benchmark_return)
            score = self._score(features)
            if score < self.min_score:
                rejected.append(
                    RejectedDiscovery(
                    symbol=seed.symbol,
                    reason=f"score {score:.1f} below threshold {self.min_score:.1f}",
                    )
                )
                continue
            accepted.append(
                DiscoveryCandidate(
                symbol=seed.symbol,
                display_name=seed.display_name,
                market=seed.market,
                exchange=seed.exchange,
                currency=seed.currency,
                theme=seed.theme,
                theme_verified=seed.theme_verified,
                component=seed.component,
                relationship=seed.relationship,
                score=score,
                rank=1,
                reasons=self._reasons(features),
                features=features,
                evidence=sorted(seed.evidence, key=lambda e: (e.kind.value, e.url)),
                )
            )

        accepted.sort(key=lambda item: (-item.score, item.symbol))
        accepted = accepted[: self.max_candidates]
        accepted = [item.model_copy(update={"rank": idx + 1}) for idx, item in enumerate(accepted)]
        return DiscoveryPool(
            market=market,
            as_of=now,
            candidates=accepted,
            rejected=sorted(rejected, key=lambda item: (item.symbol, item.reason)),
            source_count=len(seeds),
            status="complete",
        )

    def _benchmark_return(self) -> float:
        try:
            bars = sorted(
                self.feed.get_bars(self.benchmark_symbol, "1d", limit=80),
                key=lambda bar: bar.ts,
            )
        except (DataFeedError, ValueError):
            return 0.0
        return _return_20d(bars) if len(bars) >= 21 else 0.0

    def _validate_seed(self, seed: DiscoverySeed, market: str, now: datetime) -> Optional[str]:
        if seed.market != market:
            return f"seed market {seed.market} does not match scanner market {market}"
        if not self.instrument_validator(seed):
            return "instrument unresolved"
        if not seed.evidence:
            return "no provenance evidence"
        cutoff = now - timedelta(days=self.max_evidence_age_days)
        if all(item.observed_at < cutoff for item in seed.evidence):
            return "all relationship evidence is stale"
        return None

    def _features(
        self,
        seed: DiscoverySeed,
        bars: list[Bar],
        news: list[NewsItem],
        benchmark_return: float,
    ) -> DiscoveryFeatures:
        trend = _return_20d(bars)
        event_hits = sum(
            1 for item in news if any(term in item.headline.lower() for term in _EVENT_TERMS)
        )
        structural = [
            item.confidence
            for item in seed.evidence
            if item.kind
            in {
                EvidenceKind.COMPANY,
                EvidenceKind.FILING,
                EvidenceKind.SUPPLY_CHAIN,
            }
        ]
        screen = [
            item.confidence for item in seed.evidence if item.kind is EvidenceKind.MARKET_SCREEN
        ]
        return DiscoveryFeatures(
            adv20=_adv20(bars),
            volume_ratio=_volume_ratio(bars),
            trend_20d_pct=trend,
            relative_strength_20d_pct=trend - benchmark_return,
            news_heat=_clamp(len(news) / 5.0),
            event_score=_clamp(event_hits / 3.0),
            moat_score=(sum(structural) / len(structural) if structural else 0.0),
            etf_change_score=(
                1.0
                if any(item.kind is EvidenceKind.ETF_CONSTITUENT for item in seed.evidence)
                else 0.0
            ),
            screen_score=(sum(screen) / len(screen) if screen else 0.0),
        )

    @staticmethod
    def _score(features: DiscoveryFeatures) -> float:
        trend = _clamp((features.trend_20d_pct + 5.0) / 25.0)
        relative = _clamp((features.relative_strength_20d_pct + 5.0) / 20.0)
        volume = _clamp((features.volume_ratio - 1.0) / 2.0)
        score = 100.0 * (
            0.18 * trend
            + 0.14 * relative
            + 0.13 * volume
            + 0.12 * features.news_heat
            + 0.08 * features.event_score
            + 0.20 * features.moat_score
            + 0.05 * features.etf_change_score
            + 0.10 * features.screen_score
        )
        return round(score, 4)

    @staticmethod
    def _reasons(features: DiscoveryFeatures) -> list[str]:
        reasons = [
            f"20d trend {features.trend_20d_pct:+.1f}%",
            f"relative strength {features.relative_strength_20d_pct:+.1f}%",
            f"volume {features.volume_ratio:.1f}x 20d average",
            f"evidence confidence {features.moat_score:.2f}",
        ]
        if features.news_heat > 0:
            reasons.append(f"news heat {features.news_heat:.2f}")
        if features.event_score > 0:
            reasons.append(f"event catalyst {features.event_score:.2f}")
        if features.etf_change_score > 0:
            reasons.append("ETF constituent-change evidence")
        if features.screen_score > 0:
            reasons.append(f"broad-market screen {features.screen_score:.2f}")
        return reasons
