"""DataFeed adapters (Loop.md §5.1, §8).

Data-source policy (Loop.md §8): start free — ``yfinance`` for quotes, bars,
and basic news — and keep any paid feed (Polygon / Alpaca data / IBKR) behind
the :class:`~swing_trader.interfaces.DataFeed` interface as a stub so it can
be swapped in later without touching the core.

``yfinance`` is imported lazily (never at module import time) and the ticker
constructor is injectable, so tests are fully deterministic and never touch
the network (Loop.md §3).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from urllib.parse import quote as urlquote, urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.log import get_logger
from swing_trader.schemas import utcnow

logger = get_logger(__name__)

__all__ = ["DataFeedError", "FundAwareFeed", "RetryingFeed", "StubPaidFeed", "YFinanceFeed"]

#: Ticker used for market-wide news when no symbol is given (Loop.md §11.A).
MARKET_PROXY_SYMBOL = "SPY"

#: Supported DataFeed timeframes -> yfinance ``interval`` strings. Intraday
#: (1m/5m/15m/30m/1h) power the "1 day"/"5 day" chart presets; 1d/1wk/1mo power
#: the "day"/"week"/"month" presets (Loop.md Phase 0.75 chart iteration).
_TIMEFRAME_TO_INTERVAL: dict[str, str] = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "1d": "1d", "1wk": "1wk", "1mo": "1mo",
}

# Period ladders: smallest yfinance ``period`` that comfortably covers
# ``limit`` bars (thresholds ~80% of the bars a period typically yields:
# ~21 trading days/month, ~78 5m-bars/session, ~7 hourly bars/session,
# 52 weeks/year, 12 months/year). Yahoo caps intraday history: 1m -> ~7d,
# 5m/15m/30m -> ~60d, 1h -> ~730d.
_PERIOD_LADDERS: dict[str, tuple[tuple[int, str], ...]] = {
    "1m": ((390, "1d"), (1950, "5d"),),
    "5m": ((78, "1d"), (390, "5d"), (1560, "1mo")),
    "15m": ((26, "1d"), (130, "5d"), (520, "1mo")),
    "30m": ((13, "1d"), (65, "5d"), (260, "1mo")),
    "1h": ((117, "1mo"), (352, "3mo"), (705, "6mo"), (1411, "1y")),
    "1d": ((50, "3mo"), (100, "6mo"), (200, "1y"), (400, "2y"), (1000, "5y"), (2000, "10y")),
    "1wk": ((41, "1y"), (83, "2y"), (208, "5y"), (416, "10y")),
    "1mo": ((12, "1y"), (24, "2y"), (60, "5y"), (120, "10y")),
}
_PERIOD_FALLBACK: dict[str, str] = {
    "1m": "7d", "5m": "60d", "15m": "60d", "30m": "60d",
    "1h": "2y", "1d": "max", "1wk": "max", "1mo": "max",
}

_REQUIRED_BAR_COLUMNS = ("Open", "High", "Low", "Close")

_YAHOO_CHART_URL = "https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"


class DataFeedError(Exception):
    """Raised when a data source cannot provide the requested data."""


class FundAwareFeed(DataFeed):
    """Route bare Chinese OTC fund codes to a NAV-history provider.

    Yahoo is still authoritative for exchange instruments.  This adapter only
    intercepts symbols that are unambiguously bare six-digit fund codes, so an
    exchange ticker with ``.SS``/``.SZ`` continues through the regular feed.
    """

    def __init__(self, market_feed: DataFeed, fund_history: Any) -> None:
        self._market = market_feed
        self._fund_history = fund_history

    @staticmethod
    def _is_fund(symbol: str) -> bool:
        from swing_trader.fund_nav import is_fund_code

        return is_fund_code(symbol.strip().upper())

    def get_quote(self, symbol: str) -> Quote:
        if not self._is_fund(symbol):
            return self._market.get_quote(symbol)
        try:
            last = self._fund_history.get_bars(symbol.strip().upper(), "1d", 1)[-1]
        except Exception as exc:  # noqa: BLE001 — normalize provider boundary
            raise DataFeedError(f"fund NAV unavailable for {symbol!r}: {exc}") from exc
        return Quote(symbol=last.symbol, ts=last.ts, last=last.close)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if not self._is_fund(symbol):
            return self._market.get_bars(symbol, timeframe, limit)
        try:
            return self._fund_history.get_bars(symbol.strip().upper(), timeframe, limit)
        except ValueError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize provider boundary
            raise DataFeedError(f"fund NAV history unavailable for {symbol!r}: {exc}") from exc

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        if symbol and self._is_fund(symbol):
            return []
        return self._market.get_news(symbol, limit)


# --------------------------------------------------------------------------- helpers


def _period_for(timeframe: str, limit: int) -> str:
    """Pick a yfinance ``period`` large enough to yield ``limit`` bars."""
    for max_bars, period in _PERIOD_LADDERS[timeframe]:
        if limit <= max_bars:
            return period
    return _PERIOD_FALLBACK[timeframe]


def _to_utc(ts: Any) -> datetime:
    """Normalize a pandas Timestamp / datetime to a tz-aware UTC datetime.

    Naive timestamps are ASSUMED to be UTC and localized; aware timestamps
    are converted.
    """
    if getattr(ts, "tzinfo", None) is None:
        if hasattr(ts, "tz_localize"):  # pandas.Timestamp
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.replace(tzinfo=timezone.utc)
    elif hasattr(ts, "tz_convert"):  # pandas.Timestamp
        ts = ts.tz_convert("UTC")
    else:
        ts = ts.astimezone(timezone.utc)
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    return ts


def _as_float(value: Any) -> Optional[float]:
    """Coerce to a finite float; None for missing/NaN/garbage."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    return f


