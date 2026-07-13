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

from datetime import datetime, timezone
from typing import Any, Callable, Optional

from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.log import get_logger
from swing_trader.schemas import utcnow

logger = get_logger(__name__)

__all__ = ["DataFeedError", "StubPaidFeed", "YFinanceFeed"]

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


class DataFeedError(Exception):
    """Raised when a data source cannot provide the requested data."""


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


# --------------------------------------------------------------------------- yfinance


class YFinanceFeed(DataFeed):
    """Free Yahoo Finance adapter (Loop.md §5.1, §8).

    ``ticker_factory`` is injected in tests so nothing ever hits the network
    (Loop.md §3); the default lazily imports ``yfinance`` on first use — NOT
    at module import time.
    """

    def __init__(self, ticker_factory: Callable[[str], Any] | None = None) -> None:
        self._ticker_factory = ticker_factory

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
            raise DataFeedError(f"quote fallback history failed for {sym}: {exc}") from exc
        if df is None or len(df) == 0 or "Close" not in df.columns:
            raise DataFeedError(f"no quote data available for {sym}")
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
        ticker = self._ticker(sym)
        try:
            df = ticker.history(period=period, interval=interval)
        except Exception as exc:  # noqa: BLE001
            raise DataFeedError(f"history failed for {sym} ({timeframe}): {exc}") from exc

        if df is None or len(df) == 0:
            raise DataFeedError(f"no bars returned for {sym} ({timeframe})")
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
