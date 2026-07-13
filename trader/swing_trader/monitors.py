"""Monitors — scheduled pollers (Loop.md §5.2).

Each monitor polls injected ports (:class:`~swing_trader.interfaces.DataFeed`,
:class:`~swing_trader.interfaces.BrokerInterface`) and returns a timestamped
pydantic snapshot; snapshots can be persisted through a :class:`SnapshotSink`
(Loop.md §5.2: "each persists timestamped snapshots"). All dependencies are
injected so tests never touch the network (Loop.md §3).

Components
----------
- :class:`MarketMonitor` — indices, VIX, breadth, risk-on/off regime.
- :class:`PortfolioMonitor` — holdings + watchlist state (Loop.md §11);
  RE-TAGS broker positions with their watchlist role because the RiskEngine
  computes pool exposure from ``Position.pool`` (brokers default every
  position to ROTATION). Also builds the per-symbol
  :class:`~swing_trader.risk.LiquidityInfo` that feeds
  :meth:`~swing_trader.risk.RiskEngine.evaluate`.
- :class:`NewsMonitor` — headlines + deterministic keyword sentiment scoring.
- :class:`AccountRiskMonitor` — equity/P&L/drawdown/exposure and the daily
  drawdown circuit breaker (Loop.md §3): brokers always report
  ``breaker_state=NORMAL``; THIS monitor is the component that trips it.

Timestamps are timezone-aware UTC throughout.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Optional, Protocol, Sequence, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from swing_trader import watchlist
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, BrokerInterface, DataFeed
from swing_trader.log import get_logger
from swing_trader.risk import LiquidityInfo, RiskParams
from swing_trader.schemas import AccountSnapshot, BreakerState, Position, Role, utcnow

__all__ = [
    "AccountRiskMonitor",
    "JsonlSink",
    "MarketMonitor",
    "MarketSnapshot",
    "NewsMonitor",
    "NewsSnapshot",
    "PortfolioMonitor",
    "PortfolioSnapshot",
    "RiskStatus",
    "SnapshotSink",
    "WatchState",
    "score_headline",
]

logger = get_logger(__name__)

# ----------------------------------------------------------------- constants

#: VIX quote symbol on Yahoo Finance.
VIX_SYMBOL = "^VIX"

#: Regime thresholds (Loop.md §5.2 MarketMonitor: risk-on/off).
RISK_ON_VIX_MAX = 20.0
RISK_OFF_VIX_MIN = 28.0

#: Reference indices polled by default (Loop.md §11.A).
DEFAULT_INDEX_SYMBOLS: tuple[str, ...] = ("SPY", "QQQ", "DIA")

#: Bars fetched per watch/breadth symbol (enough for SMA50 / ATR14 / ADV20).
WATCH_BARS_LIMIT = 60

#: Bars fetched per index (enough for the 200dma risk-off check).
INDEX_BARS_LIMIT = 200

#: per_symbol_sentiment key for market-wide items (NewsItem.symbol is None).
MARKET_SENTIMENT_KEY = "MARKET"

#: Cash-buffer warning floor: warn when cash < this fraction of equity.
MIN_CASH_FRACTION = 0.10

RiskRegime = Literal["risk_on", "neutral", "risk_off"]


# ---------------------------------------------------------------- sentiment

_WORD_RE = re.compile(r"[a-z]+(?:'[a-z]+)?")

POSITIVE_WORDS: frozenset[str] = frozenset(
    {
        "beat", "beats", "surge", "surges", "surged", "soar", "soars",
        "soared", "upgrade", "upgrades", "upgraded", "record", "strong",
        "raise", "raises", "raised", "rally", "rallies", "jump", "jumps",
        "jumped", "gain", "gains", "tops", "outperform", "outperforms",
        "bullish", "growth",
    }
)

NEGATIVE_WORDS: frozenset[str] = frozenset(
    {
        "miss", "misses", "missed", "plunge", "plunges", "plunged",
        "downgrade", "downgrades", "downgraded", "cut", "cuts", "weak",
        "probe", "recall", "recalls", "lawsuit", "fall", "falls", "fell",
        "drop", "drops", "dropped", "slump", "slumps", "bearish", "warns",
        "warning", "fraud",
    }
)


def score_headline(text: str) -> float:
    """Deterministic keyword sentiment score in ``[-1, 1]``.

    ``score = (pos - neg) / max(1, pos + neg)`` over whole-word matches
    against small positive/negative word lists; no keywords -> 0.0.
    """
    words = _WORD_RE.findall(text.lower())
    pos = sum(1 for w in words if w in POSITIVE_WORDS)
    neg = sum(1 for w in words if w in NEGATIVE_WORDS)
    return (pos - neg) / max(1, pos + neg)


# ------------------------------------------------------------------- sinks


@runtime_checkable
class SnapshotSink(Protocol):
    """Persistence port for monitor snapshots (Loop.md §5.2)."""

    def write(self, kind: str, payload: dict) -> None: ...


class JsonlSink:
    """Append one JSON line per snapshot to ``<dir>/<kind>-YYYYMMDD.jsonl``.

    The date suffix comes from the snapshot's ``ts`` (UTC), so a day's
    snapshots of one kind land in one file.
    """

    def __init__(self, directory: Path | str) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, payload: dict) -> None:
        day = self._payload_ts_utc(payload)
        path = self._dir / f"{kind}-{day:%Y%m%d}.jsonl"
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    @staticmethod
    def _payload_ts_utc(payload: dict) -> datetime:
        """Snapshot ``ts`` as tz-aware UTC; falls back to now if unusable."""
        ts = payload.get("ts")
        if isinstance(ts, datetime):
            dt = ts
        elif isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except ValueError:
                dt = utcnow()
        else:
            dt = utcnow()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)


# --------------------------------------------------------------- snapshots


class _Snapshot(BaseModel):
    """Base snapshot with tz-aware UTC timestamp enforcement."""

    model_config = ConfigDict(validate_assignment=True)

    ts: datetime = Field(default_factory=utcnow)

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (use UTC)")
        return v


class MarketSnapshot(_Snapshot):
    """MarketMonitor output (Loop.md §5.2: indices, VIX, breadth, regime)."""

    indices: dict[str, dict[str, Optional[float]]] = Field(default_factory=dict)
    vix: Optional[float] = None
    breadth_pct_above_50dma: float = 0.0
    risk_on_off: RiskRegime = "neutral"


class WatchState(BaseModel):
    """Per-symbol technical/liquidity state for the watch universe."""

    model_config = ConfigDict(validate_assignment=True)

    last: float
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    atr_pct: Optional[float] = None
    avg_dollar_volume: float = 0.0


class PortfolioSnapshot(_Snapshot):
    """PortfolioMonitor output: retagged positions, exposure, watch state."""

    positions: list[Position] = Field(default_factory=list)
    pool_exposure_pct: dict[Role, float] = Field(default_factory=dict)
    watch: dict[str, WatchState] = Field(default_factory=dict)


class NewsSnapshot(_Snapshot):
    """NewsMonitor output: scored items + mean sentiment per symbol."""

    items: list[dict] = Field(default_factory=list)
    per_symbol_sentiment: dict[str, float] = Field(default_factory=dict)


class RiskStatus(_Snapshot):
    """AccountRiskMonitor output (Loop.md §5.2: equity, drawdown, breaker)."""

    snapshot: AccountSnapshot
    per_pool_exposure_pct: dict[Role, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------- helpers


def _sma(closes: Sequence[float], period: int) -> Optional[float]:
    """Simple moving average over the last ``period`` closes.

    Uses however many closes are available when fewer than ``period``
    (graceful degradation for short histories); None when empty.
    """
    if not closes:
        return None
    window = closes[-period:]
    return sum(window) / len(window)


def _atr_pct(bars: Sequence[Bar], period: int = 14) -> Optional[float]:
    """ATR (mean true range over the last ``period`` TRs) as % of last close.

    TR = max(high-low, |high-prev_close|, |low-prev_close|). Needs at least
    two bars (one prev close); None otherwise.
    """
    if len(bars) < 2:
        return None
    trs: list[float] = []
    for prev, cur in zip(bars[:-1], bars[1:]):
        tr = max(
            cur.high - cur.low,
            abs(cur.high - prev.close),
            abs(cur.low - prev.close),
        )
        trs.append(tr)
    window = trs[-period:]
    atr = sum(window) / len(window)
    last_close = bars[-1].close
    if last_close <= 0:
        return None
    return atr / last_close * 100.0


def _avg_dollar_volume(bars: Sequence[Bar], period: int = 20) -> float:
    """ADV = mean(close * volume) over the last ``period`` bars."""
    if not bars:
        return 0.0
    window = bars[-period:]
    return sum(b.close * b.volume for b in window) / len(window)


def _retag_positions(positions: Sequence[Position]) -> list[Position]:
    """Re-tag ``Position.pool`` from the watchlist role (Loop.md §11).

    Brokers default every position to ROTATION; the RiskEngine computes pool
    exposure from ``Position.pool``, so this retag is REQUIRED for role caps
    to bind correctly. Unknown symbols fall back to ROTATION. Returns copies;
    the broker's objects are never mutated.
    """
    retagged: list[Position] = []
    for pos in positions:
        item = watchlist.get(pos.symbol)
        role = item.role if item is not None else Role.ROTATION
        retagged.append(pos.model_copy(update={"pool": role}))
    return retagged


def _pool_exposure_pct(positions: Sequence[Position], equity: float) -> dict[Role, float]:
    """Per-pool exposure as % of equity (cost basis when no market price)."""
    totals: dict[Role, float] = {}
    for pos in positions:
        value = pos.market_value
        if value is None:
            value = pos.avg_px * pos.qty
        totals[pos.pool] = totals.get(pos.pool, 0.0) + value
    if equity <= 0:
        return {role: 0.0 for role in totals}
    return {role: value / equity * 100.0 for role, value in totals.items()}


# ---------------------------------------------------------------- monitors


class _BaseMonitor:
    """Shared sink plumbing: subclasses call :meth:`_persist` after polling.

    ``clock`` stamps every snapshot's ``ts``. It defaults to :func:`utcnow`
    (real wall clock — unchanged in production) but is injectable so the
    simulator/backtester stamps with its own clock; this keeps snapshot
    freshness consistent with the loop's clock, which the Phase 0.8 health
    model reads to decide whether new entries are still trustworthy.
    """

    kind: str = "snapshot"

    def __init__(
        self,
        sink: SnapshotSink | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sink = sink
        self._clock = clock

    def _persist(self, snapshot: _Snapshot) -> None:
        if self._sink is not None:
            self._sink.write(self.kind, snapshot.model_dump(mode="json"))


class MarketMonitor(_BaseMonitor):
    """Indices, VIX, breadth, and risk-on/off regime (Loop.md §5.2).

    Regime rules (risk-off wins on conflict — safety first):

    - ``risk_off`` when VIX > 28 OR SPY < its 200dma (falls back to the
      50dma when fewer than 200 bars are available);
    - ``risk_on`` when SPY > its 50dma AND VIX < 20;
    - ``neutral`` otherwise (including when SPY/VIX data is unavailable).
    """

    kind = "market"

    def __init__(
        self,
        feed: DataFeed,
        index_symbols: Sequence[str] = DEFAULT_INDEX_SYMBOLS,
        breadth_symbols: Sequence[str] | None = None,
        sink: SnapshotSink | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(sink, clock)
        self._feed = feed
        self._index_symbols = list(index_symbols)
        self._breadth_symbols = (
            list(breadth_symbols)
            if breadth_symbols is not None
            else watchlist.enabled_symbols()
        )

    def poll(self) -> MarketSnapshot:
        ts = self._clock()
        indices: dict[str, dict[str, Optional[float]]] = {}
        spy_last: Optional[float] = None
        spy_sma50: Optional[float] = None
        spy_long_sma: Optional[float] = None  # 200dma, or 50dma fallback

        for symbol in self._index_symbols:
            try:
                bars = self._feed.get_bars(symbol, "1d", limit=INDEX_BARS_LIMIT)
            except DataFeedError as exc:
                logger.warning(
                    "index bars unavailable, skipping",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                continue
            closes = [b.close for b in bars]
            if not closes:
                continue
            last = closes[-1]
            sma50 = _sma(closes, 50)
            dist = (
                (last - sma50) / sma50 * 100.0 if sma50 not in (None, 0.0) else None
            )
            indices[symbol] = {"last": last, "sma50_dist_pct": dist}
            if symbol == "SPY":
                spy_last = last
                spy_sma50 = sma50
                sma200 = _sma(closes, 200) if len(closes) >= 200 else None
                spy_long_sma = sma200 if sma200 is not None else sma50

        vix: Optional[float] = None
        try:
            vix = self._feed.get_quote(VIX_SYMBOL).last
        except DataFeedError as exc:
            logger.warning("VIX quote unavailable", extra={"error": str(exc)})

        breadth = self._breadth_pct_above_50dma()
        regime = self._regime(spy_last, spy_sma50, spy_long_sma, vix)

        snapshot = MarketSnapshot(
            ts=ts,
            indices=indices,
            vix=vix,
            breadth_pct_above_50dma=breadth,
            risk_on_off=regime,
        )
        self._persist(snapshot)
        logger.info(
            "market snapshot",
            extra={"risk_on_off": regime, "vix": vix, "breadth": breadth},
        )
        return snapshot

    def _breadth_pct_above_50dma(self) -> float:
        """% of enabled watchlist symbols closing above their 50dma.

        Symbols whose bars fail with DataFeedError are skipped (Loop.md §5.2);
        the percentage is over the symbols actually considered.
        """
        above = 0
        considered = 0
        for symbol in self._breadth_symbols:
            try:
                bars = self._feed.get_bars(symbol, "1d", limit=WATCH_BARS_LIMIT)
            except DataFeedError:
                logger.debug("breadth: skipping symbol", extra={"symbol": symbol})
                continue
            closes = [b.close for b in bars]
            sma50 = _sma(closes, 50)
            if sma50 is None:
                continue
            considered += 1
            if closes[-1] > sma50:
                above += 1
        if considered == 0:
            return 0.0
        return above / considered * 100.0

    @staticmethod
    def _regime(
        spy_last: Optional[float],
        spy_sma50: Optional[float],
        spy_long_sma: Optional[float],
        vix: Optional[float],
    ) -> RiskRegime:
        risk_off = (vix is not None and vix > RISK_OFF_VIX_MIN) or (
            spy_last is not None
            and spy_long_sma is not None
            and spy_last < spy_long_sma
        )
        if risk_off:
            return "risk_off"
        risk_on = (
            spy_last is not None
            and spy_sma50 is not None
            and spy_last > spy_sma50
            and vix is not None
            and vix < RISK_ON_VIX_MAX
        )
        if risk_on:
            return "risk_on"
        return "neutral"


class PortfolioMonitor(_BaseMonitor):
    """Holdings + watchlist state (Loop.md §5.2, §11).

    Broker positions are RE-TAGGED with their watchlist role (fallback
    ROTATION) because the RiskEngine computes pool exposure from
    ``Position.pool``. :meth:`liquidity_for` exposes the per-symbol
    :class:`~swing_trader.risk.LiquidityInfo` built from the latest poll,
    feeding :meth:`~swing_trader.risk.RiskEngine.evaluate` (which treats
    None as "no data, no trade").
    """

    kind = "portfolio"

    def __init__(
        self,
        feed: DataFeed,
        broker: BrokerInterface,
        symbols: Sequence[str] | None = None,
        sink: SnapshotSink | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(sink, clock)
        self._feed = feed
        self._broker = broker
        self._symbols = (
            list(symbols) if symbols is not None else watchlist.enabled_symbols()
        )
        self._last_watch: dict[str, WatchState] = {}

    def poll(self) -> PortfolioSnapshot:
        ts = self._clock()
        account = self._broker.get_account()
        positions = _retag_positions(self._broker.get_positions())
        exposure = _pool_exposure_pct(positions, account.equity)

        watch: dict[str, WatchState] = {}
        for symbol in self._symbols:
            try:
                bars = self._feed.get_bars(symbol, "1d", limit=WATCH_BARS_LIMIT)
            except DataFeedError as exc:
                logger.warning(
                    "watch bars unavailable, skipping",
                    extra={"symbol": symbol, "error": str(exc)},
                )
                continue
            if not bars:
                continue
            closes = [b.close for b in bars]
            watch[symbol] = WatchState(
                last=closes[-1],
                sma20=_sma(closes, 20),
                sma50=_sma(closes, 50),
                atr_pct=_atr_pct(bars),
                avg_dollar_volume=_avg_dollar_volume(bars),
            )
        self._last_watch = watch

        snapshot = PortfolioSnapshot(
            ts=ts,
            positions=positions,
            pool_exposure_pct=exposure,
            watch=watch,
        )
        self._persist(snapshot)
        logger.info(
            "portfolio snapshot",
            extra={"positions": len(positions), "watched": len(watch)},
        )
        return snapshot

    def liquidity_for(self, symbol: str) -> LiquidityInfo | None:
        """LiquidityInfo from the latest poll; None when unknown.

        None makes the RiskEngine veto the entry ("no data, no trade"),
        which is the conservative default we want.
        """
        state = self._last_watch.get(symbol.strip().upper())
        if state is None:
            return None
        return LiquidityInfo(
            avg_dollar_volume=state.avg_dollar_volume,
            atr_pct=state.atr_pct,
        )


class NewsMonitor(_BaseMonitor):
    """Headlines + deterministic keyword sentiment (Loop.md §5.2)."""

    kind = "news"

    def __init__(self, feed: DataFeed, sink: SnapshotSink | None = None,
                 clock: Callable[[], datetime] = utcnow) -> None:
        super().__init__(sink, clock)
        self._feed = feed

    def poll(self, symbols: list[str] | None = None) -> NewsSnapshot:
        """Fetch news (market-wide when ``symbols`` is None), score, snapshot.

        Symbols whose news fetch fails with DataFeedError are skipped.
        """
        ts = self._clock()
        collected = []
        if symbols is None:
            try:
                collected = list(self._feed.get_news(None))
            except DataFeedError as exc:
                logger.warning("market news unavailable", extra={"error": str(exc)})
        else:
            for symbol in symbols:
                try:
                    collected.extend(self._feed.get_news(symbol))
                except DataFeedError as exc:
                    logger.warning(
                        "news unavailable, skipping symbol",
                        extra={"symbol": symbol, "error": str(exc)},
                    )

        items: list[dict] = []
        sentiments: dict[str, list[float]] = {}
        for item in collected:
            score = score_headline(item.headline)
            scored = replace(item, sentiment=score)
            payload = asdict(scored)
            payload["ts"] = scored.ts.astimezone(timezone.utc).isoformat()
            items.append(payload)
            key = scored.symbol if scored.symbol else MARKET_SENTIMENT_KEY
            sentiments.setdefault(key, []).append(score)

        per_symbol = {
            key: sum(scores) / len(scores) for key, scores in sentiments.items()
        }
        snapshot = NewsSnapshot(ts=ts, items=items, per_symbol_sentiment=per_symbol)
        self._persist(snapshot)
        logger.info("news snapshot", extra={"items": len(items)})
        return snapshot

    def get_earnings_calendar(self) -> list[dict]:
        """Upcoming earnings events for the watch universe.

        TODO(Phase 0.5): wire a free earnings-calendar source (e.g. Yahoo
        earnings dates via yfinance) behind the DataFeed port; Phase 0
        returns an empty calendar.
        """
        return []


class AccountRiskMonitor(_BaseMonitor):
    """Equity, P&L, drawdown, per-pool exposure, breaker status (Loop.md §5.2).

    THIS is the component that trips the daily drawdown circuit breaker
    (Loop.md §3: −4% halts new entries): brokers always report
    ``breaker_state=NORMAL``; when the reported drawdown is at/below
    ``params.effective_breaker_pct`` (hard-cap clamped — params can only
    tighten it) the snapshot is re-emitted with ``breaker_state=TRIPPED``.
    """

    kind = "risk"

    def __init__(
        self,
        broker: BrokerInterface,
        params: RiskParams | None = None,
        sink: SnapshotSink | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(sink, clock)
        self._broker = broker
        self._params = params if params is not None else RiskParams()

    def poll(self) -> RiskStatus:
        ts = self._clock()
        account = self._broker.get_account()
        breaker = self._params.effective_breaker_pct
        if account.drawdown_pct <= breaker:
            account = account.model_copy(
                update={"breaker_state": BreakerState.TRIPPED}
            )
            logger.warning(
                "daily drawdown breaker TRIPPED",
                extra={"drawdown_pct": account.drawdown_pct, "breaker_pct": breaker},
            )

        positions = _retag_positions(self._broker.get_positions())
        exposure = _pool_exposure_pct(positions, account.equity)

        warnings: list[str] = []
        for role, pct in exposure.items():
            cap = float(self._params.role_caps.get(role, 0.0))
            if pct > cap:
                warnings.append(
                    f"{role.value} pool exposure {pct:.1f}% exceeds its "
                    f"{cap:.1f}% cap"
                )
        if account.equity > 0 and account.cash < MIN_CASH_FRACTION * account.equity:
            warnings.append(
                f"cash {account.cash:.2f} below "
                f"{MIN_CASH_FRACTION:.0%} of equity {account.equity:.2f}"
            )

        status = RiskStatus(
            ts=ts,
            snapshot=account,
            per_pool_exposure_pct=exposure,
            warnings=warnings,
        )
        self._persist(status)
        logger.info(
            "risk status",
            extra={
                "breaker_state": account.breaker_state.value,
                "drawdown_pct": account.drawdown_pct,
                "warnings": len(warnings),
            },
        )
        return status