def _fast_info_value(fast_info: Any, *names: str) -> Optional[float]:
    """Read a numeric field from yfinance ``fast_info`` defensively.

    Supports both attribute access (``fi.last_price``) and mapping access
    (``fi["lastPrice"]``); any candidate name may be absent or raise.
    """
    for name in names:
        try:
            value = getattr(fast_info, name)
        except Exception:
            value = None
        if value is None:
            try:
                value = fast_info[name]
            except Exception:
                value = None
        f = _as_float(value)
        if f is not None:
            return f
    return None


def _default_chart_fetcher(symbol: str, period: str, interval: str) -> dict:
    """Fetch Yahoo's public chart JSON as a bounded yfinance fallback.

    yfinance normally uses Yahoo's query1/crumb flow.  That path can be
    throttled independently while query2's chart endpoint remains available;
    keeping this small fallback in the same adapter prevents every K-line from
    hanging behind repeated yfinance failures.
    """
    query = urlencode(
        {
            "range": period,
            "interval": interval,
            "includePrePost": "false",
            "events": "div,splits",
        }
    )
    url = f"{_YAHOO_CHART_URL.format(symbol=urlquote(symbol, safe=''))}?{query}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 HermesFinance/1.0"})
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            with urlopen(request, timeout=5.0) as response:  # noqa: S310 — fixed Yahoo host
                return json.loads(response.read().decode("utf-8"))
        except HTTPError:
            # A real 4xx (notably an unsupported symbol) is deterministic; do
            # not turn it into a second outbound request.
            raise
        except Exception as exc:  # noqa: BLE001 — one short transport retry
            last_error = exc
    assert last_error is not None
    raise last_error


def _chart_result(payload: dict, symbol: str) -> dict:
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise DataFeedError(f"no Yahoo chart result for {symbol}") from exc
    if not isinstance(result, dict):
        raise DataFeedError(f"invalid Yahoo chart result for {symbol}")
    return result


