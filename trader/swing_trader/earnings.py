"""Earnings / events calendar (Loop.md Phase 0.75 thrust A).

Fills the brief's explicit "earnings not wired" gap and makes the system aware
of a real landmine — opening a fresh position right into an earnings print. A
provider returns the NEXT scheduled earnings date per symbol; :func:`upcoming_earnings`
turns a watchlist into a sorted, dated list the brief and (later) the decision
core can reason about.

Design mirrors :mod:`swing_trader.fundamentals`: an injectable lookup so tests
never hit the network (Loop.md §3), a per-symbol TTL cache (dates move slowly),
and fail-None on any error. The yfinance date extraction is isolated behind an
injectable ``next_earnings_fn`` so the provider is decoupled from yfinance's
(shifting) DataFrame shapes and is trivially mockable.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Optional

from pydantic import BaseModel
from zoneinfo import ZoneInfo

from swing_trader.log import get_logger
from swing_trader.schemas import utcnow

logger = get_logger(__name__)

__all__ = ["EarningsEvent", "YFinanceEarnings", "upcoming_earnings", "ET_ZONE"]

ET_ZONE = ZoneInfo("America/New_York")

#: Earnings within this many days count as "imminent" (avoid fresh entries).
IMMINENT_DAYS: int = 5


class EarningsEvent(BaseModel):
    """One upcoming earnings date for a watchlist symbol."""

    symbol: str
    date: str  # YYYY-MM-DD
    days_until: int
    imminent: bool = False


def _yf_next_earnings(symbol: str) -> Optional[date]:
    """Next FUTURE earnings date via yfinance (lazy import); None on any trouble.

    Tries ``earnings_dates`` (a DatetimeIndex DataFrame) then ``calendar``.
    """
    import yfinance as yf

    ticker = yf.Ticker(symbol)
    # earnings_dates: DataFrame indexed by tz-aware datetimes (past + future).
    df = getattr(ticker, "earnings_dates", None)
    today = datetime.now(tz=ET_ZONE).date()
    candidates: list[date] = []
    try:
        if df is not None and getattr(df, "empty", True) is False:
            for idx in df.index:
                d = idx.date() if hasattr(idx, "date") else None
                if d is not None:
                    candidates.append(d)
    except Exception:  # noqa: BLE001
        pass
    if not candidates:
        try:
            cal = ticker.calendar
            raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
            if isinstance(raw, (list, tuple)):
                for x in raw:
                    d = x.date() if hasattr(x, "date") else x
                    if isinstance(d, date):
                        candidates.append(d)
            elif hasattr(raw, "date"):
                candidates.append(raw.date())
        except Exception:  # noqa: BLE001
            pass
    future = sorted(d for d in candidates if d >= today)
    return future[0] if future else None


class YFinanceEarnings:
    """Cached, mockable, fail-None earnings provider."""

    def __init__(
        self,
        next_earnings_fn: Optional[Callable[[str], Optional[date]]] = None,
        cache_ttl_hours: float = 12.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._fn = next_earnings_fn or _yf_next_earnings
        self._ttl = timedelta(hours=cache_ttl_hours)
        self._clock = clock
        self._cache: dict[str, tuple[datetime, Optional[date]]] = {}

    def get_next_earnings(self, symbol: str) -> Optional[date]:
        norm = symbol.strip().upper()
        now = self._clock()
        cached = self._cache.get(norm)
        if cached is not None and (now - cached[0]) < self._ttl:
            return cached[1]
        try:
            result = self._fn(symbol)
        except Exception as exc:  # noqa: BLE001 — fail-None, never raise
            logger.warning("earnings fetch failed",
                           extra={"symbol": symbol, "error": str(exc)[:200]})
            result = None
        if not isinstance(result, date):
            result = None
        self._cache[norm] = (now, result)
        return result


def upcoming_earnings(
    provider,
    symbols: list[str],
    *,
    now: Optional[datetime] = None,
    within_days: Optional[int] = None,
) -> list[EarningsEvent]:
    """Next-earnings events for ``symbols``, future-only, sorted by date.

    ``within_days`` limits to events at most that many days out (None = all
    future). Never raises: a provider failure for one symbol just drops it.
    """
    now = now or utcnow()
    today = now.astimezone(ET_ZONE).date()
    events: list[EarningsEvent] = []
    for symbol in symbols:
        try:
            d = provider.get_next_earnings(symbol)
        except Exception:  # noqa: BLE001
            d = None
        if not isinstance(d, date) or d < today:
            continue
        days = (d - today).days
        if within_days is not None and days > within_days:
            continue
        events.append(EarningsEvent(
            symbol=symbol.strip().upper(),
            date=d.isoformat(),
            days_until=days,
            imminent=days <= IMMINENT_DAYS,
        ))
    events.sort(key=lambda e: (e.days_until, e.symbol))
    return events
