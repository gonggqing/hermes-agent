"""Chinese OTC fund NAV provider (Loop.md P0.9 #41 — domestic data).

Exchange-traded holdings (A-share ETFs, US/HK stocks) get live prices from the
market feed, but the user's 蚂蚁财富 场外基金 (open-end funds, bare 6-digit codes)
have no exchange quote — their price is a daily/estimated NAV (净值). This
provider fetches that NAV so those holdings can be valued (market value + P&L)
instead of showing 未知.

Behind a mockable :class:`NavProvider` port; the default adapter reads 天天基金's
free real-time estimate endpoint (fundgz). Network is injected (``http_get``) so
tests never hit it; every failure returns None (fail-closed — never a guessed
price). Fund NAV also covers gold *funds* (e.g. 前海开源黄金ETF联接); real SGE
AU9999 spot is a further data source (still derived in the chart for now).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlencode

from swing_trader.interfaces import Bar
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = [
    "CachedNavProvider",
    "EastmoneyFundHistory",
    "EastmoneyFundNav",
    "FakeNavProvider",
    "FundHistoryProvider",
    "NavProvider",
    "NavQuote",
    "is_fund_code",
]

_FUNDGZ_URL = "http://fundgz.1234567.com.cn/js/{code}.js"
_FUND_HISTORY_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_JSONP = re.compile(r"jsonpgz\((.*)\);?\s*$", re.S)


def is_fund_code(symbol: str) -> bool:
    """True for a bare 6-digit OTC fund code (no exchange suffix)."""
    base = symbol.split(".")[0].strip()
    return base.isdigit() and len(base) == 6 and "." not in symbol


@dataclass
class NavQuote:
    symbol: str  # the 6-digit fund code
    price: float  # estimated (gsz) or last confirmed (dwjz) NAV
    name: str
    as_of: datetime
    source: str  # "eastmoney-estimate" | "eastmoney-nav"


@runtime_checkable
class NavProvider(Protocol):
    def get_nav(self, code: str) -> Optional[NavQuote]:
        ...


@runtime_checkable
class FundHistoryProvider(Protocol):
    def get_bars(self, code: str, timeframe: str, limit: int) -> list[Bar]: ...


def _default_http_get(url: str, timeout: float) -> Optional[str]:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "hermes-finance/0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        return resp.read().decode("utf-8", errors="replace")


def _default_history_get(url: str, timeout: float) -> dict:
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 HermesFinance/1.0",
            "Referer": "https://fundf10.eastmoney.com/",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — fixed host
        return json.loads(resp.read().decode("utf-8", errors="replace"))


class EastmoneyFundHistory:
    """Confirmed daily NAV history for bare six-digit Chinese OTC funds.

    OTC funds do not have exchange candles or intraday volume.  Each daily NAV
    therefore becomes one flat OHLC bar; weekly/monthly bars are honest
    aggregates of those daily observations.  Intraday requests degrade to the
    same daily series instead of fabricating minute prices.
    """

    _SUPPORTED = {"1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"}

    def __init__(
        self,
        http_get: Optional[Callable[[str, float], dict]] = None,
        timeout: float = 6.0,
    ) -> None:
        self._get = http_get or _default_history_get
        self._timeout = timeout

    @staticmethod
    def _aggregate(rows: list[Bar], timeframe: str, limit: int) -> list[Bar]:
        if timeframe not in {"1wk", "1mo"}:
            return rows[-limit:]
        grouped: dict[tuple[int, int], list[Bar]] = {}
        for row in rows:
            local = row.ts.astimezone(timezone(timedelta(hours=8)))
            if timeframe == "1wk":
                iso = local.isocalendar()
                key = (iso.year, iso.week)
            else:
                key = (local.year, local.month)
            grouped.setdefault(key, []).append(row)
        out: list[Bar] = []
        for group in grouped.values():
            out.append(
                Bar(
                    symbol=group[0].symbol,
                    ts=group[-1].ts,
                    open=group[0].close,
                    high=max(item.close for item in group),
                    low=min(item.close for item in group),
                    close=group[-1].close,
                    volume=0.0,
                )
            )
        return out[-limit:]

    def get_bars(self, code: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if not is_fund_code(code):
            raise ValueError(f"not an OTC fund code: {code!r}")
        if timeframe not in self._SUPPORTED:
            raise ValueError(f"unsupported timeframe {timeframe!r}")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        base = code.strip()
        multiplier = 7 if timeframe == "1wk" else 31 if timeframe == "1mo" else 1
        page_size = min(1000, max(limit * multiplier, limit))
        url = f"{_FUND_HISTORY_URL}?{urlencode({'fundCode': base, 'pageIndex': 1, 'pageSize': page_size})}"
        try:
            payload = self._get(url, self._timeout)
            records = payload.get("Data", {}).get("LSJZList", [])
        except Exception as exc:  # noqa: BLE001 — normalize the provider boundary
            raise RuntimeError(f"fund history fetch failed for {base}: {exc}") from exc
        rows: list[Bar] = []
        china_tz = timezone(timedelta(hours=8))
        for record in reversed(records if isinstance(records, list) else []):
            if not isinstance(record, dict):
                continue
            try:
                close = float(record.get("DWJZ"))
                ts = datetime.strptime(str(record.get("FSRQ")), "%Y-%m-%d")
            except (TypeError, ValueError):
                continue
            if close <= 0:
                continue
            utc_ts = ts.replace(tzinfo=china_tz).astimezone(timezone.utc)
            rows.append(
                Bar(
                    symbol=base,
                    ts=utc_ts,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=0.0,
                )
            )
        if not rows:
            raise RuntimeError(f"no usable fund history for {base}")
        return self._aggregate(rows, timeframe, limit)


class EastmoneyFundNav:
    """Real-time OTC fund NAV estimate from 天天基金 (fundgz). Bare fund codes
    only; network via ``http_get`` (injectable); fail-None on anything."""

    def __init__(self, http_get: Optional[Callable[[str, float], Optional[str]]] = None,
                 timeout: float = 6.0) -> None:
        self._get = http_get or _default_http_get
        self._timeout = timeout

    def get_nav(self, code: str) -> Optional[NavQuote]:
        if not is_fund_code(code):  # check the ORIGINAL (a .SS suffix disqualifies)
            return None
        base = code.split(".")[0].strip()
        try:
            raw = self._get(_FUNDGZ_URL.format(code=base), self._timeout)
        except Exception as exc:  # noqa: BLE001 — network failure -> None
            logger.warning("fund nav fetch failed", extra={"code": base, "error": str(exc)[:160]})
            return None
        return _parse_fundgz(base, raw)


def _parse_fundgz(code: str, raw: Optional[str]) -> Optional[NavQuote]:
    if not raw:
        return None
    m = _JSONP.search(raw.strip())
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    # gsz = 实时估算净值 (preferred), dwjz = 最新单位净值 (fallback)
    gsz, dwjz = obj.get("gsz"), obj.get("dwjz")
    price_raw = gsz if gsz not in (None, "", "0") else dwjz
    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    source = "eastmoney-estimate" if price_raw is gsz else "eastmoney-nav"
    as_of = _parse_gztime(obj.get("gztime")) or datetime.now(timezone.utc)
    return NavQuote(symbol=code, price=price, name=str(obj.get("name", "")).strip(),
                    as_of=as_of, source=source)


def _parse_gztime(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:  # "2026-07-13 15:00" — Beijing time (UTC+8)
        dt = datetime.strptime(str(raw), "%Y-%m-%d %H:%M")
        return dt.replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class FakeNavProvider:
    """Deterministic offline provider for tests: {code: price} or {code: NavQuote}."""

    def __init__(self, data: dict) -> None:
        self._data = data

    def get_nav(self, code: str) -> Optional[NavQuote]:
        base = code.split(".")[0].strip()
        v = self._data.get(base)
        if v is None:
            return None
        if isinstance(v, NavQuote):
            return v
        return NavQuote(symbol=base, price=float(v), name="",
                        as_of=datetime(2026, 7, 13, tzinfo=timezone.utc),
                        source="eastmoney-estimate")


class CachedNavProvider:
    """TTL cache over a :class:`NavProvider`; still fail-None."""

    def __init__(self, inner: NavProvider, ttl_s: float = 900.0,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._inner = inner
        self._ttl = ttl_s
        self._clock = clock
        self._cache: dict[str, tuple[float, Optional[NavQuote]]] = {}

    def get_nav(self, code: str) -> Optional[NavQuote]:
        base = code.split(".")[0].strip()
        now = self._clock().timestamp()
        hit = self._cache.get(base)
        if hit is not None and (now - hit[0]) <= self._ttl:
            return hit[1]
        q = self._inner.get_nav(base)
        self._cache[base] = (now, q)
        return q