def _chart_bars(payload: dict, symbol: str, limit: int) -> list[Bar]:
    result = _chart_result(payload, symbol)
    timestamps = result.get("timestamp") or []
    try:
        quote = result["indicators"]["quote"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise DataFeedError(f"Yahoo chart bars missing for {symbol}") from exc
    if not isinstance(quote, dict):
        raise DataFeedError(f"Yahoo chart bars invalid for {symbol}")

    fields = {name: quote.get(name) or [] for name in ("open", "high", "low", "close", "volume")}
    rows: list[Bar] = []
    for index, epoch in enumerate(timestamps):
        try:
            opened = _as_float(fields["open"][index])
            high = _as_float(fields["high"][index])
            low = _as_float(fields["low"][index])
            close = _as_float(fields["close"][index])
        except (IndexError, TypeError):
            continue
        if None in (opened, high, low, close):
            continue
        try:
            ts = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        volume = _as_float(fields["volume"][index]) if index < len(fields["volume"]) else None
        rows.append(
            Bar(
                symbol=symbol,
                ts=ts,
                open=opened,
                high=high,
                low=low,
                close=close,
                volume=volume or 0.0,
            )
        )
    if not rows:
        raise DataFeedError(f"no usable Yahoo chart bars for {symbol}")
    return rows[-limit:]


# --------------------------------------------------------------------------- yfinance


class YFinanceFeed(DataFeed):
    """Free Yahoo Finance adapter (Loop.md §5.1, §8).

    ``ticker_factory`` is injected in tests so nothing ever hits the network
    (Loop.md §3); the default lazily imports ``yfinance`` on first use — NOT
    at module import time.
    """

    def __init__(
        self,
        ticker_factory: Callable[[str], Any] | None = None,
        chart_fetcher: Callable[[str, str, str], dict] | None = None,
        prefer_chart: bool = False,
        chart_only: bool = False,
    ) -> None:
        self._ticker_factory = ticker_factory
        # Injected fake tickers remain completely offline in tests.  Production
        # enables the query2 fallback; tests can opt in with chart_fetcher.
        self._chart_fallback_enabled = ticker_factory is None or chart_fetcher is not None
        self._chart_fetcher = chart_fetcher or _default_chart_fetcher
        self._prefer_chart = prefer_chart or chart_fetcher is not None
        self._chart_only = chart_only

    def _direct_chart_bars(
        self, symbol: str, timeframe: str, limit: int
    ) -> list[Bar]:
        interval = _TIMEFRAME_TO_INTERVAL[timeframe]
        period = _period_for(timeframe, limit)
        return _chart_bars(self._chart_fetcher(symbol, period, interval), symbol, limit)

    def _fallback_bars(
        self, symbol: str, timeframe: str, limit: int, original: Exception
    ) -> list[Bar]:
        if not self._chart_fallback_enabled:
            raise original
        try:
            return self._direct_chart_bars(symbol, timeframe, limit)
        except Exception as exc:  # noqa: BLE001 — normalize the fallback boundary
            if isinstance(exc, DataFeedError):
                fallback_error = exc
            else:
                fallback_error = DataFeedError(f"Yahoo chart fallback failed for {symbol}: {exc}")
            raise DataFeedError(f"{original}; fallback: {fallback_error}") from exc

    def _ticker(self, symbol: str) -> Any:
        factory = self._ticker_factory
        if factory is None:
            import yfinance  # lazy: keep module import network-lib free

            factory = yfinance.Ticker
            self._ticker_factory = factory
        return factory(symbol)

    # ------------------------------------------------------------- quotes

    def get_quote(self, symbol: str) -> Quote:
        sym = symbol.strip().upper()
        # query2 is the fast, bounded production path.  It does not require
        # Yahoo's crumb/cookie cache (which can hang or be independently rate
        # limited in containers).  Injected unit-test tickers keep the original
        # yfinance-first behavior unless a chart_fetcher is explicitly supplied.
        if self._prefer_chart:
            try:
                last = self._direct_chart_bars(sym, "1d", 5)[-1]
                return Quote(symbol=sym, ts=last.ts, last=last.close)
            except Exception as exc:  # noqa: BLE001 — yfinance remains fallback
                if self._chart_only:
                    raise DataFeedError(
                        f"Yahoo chart quote path failed for {sym}: {exc}"
                    ) from exc
                logger.warning(
                    "Yahoo chart quote path failed; falling back to yfinance",
                    extra={"symbol": sym, "error": str(exc)[:160]},
                )
        ticker = self._ticker(sym)

        try:
            fast_info = ticker.fast_info
        except Exception:
            fast_info = None
        if fast_info is not None:
            last = _fast_info_value(fast_info, "last_price", "lastPrice")
            if last is not None and last > 0:
                return Quote(
                    symbol=sym,
                    ts=utcnow(),
                    last=last,
                    bid=_fast_info_value(fast_info, "bid"),
                    ask=_fast_info_value(fast_info, "ask"),
                )

        # Fallback: last daily close.
        logger.debug("fast_info unavailable, falling back to history", extra={"symbol": sym})
        try:
            df = ticker.history(period="5d", interval="1d")
        except Exception as exc:  # noqa: BLE001 — any upstream failure is a feed error
            original = DataFeedError(f"quote fallback history failed for {sym}: {exc}")
            bars = self._fallback_bars(sym, "1d", 5, original)
            last = bars[-1]
            return Quote(symbol=sym, ts=last.ts, last=last.close)
        if df is None or len(df) == 0 or "Close" not in df.columns:
            original = DataFeedError(f"no quote data available for {sym}")
            bars = self._fallback_bars(sym, "1d", 5, original)
            last = bars[-1]
            return Quote(symbol=sym, ts=last.ts, last=last.close)
        close = _as_float(df["Close"].iloc[-1])
        if close is None or close <= 0:
            raise DataFeedError(f"no usable close price for {sym}")
        return Quote(symbol=sym, ts=_to_utc(df.index[-1]), last=close)

    # ------------------------------------------------------------- bars

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if timeframe not in _TIMEFRAME_TO_INTERVAL:
            raise ValueError(
                f"unsupported timeframe {timeframe!r}; "
                f"supported: {sorted(_TIMEFRAME_TO_INTERVAL)}"
            )
        if limit < 1:
            raise ValueError(f"limit must be >= 1, got {limit}")

        sym = symbol.strip().upper()
        interval = _TIMEFRAME_TO_INTERVAL[timeframe]
        period = _period_for(timeframe, limit)
        if self._prefer_chart:
            try:
                return self._direct_chart_bars(sym, timeframe, limit)
            except Exception as exc:  # noqa: BLE001 — yfinance remains fallback
                if self._chart_only:
                    raise DataFeedError(
                        f"Yahoo chart bars path failed for {sym} ({timeframe}): {exc}"
                    ) from exc
                logger.warning(
                    "Yahoo chart bars path failed; falling back to yfinance",
                    extra={
                        "symbol": sym,
                        "timeframe": timeframe,
                        "error": str(exc)[:160],
                    },
                )
        ticker = self._ticker(sym)
        try:
            df = ticker.history(period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001
            original = DataFeedError(f"history failed for {sym} ({timeframe}): {exc}")
            return self._fallback_bars(sym, timeframe, limit, original)

        if df is None or len(df) == 0:
            original = DataFeedError(f"no bars returned for {sym} ({timeframe})")
            return self._fallback_bars(sym, timeframe, limit, original)
        missing = [c for c in _REQUIRED_BAR_COLUMNS if c not in df.columns]
        if missing:
            raise DataFeedError(f"bars for {sym} missing columns {missing}")

        df = df.dropna(subset=list(_REQUIRED_BAR_COLUMNS)).sort_index()
        if len(df) == 0:
            raise DataFeedError(f"no usable bars for {sym} ({timeframe})")
        df = df.iloc[-limit:]  # keep the LAST `limit` rows, ascending

        has_volume = "Volume" in df.columns
        bars: list[Bar] = []
        for idx, row in df.iterrows():
            volume = _as_float(row["Volume"]) if has_volume else None
            bars.append(
                Bar(
                    symbol=sym,
                    ts=_to_utc(idx),
                    open=float(row["Open"]),
                    high=float(row["High"]),
                    low=float(row["Low"]),
                    close=float(row["Close"]),
                    volume=volume if volume is not None else 0.0,
                )
            )
        return bars

    # ------------------------------------------------------------- news

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        sym = symbol.strip().upper() if symbol else None
        fetch_sym = sym if sym is not None else MARKET_PROXY_SYMBOL
        ticker = self._ticker(fetch_sym)
        try:
            raw = ticker.news
        except Exception as exc:  # noqa: BLE001
            raise DataFeedError(f"news fetch failed for {fetch_sym}: {exc}") from exc
        if not raw:
            return []

        items: list[NewsItem] = []
        for raw_item in raw:
            item = self._parse_news_item(raw_item, sym)
            if item is None:
                logger.debug("skipping malformed news item", extra={"symbol": fetch_sym})
                continue
            items.append(item)
            if len(items) >= limit:
                break
        return items

    @staticmethod
    def _parse_news_item(raw_item: Any, symbol: Optional[str]) -> Optional[NewsItem]:
        """Parse one yfinance news dict; None if malformed (skipped silently).

        Handles both formats:
        - old: ``{title, publisher, link, providerPublishTime(epoch seconds)}``
        - new: ``{content: {title, pubDate(ISO), provider: {displayName},
          canonicalUrl: {url}}}``
        """
        if not isinstance(raw_item, dict):
            return None
        content = raw_item.get("content")
        if isinstance(content, dict):
            title = content.get("title")
            pub_date = content.get("pubDate")
            if not title or not isinstance(title, str) or not pub_date:
                return None
            try:
                ts = datetime.fromisoformat(str(pub_date).replace("Z", "+00:00"))
            except ValueError:
                return None
            ts = ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)
            provider = content.get("provider")
            source = provider.get("displayName", "") if isinstance(provider, dict) else ""
            canonical = content.get("canonicalUrl")
            url = canonical.get("url", "") if isinstance(canonical, dict) else ""
            return NewsItem(
                symbol=symbol,
                ts=ts,
                headline=title,
                source=source or "",
                url=url or "",
                sentiment=None,
            )

        # Old flat format.
        title = raw_item.get("title")
        epoch = raw_item.get("providerPublishTime")
        if not title or not isinstance(title, str):
            return None
        if not isinstance(epoch, (int, float)) or isinstance(epoch, bool):
            return None
        try:
            ts = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return NewsItem(
            symbol=symbol,
            ts=ts,
            headline=title,
            source=raw_item.get("publisher") or "",
            url=raw_item.get("link") or "",
            sentiment=None,
        )


# --------------------------------------------------------------------------- paid stub


class StubPaidFeed(DataFeed):
    """Placeholder for a paid data feed (Loop.md §8).

    Kept behind the ``DataFeed`` interface so swapping it in later touches
    nothing in the core.
    """

    _TODO = (
        "TODO(Phase 1+): Polygon/Alpaca/IBKR paid feed goes here "
        "(Loop.md §8 data policy); Phase 0 uses YFinanceFeed"
    )

    def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError(self._TODO)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        raise NotImplementedError(self._TODO)

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        raise NotImplementedError(self._TODO)


class RetryingFeed(DataFeed):
    """Wrap a :class:`DataFeed` with bounded retry + exponential backoff on
    TRANSIENT :class:`DataFeedError` (Loop.md §5.1, Phase 0.8 resilience).

    A flaky network / rate-limit blip no longer drops a whole monitor cycle:
    the call is retried ``retries`` times with ``backoff_s * 2**attempt`` waits.
    ``ValueError`` (a programming error like a bad timeframe) is NOT retried.
    ``sleep`` is injectable so tests never actually wait. After the last attempt
    the final :class:`DataFeedError` is re-raised so callers still degrade
    exactly as before (fail closed) when the source is genuinely down.
    """

    def __init__(
        self,
        inner: DataFeed,
        retries: int = 2,
        backoff_s: float = 0.5,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._inner = inner
        self._retries = max(0, retries)
        self._backoff = max(0.0, backoff_s)
        self._sleep = sleep or time.sleep

    def _retry(self, what: str, fn: Callable, *args):
        last: Optional[DataFeedError] = None
        for attempt in range(self._retries + 1):
            try:
                return fn(*args)
            except DataFeedError as exc:
                last = exc
                if attempt < self._retries:
                    wait = self._backoff * (2 ** attempt)
                    logger.warning("feed call failed, retrying",
                                   extra={"call": what, "attempt": attempt + 1,
                                          "wait_s": wait, "error": str(exc)[:160]})
                    self._sleep(wait)
        assert last is not None  # loop runs at least once
        raise last

    def get_quote(self, symbol: str) -> Quote:
        return self._retry("get_quote", self._inner.get_quote, symbol)

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        return self._retry("get_bars", self._inner.get_bars, symbol, timeframe, limit)

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        return self._retry("get_news", self._inner.get_news, symbol, limit)
