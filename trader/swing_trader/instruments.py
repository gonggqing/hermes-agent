"""Instrument search — US / HK / CN type-ahead behind a mockable port (P0.9).

The Portfolio symbol field must resolve a PARTIAL ticker or a company/fund
name into a canonical instrument with exchange, currency and security type, so
the user never needs the exact code (Loop.md §7 P0.9, backlog "US/HK/CN
instrument search"). Requirements honored here:

- **Port + fake in tests.** :class:`InstrumentSearchProvider` is a Protocol;
  :class:`StaticInstrumentProvider` is a curated, deterministic, OFFLINE
  catalog used as the default and in every test — no network (Loop.md §3).
  A live (yfinance) adapter can be slotted behind the same port later.
- **Explicit degraded state.** :class:`CachedInstrumentSearch` caches results
  and, when the inner provider fails, returns ``degraded=True`` rather than
  silently empty — a stale/unavailable source is never presented as current.
- **Same-name disambiguation.** Every match carries exchange + currency +
  security type so two like-named securities can be told apart.

Symbol normalization: US ``NVDA``; HK ``0700.HK``; Shanghai ``600519.SS``;
Shenzhen ``000001.SZ``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from swing_trader.log import get_logger
from swing_trader.portfolio import MarketScope, SecurityType, utcnow

logger = get_logger(__name__)

__all__ = [
    "CachedInstrumentSearch",
    "InstrumentMatch",
    "InstrumentSearchProvider",
    "InstrumentSearchResult",
    "StaticInstrumentProvider",
    "normalize_symbol",
]


# --------------------------------------------------------------- normalization


def normalize_symbol(raw: str, market: MarketScope | str) -> str:
    """Canonicalize a raw code for a market. Idempotent (an already-canonical
    symbol is returned unchanged). Shanghai vs Shenzhen is inferred from the
    leading digit when a bare 6-digit CN code is given."""
    market = MarketScope(market)
    s = raw.strip().upper()
    if not s:
        raise ValueError("empty symbol")

    if market is MarketScope.US:
        return s.split(".")[0]  # bare ticker; drop any suffix

    if market is MarketScope.HK:
        base = s.split(".")[0]
        if not base.isdigit():
            return s  # non-numeric HK ticker (rare) — leave as-is
        return f"{int(base):04d}.HK"

    # CN: Shanghai (.SS) vs Shenzhen (.SZ)
    if s.endswith(".SS") or s.endswith(".SZ"):
        return s
    base = s.split(".")[0]
    if not (base.isdigit() and len(base) == 6):
        return s
    # 6/5/9 -> Shanghai; 0/3/1/2 -> Shenzhen (standard A-share code ranges).
    suffix = "SS" if base[0] in {"6", "5", "9"} else "SZ"
    return f"{base}.{suffix}"


# ------------------------------------------------------------------- models


class InstrumentMatch(BaseModel):
    canonical_symbol: str
    display_name: str
    market: MarketScope
    exchange: str  # NASDAQ | NYSE | SEHK | SSE | SZSE
    currency: str
    security_type: SecurityType
    provider_id: Optional[str] = Field(default=None)  # e.g. IBKR conId


@dataclass
class InstrumentSearchResult:
    """Search outcome. ``degraded`` is True when the underlying source failed —
    surfaced to the UI, never presented as an authoritative empty result."""

    matches: list[InstrumentMatch] = field(default_factory=list)
    degraded: bool = False
    source: str = "static"


@runtime_checkable
class InstrumentSearchProvider(Protocol):
    def search(
        self, query: str, *, market: Optional[MarketScope] = None, limit: int = 10
    ) -> list[InstrumentMatch]:
        ...


# ---------------------------------------------------------- seed catalog


@dataclass(frozen=True)
class _Seed:
    canonical: str
    name: str
    market: MarketScope
    exchange: str
    currency: str
    sec_type: SecurityType
    aliases: tuple[str, ...] = ()


_US = MarketScope.US
_HK = MarketScope.HK
_CN = MarketScope.CN
_ST = SecurityType.STOCK
_ETF = SecurityType.ETF

#: Curated, offline seed set covering the watchlist + common CN/HK names. Not
#: exhaustive — a live adapter behind the same port broadens it later; the seed
#: keeps type-ahead useful and tests hermetic.
_SEED_CATALOG: tuple[_Seed, ...] = (
    _Seed("NVDA", "NVIDIA Corp", _US, "NASDAQ", "USD", _ST, ("nvidia",)),
    _Seed("AMD", "Advanced Micro Devices", _US, "NASDAQ", "USD", _ST, ("amd",)),
    _Seed("MU", "Micron Technology", _US, "NASDAQ", "USD", _ST, ("micron",)),
    _Seed("ANET", "Arista Networks", _US, "NYSE", "USD", _ST, ("arista",)),
    _Seed("AVGO", "Broadcom Inc", _US, "NASDAQ", "USD", _ST, ("broadcom",)),
    _Seed("TSM", "Taiwan Semiconductor ADR", _US, "NYSE", "USD", _ST, ("tsmc", "taiwan semi")),
    _Seed("AAPL", "Apple Inc", _US, "NASDAQ", "USD", _ST, ("apple",)),
    _Seed("MSFT", "Microsoft Corp", _US, "NASDAQ", "USD", _ST, ("microsoft",)),
    _Seed("GOOGL", "Alphabet Inc Class A", _US, "NASDAQ", "USD", _ST, ("google", "alphabet")),
    _Seed("AMZN", "Amazon.com Inc", _US, "NASDAQ", "USD", _ST, ("amazon",)),
    _Seed("META", "Meta Platforms", _US, "NASDAQ", "USD", _ST, ("facebook", "meta")),
    _Seed("TSLA", "Tesla Inc", _US, "NASDAQ", "USD", _ST, ("tesla",)),
    _Seed("SPY", "SPDR S&P 500 ETF Trust", _US, "NYSE", "USD", _ETF, ("s&p 500", "sp500")),
    _Seed("QQQ", "Invesco QQQ Trust", _US, "NASDAQ", "USD", _ETF, ("nasdaq 100",)),
    _Seed("VOO", "Vanguard S&P 500 ETF", _US, "NYSE", "USD", _ETF, ("vanguard",)),
    # Hong Kong (SEHK)
    _Seed("0700.HK", "Tencent Holdings", _HK, "SEHK", "HKD", _ST, ("腾讯", "tencent")),
    _Seed("9988.HK", "Alibaba Group", _HK, "SEHK", "HKD", _ST, ("阿里巴巴", "alibaba")),
    _Seed("3690.HK", "Meituan", _HK, "SEHK", "HKD", _ST, ("美团", "meituan")),
    _Seed("0981.HK", "SMIC", _HK, "SEHK", "HKD", _ST, ("中芯国际", "smic")),
    _Seed("1810.HK", "Xiaomi Corp", _HK, "SEHK", "HKD", _ST, ("小米", "xiaomi")),
    _Seed("9618.HK", "JD.com", _HK, "SEHK", "HKD", _ST, ("京东", "jd")),
    _Seed("2800.HK", "Tracker Fund of Hong Kong", _HK, "SEHK", "HKD", _ETF, ("盈富基金",)),
    # Mainland China (SSE / SZSE)
    _Seed("600519.SS", "Kweichow Moutai", _CN, "SSE", "CNY", _ST, ("贵州茅台", "moutai", "茅台")),
    _Seed("601318.SS", "Ping An Insurance", _CN, "SSE", "CNY", _ST, ("中国平安", "ping an")),
    _Seed("000001.SZ", "Ping An Bank", _CN, "SZSE", "CNY", _ST, ("平安银行",)),
    _Seed("000858.SZ", "Wuliangye Yibin", _CN, "SZSE", "CNY", _ST, ("五粮液", "wuliangye")),
    _Seed("300750.SZ", "CATL", _CN, "SZSE", "CNY", _ST, ("宁德时代", "catl")),
    _Seed("510300.SS", "CSI 300 ETF", _CN, "SSE", "CNY", _ETF, ("沪深300", "csi 300", "hs300")),
    _Seed("510050.SS", "SSE 50 ETF", _CN, "SSE", "CNY", _ETF, ("上证50",)),
)


def _seed_to_match(s: _Seed) -> InstrumentMatch:
    return InstrumentMatch(
        canonical_symbol=s.canonical,
        display_name=s.name,
        market=s.market,
        exchange=s.exchange,
        currency=s.currency,
        security_type=s.sec_type,
    )


class StaticInstrumentProvider:
    """Deterministic, offline instrument search over the seed catalog.

    Matches a partial CODE (canonical symbol or its bare numeric code) or a
    NAME/alias (case-insensitive substring, incl. Chinese names). Results are
    ranked exact-symbol > code-prefix > name-prefix > substring so the best
    candidate leads the type-ahead.
    """

    def __init__(self, seeds: tuple[_Seed, ...] = _SEED_CATALOG) -> None:
        self._seeds = seeds

    @staticmethod
    def _bare_code(canonical: str) -> str:
        return canonical.split(".")[0].upper()

    def _score(self, seed: _Seed, q_upper: str, q_lower: str) -> Optional[int]:
        sym = seed.canonical.upper()
        code = self._bare_code(seed.canonical)
        if sym == q_upper or code == q_upper:
            return 0  # exact
        if sym.startswith(q_upper) or code.startswith(q_upper):
            return 1  # code prefix
        name = seed.name.lower()
        if name.startswith(q_lower):
            return 2  # name prefix
        if q_lower in name or any(q_lower in a.lower() for a in seed.aliases):
            return 3  # name/alias substring
        return None

    def search(
        self, query: str, *, market: Optional[MarketScope] = None, limit: int = 10
    ) -> list[InstrumentMatch]:
        q = query.strip()
        if not q:
            return []
        q_upper, q_lower = q.upper(), q.lower()
        scored: list[tuple[int, int, _Seed]] = []
        for i, seed in enumerate(self._seeds):
            if market is not None and seed.market is not market:
                continue
            score = self._score(seed, q_upper, q_lower)
            if score is not None:
                scored.append((score, i, seed))  # i keeps catalog order stable
        scored.sort(key=lambda t: (t[0], t[1]))
        return [_seed_to_match(s) for _, _, s in scored[: max(1, limit)]]


# ------------------------------------------------------------------ cache


class CachedInstrumentSearch:
    """TTL cache over any :class:`InstrumentSearchProvider`. On provider
    failure returns a ``degraded`` result (never a silent empty) so the UI can
    warn; a cached fresh result is reused within ``ttl_s`` (Loop.md P0.9)."""

    def __init__(
        self,
        inner: InstrumentSearchProvider,
        ttl_s: float = 300.0,
        clock: Callable[[], object] = utcnow,
    ) -> None:
        self._inner = inner
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[tuple, tuple[float, list[InstrumentMatch]]] = {}

    def _now(self) -> float:
        return self._clock().timestamp()

    def search(
        self, query: str, *, market: Optional[MarketScope] = None, limit: int = 10
    ) -> InstrumentSearchResult:
        key = (query.strip().lower(), market.value if market else None, limit)
        now = self._now()
        hit = self._cache.get(key)
        if hit is not None and (now - hit[0]) <= self._ttl:
            return InstrumentSearchResult(matches=list(hit[1]), degraded=False, source="cache")
        try:
            matches = self._inner.search(query, market=market, limit=limit)
        except Exception as exc:  # noqa: BLE001 — search must never crash the UI
            logger.warning("instrument search failed", extra={"error": str(exc)[:160]})
            if hit is not None:  # serve stale but flag degraded
                return InstrumentSearchResult(matches=list(hit[1]), degraded=True, source="stale")
            return InstrumentSearchResult(matches=[], degraded=True, source="unavailable")
        self._cache[key] = (now, matches)
        return InstrumentSearchResult(matches=matches, degraded=False, source="live")
