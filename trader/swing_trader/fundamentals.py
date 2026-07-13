"""Real fundamentals via yfinance (Loop.md Phase 0.75 thrust A).

Implements the :class:`~swing_trader.analysis.FundamentalsProvider` port with
live-ish data from yfinance ``.info``: trailing/forward P/E, revenue growth,
gross/profit margins, earnings growth. Design mirrors
:class:`~swing_trader.datafeed.YFinanceFeed`:

- an **injectable ticker factory** so tests never hit the network (Loop.md §3);
- a **per-symbol cache** with a TTL — fundamentals move slowly, so the daily
  loop and on-demand ``/v1/analyze`` calls do not hammer Yahoo (negative
  results are cached too, so a bad symbol is not re-fetched every request);
- **fail-None**: ANY error (import, network, missing fields) yields ``None``
  ("no data, no signal") and never raises.

The provider returns the port's documented keys — ``pe``, ``fwd_pe``,
``rev_growth_pct``, ``gross_margin_pct`` — plus a few extra context fields
(``profit_margin_pct``, ``earnings_growth_pct``, ``market_cap``, ``name``)
that ride along in ``Signal.features_json`` for richer analysis/UI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from swing_trader.log import get_logger
from swing_trader.schemas import utcnow

logger = get_logger(__name__)

__all__ = ["YFinanceFundamentals"]

#: yfinance ``.info`` field -> (our metric key, scale). Ratios that yfinance
#: reports as fractions (0.22) are scaled to percent (22.0).
_FIELD_MAP: tuple[tuple[str, str, float], ...] = (
    ("trailingPE", "pe", 1.0),
    ("forwardPE", "fwd_pe", 1.0),
    ("revenueGrowth", "rev_growth_pct", 100.0),
    ("grossMargins", "gross_margin_pct", 100.0),
    ("profitMargins", "profit_margin_pct", 100.0),
    ("earningsGrowth", "earnings_growth_pct", 100.0),
    ("marketCap", "market_cap", 1.0),
)


def _default_ticker_factory(symbol: str):
    import yfinance as yf  # lazy: keeps yfinance optional + tests offline

    return yf.Ticker(symbol)


def _read_info(ticker: Any) -> Optional[dict]:
    """Read the yfinance info dict defensively (``get_info()`` or ``.info``)."""
    getter = getattr(ticker, "get_info", None)
    info = getter() if callable(getter) else getattr(ticker, "info", None)
    return info if isinstance(info, dict) else None


def _num(info: dict, key: str, scale: float) -> Optional[float]:
    value = info.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) * scale


class YFinanceFundamentals:
    """yfinance-backed :class:`FundamentalsProvider` (cached, mockable, fail-None)."""

    def __init__(
        self,
        ticker_factory: Optional[Callable[[str], Any]] = None,
        cache_ttl_hours: float = 24.0,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._ticker_factory = ticker_factory or _default_ticker_factory
        self._ttl = timedelta(hours=cache_ttl_hours)
        self._clock = clock
        # symbol -> (fetched_at, metrics|None); None caches a negative result.
        self._cache: dict[str, tuple[datetime, Optional[dict]]] = {}

    def get_metrics(self, symbol: str) -> Optional[dict]:
        norm = symbol.strip().upper()
        now = self._clock()
        cached = self._cache.get(norm)
        if cached is not None and (now - cached[0]) < self._ttl:
            return dict(cached[1]) if cached[1] is not None else None

        metrics = self._fetch(symbol)
        self._cache[norm] = (now, metrics)
        return dict(metrics) if metrics is not None else None

    def _fetch(self, symbol: str) -> Optional[dict]:
        try:
            info = _read_info(self._ticker_factory(symbol))
        except Exception as exc:  # noqa: BLE001 — fail-None, never raise
            logger.warning(
                "fundamentals fetch failed",
                extra={"symbol": symbol, "error": str(exc)[:200]},
            )
            return None
        if not info:
            return None
        metrics: dict[str, Any] = {}
        for field_name, key, scale in _FIELD_MAP:
            val = _num(info, field_name, scale)
            if val is not None:
                metrics[key] = val
        name = info.get("shortName") or info.get("longName")
        if isinstance(name, str) and name:
            metrics["name"] = name
        # No usable numeric metric -> treat as "no data" so the agent stays silent.
        if not any(k in metrics for k in ("pe", "fwd_pe", "rev_growth_pct")):
            return None
        return metrics
