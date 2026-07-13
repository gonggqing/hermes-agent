"""Domestic gold (SGE) spot provider — real 国内金价 (Loop.md P0.9 #41).

The Finance chart shows AU9999 (国内金价) DERIVED from GC=F × CNY=X / 31.1035.
This provides the REAL Shanghai Gold Exchange Au99.99 spot instead, behind a
mockable port so tests never hit the network. The default adapter reads Sina's
free SGE quote line (``gds_AU9999`` / ``gds_AUTD``); ``http_get`` is injected,
and every failure returns None (fail-closed — the chart keeps its derived value
as a graceful fallback, never a guessed price).

Prices are ¥/gram (SGE convention), which is what the Finance tab already shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol, runtime_checkable

from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = [
    "CachedGoldProvider",
    "FakeGoldProvider",
    "GoldProvider",
    "GoldQuote",
    "SinaSgeGold",
]

#: Sina realtime SGE line. Requires a finance.sina.com.cn Referer or it 403s.
_SINA_URL = "https://hq.sinajs.cn/list={code}"
_SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "hermes-finance/0.9",
}
#: var hq_str_gds_AU9999="Au99.99,<time>,<open>,<high>,<low>,<price>,...";
_SINA_LINE = re.compile(r'hq_str_\w+="([^"]*)"')
_BJ = timezone(timedelta(hours=8))


@dataclass
class GoldQuote:
    symbol: str  # "AU9999" | "AUTD"
    price: float  # ¥ per gram
    as_of: datetime
    source: str  # "sina-sge"


@runtime_checkable
class GoldProvider(Protocol):
    def get_spot(self, symbol: str = "AU9999") -> Optional[GoldQuote]:
        ...


def _default_http_get(url: str, headers: dict, timeout: float) -> Optional[str]:
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        # Sina serves GBK; decode leniently (we only need the numeric fields).
        return resp.read().decode("gbk", errors="replace")


class SinaSgeGold:
    """SGE Au99.99 / Au(T+D) spot from Sina (¥/gram). Network via ``http_get``
    (injectable); fail-None on anything."""

    _CODES = {"AU9999": "gds_AU9999", "AUTD": "gds_AUTD"}

    def __init__(self, http_get: Optional[Callable] = None, timeout: float = 6.0) -> None:
        self._get = http_get or _default_http_get
        self._timeout = timeout

    def get_spot(self, symbol: str = "AU9999") -> Optional[GoldQuote]:
        sym = symbol.strip().upper()
        code = self._CODES.get(sym)
        if code is None:
            return None
        try:
            raw = self._get(_SINA_URL.format(code=code), _SINA_HEADERS, self._timeout)
        except Exception as exc:  # noqa: BLE001 — network failure -> None
            logger.warning("sge gold fetch failed", extra={"symbol": sym, "error": str(exc)[:160]})
            return None
        return _parse_sina(sym, raw)


def _parse_sina(symbol: str, raw: Optional[str]) -> Optional[GoldQuote]:
    if not raw:
        return None
    m = _SINA_LINE.search(raw)
    if not m:
        return None
    fields = m.group(1).split(",")
    # gds_AU9999 line: name, time, open, high, low, PRICE(index 5), ...  The
    # exact index can vary; scan for the first plausible ¥/gram price (100..2000).
    price = None
    for f in fields[2:]:
        try:
            v = float(f)
        except (TypeError, ValueError):
            continue
        if 100.0 <= v <= 2000.0:  # ¥/gram sanity band for SGE gold
            price = v
            break
    if price is None:
        return None
    as_of = _parse_sina_time(fields) or datetime.now(timezone.utc)
    return GoldQuote(symbol=symbol, price=price, as_of=as_of, source="sina-sge")


def _parse_sina_time(fields: list) -> Optional[datetime]:
    # look for a HH:MM:SS field, pair with today's Beijing date
    for f in fields:
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}", f.strip()):
            try:
                now_bj = datetime.now(_BJ)
                hh, mm, ss = (int(x) for x in f.strip().split(":"))
                return now_bj.replace(hour=hh, minute=mm, second=ss,
                                      microsecond=0).astimezone(timezone.utc)
            except (ValueError, TypeError):
                return None
    return None


class FakeGoldProvider:
    """Deterministic offline provider for tests: {symbol: price|GoldQuote}."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get_spot(self, symbol: str = "AU9999") -> Optional[GoldQuote]:
        v = self._data.get(symbol.strip().upper())
        if v is None:
            return None
        if isinstance(v, GoldQuote):
            return v
        return GoldQuote(symbol=symbol.upper(), price=float(v),
                         as_of=datetime(2026, 7, 13, tzinfo=timezone.utc), source="sina-sge")


class CachedGoldProvider:
    """TTL cache over a :class:`GoldProvider`; still fail-None."""

    def __init__(self, inner: GoldProvider, ttl_s: float = 300.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._inner = inner
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, Optional[GoldQuote]]] = {}

    def get_spot(self, symbol: str = "AU9999") -> Optional[GoldQuote]:
        sym = symbol.strip().upper()
        now = self._clock().timestamp()
        hit = self._cache.get(sym)
        if hit is not None and (now - hit[0]) <= self._ttl:
            return hit[1]
        q = self._inner.get_spot(sym)
        self._cache[sym] = (now, q)
        return q
