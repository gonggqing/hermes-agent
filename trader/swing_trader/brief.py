"""Investment Research daily brief — model + builder (Loop.md §7 Phase 0.5).

Phase 0.5 makes Finance useful to a HUMAN READER every day before it becomes
a busy order console (Loop.md §7 Phase 0.5, §5.9 research-first information
architecture, §10 Phase-0.5 backlog item 2): research and risk awareness are
primary. The :class:`ResearchBrief` is the briefing contract the daily loop
and the Finance API expose to Desktop/Web:

- every brief carries as-of times, PAPER/LIVE mode, explicit data-freshness
  flags, provenance links, and an explicit ``uncertainty`` section — stale or
  unavailable data is an explicit warning, never silently presented as
  current (Loop.md §5.9);
- :func:`build_research_brief` NEVER raises for missing monitor snapshots:
  a degraded brief states which sources are missing and falls back to the
  ledger's last account snapshot where possible;
- signals/candidates are filtered to the current ET trading date
  (zoneinfo ``America/New_York``), and the builder is deterministic given
  its inputs (``now`` is injectable for tests).

All models are JSON-serializable via ``model_dump(mode="json")`` so the API
layer can return them unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional, TypeVar
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from swing_trader import watchlist
from swing_trader.ledger import Ledger, TradeStats
from swing_trader.log import get_logger
from swing_trader.monitors import (
    MarketSnapshot,
    NewsSnapshot,
    PortfolioSnapshot,
    RiskStatus,
    WatchState,
)
from swing_trader.schemas import (
    AiPhase,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Mode,
    Role,
    Signal,
    utcnow,
)

__all__ = [
    "CandidatesToday",
    "DATA_SOURCE_NOTE",
    "EARNINGS_NOT_WIRED_NOTE",
    "EventsView",
    "FreshnessInfo",
    "Mover",
    "MoversView",
    "NewsDigestItem",
    "NewsSection",
    "PendingCandidate",
    "ProvenanceLink",
    "RegimeView",
    "ResearchBrief",
    "RiskView",
    "SignalView",
    "STALE_AFTER_MINUTES",
    "ThemeView",
    "build_research_brief",
]

logger = get_logger(__name__)

# ----------------------------------------------------------------- constants

#: Data older than this is flagged stale (Loop.md §5.9: stale or unavailable
#: data is an explicit warning, never silently presented as current).
STALE_AFTER_MINUTES: float = 120.0

#: Movers shown per direction (top / bottom, ranked by distance to SMA20).
TOP_MOVERS: int = 5

#: News digest size (ranked by |sentiment|).
TOP_NEWS: int = 10

#: Leader symbols shown per theme.
THEME_LEADERS: int = 2

#: Signal thesis truncation length in the brief.
THESIS_MAX_CHARS: int = 200

#: ET trading calendar zone (Loop.md §5.5: all scheduling is ET-aware).
ET_ZONE = ZoneInfo("America/New_York")

#: source_agent used by the DebateAgent (bull-vs-bear synthesis, Loop.md §5.3).
DEBATE_AGENT: str = "debate"

#: Honest data-source note attached to every brief (Loop.md §8 data policy).
DATA_SOURCE_NOTE: str = "prices/news: Yahoo Finance via yfinance"
DATA_SOURCE_URL: str = "https://finance.yahoo.com"

#: Honest unknown: NewsMonitor.get_earnings_calendar is a Phase-0.5 TODO.
EARNINGS_NOT_WIRED_NOTE: str = (
    "earnings calendar feed is not wired yet (Phase 0.5 TODO) — "
    "no earnings events are shown"
)

#: Candidate statuses still awaiting action (shown under "pending").
PENDING_CANDIDATE_STATUSES: frozenset[CandidateStatus] = frozenset(
    {
        CandidateStatus.PROPOSED,
        CandidateStatus.RISK_APPROVED,
        CandidateStatus.PUSHED,
    }
)

_T = TypeVar("_T")


# -------------------------------------------------------------------- models


class FreshnessInfo(BaseModel):
    """Per-source as-of times, ages, and stale flags (Loop.md §5.9).

    A source is ``stale`` when its snapshot is missing entirely or older
    than :data:`STALE_AFTER_MINUTES`; every problem yields a human-readable
    warning sentence.
    """

    model_config = ConfigDict(validate_assignment=True)

    market_as_of: Optional[datetime] = None
    news_as_of: Optional[datetime] = None
    portfolio_as_of: Optional[datetime] = None
    market_age_minutes: Optional[float] = None
    news_age_minutes: Optional[float] = None
    portfolio_age_minutes: Optional[float] = None
    market_stale: bool = True
    news_stale: bool = True
    portfolio_stale: bool = True
    warnings: list[str] = Field(default_factory=list)


class RegimeView(BaseModel):
    """Dated market pulse: regime, VIX, breadth, indices (Loop.md §5.9 (1))."""

    risk_on_off: str
    vix: Optional[float] = None
    breadth_pct_above_50dma: float = 0.0
    indices: dict[str, dict[str, Optional[float]]] = Field(default_factory=dict)


class RiskView(BaseModel):
    """Account risk pulse with actionable warnings (Loop.md §5.9 (1))."""

    equity: float
    cash: float
    day_pnl: float
    drawdown_pct: float
    breaker_state: str
    pool_exposure_pct: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    #: {n_closed, win_rate, expectancy, max_drawdown_pct} from Ledger.stats.
    stats: dict[str, float] = Field(default_factory=dict)


def _region_of(symbol: str) -> str:
    """Market region from the yfinance symbol suffix (mirrors the API's split):
    ``.HK`` → HK, ``.SS`` / ``.SZ`` → CN mainland A-share, ``.KS`` → KR, else US."""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HK"
    if s.endswith((".SS", ".SZ")):
        return "CN"
    if s.endswith(".KS"):
        return "KR"
    return "US"


class Mover(BaseModel):
    """One watchlist symbol ranked by distance to its SMA20 (Loop.md §11)."""

    symbol: str
    last: float
    dist_sma20_pct: float
    dist_sma50_pct: Optional[float] = None
    theme: str
    ai_phase: str
    role: str
    #: Market region derived from the symbol suffix (CN mainland / HK / US / KR)
    #: so a CN brief's movers can be split into China vs Hong Kong. Optional +
    #: default so US briefs and existing consumers are unaffected.
    region: Optional[str] = None


class MoversView(BaseModel):
    """Top/bottom watchlist movers by ``dist_sma20_pct`` (5 each)."""

    top: list[Mover] = Field(default_factory=list)
    bottom: list[Mover] = Field(default_factory=list)


class ThemeView(BaseModel):
    """Theme aggregation over the watch universe (Loop.md §11/§12 rotation)."""

    theme: str
    avg_dist_sma50_pct: float
    n_symbols: int
    leaders: list[str] = Field(default_factory=list)


class NewsDigestItem(BaseModel):
    """One cited headline in the digest (top-|sentiment| selection)."""

    headline: str
    source: str = ""
    url: str = ""
    sentiment: Optional[float] = None
    symbol: Optional[str] = None


class NewsSection(BaseModel):
    """News digest: top items by |sentiment| + per-symbol mean sentiment."""

    items: list[NewsDigestItem] = Field(default_factory=list)
    per_symbol_sentiment: dict[str, float] = Field(default_factory=dict)


class SignalView(BaseModel):
    """Analysis/debate signal summary (thesis truncated for the brief)."""

    symbol: str
    direction: str
    confidence: float
    source_agent: str
    thesis: str


class EventsView(BaseModel):
    """Earnings/events section — honest about the unwired earnings feed."""

    earnings: list[dict] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class PendingCandidate(BaseModel):
    """Compact 'actions requiring attention' row (Loop.md §5.9 (4))."""

    symbol: str
    side: str
    qty: float
    confidence: float
    status: str


class CandidatesToday(BaseModel):
    """Today's candidate flow: status counts + still-pending items."""

    counts: dict[str, int] = Field(default_factory=dict)
    pending: list[PendingCandidate] = Field(default_factory=list)


class ProvenanceLink(BaseModel):
    """One citation: label + URL (Loop.md §5.9: every claim has a source)."""

    label: str
    url: str


class ResearchBrief(BaseModel):
    """The daily Investment Research brief (Loop.md §7 Phase 0.5 acceptance:
    as-of time, citations/unknowns, PAPER/LIVE mode, actionable warnings)."""

    model_config = ConfigDict(validate_assignment=True)

    as_of: datetime
    trading_date: str  # YYYY-MM-DD in America/New_York
    mode: Mode
    freshness: FreshnessInfo
    regime: Optional[RegimeView] = None
    risk: Optional[RiskView] = None
    movers: MoversView = Field(default_factory=MoversView)
    themes: list[ThemeView] = Field(default_factory=list)
    events: EventsView = Field(default_factory=EventsView)
    news: NewsSection = Field(default_factory=NewsSection)
    signals_today: list[SignalView] = Field(default_factory=list)
    candidates_today: CandidatesToday = Field(default_factory=CandidatesToday)
    uncertainty: list[str] = Field(default_factory=list)
    provenance: list[ProvenanceLink] = Field(default_factory=list)

    @field_validator("as_of")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("as_of must be timezone-aware (use UTC)")
        return v


# ------------------------------------------------------------------- helpers


def _coerce_now(now: Optional[datetime]) -> datetime:
    """Injectable clock: default UTC now; naive datetimes assumed UTC."""
    if now is None:
        return utcnow()
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _local_date(ts: datetime, tz: ZoneInfo = ET_ZONE) -> str:
    """Calendar date (YYYY-MM-DD) of a tz-aware timestamp in ``tz``.

    Defaults to ET (the US session); the CN research session passes
    Asia/Shanghai so "today's" signals are filtered on the CN trading date,
    not the ET date (which would be the previous day for a CN-morning run).
    """
    return ts.astimezone(tz).date().isoformat()


def _default_lookup(symbol: str):
    """Default watchlist metadata lookup (the US universe)."""
    return watchlist.get(symbol)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _dist_pct(last: float, sma: Optional[float]) -> Optional[float]:
    """Distance of last close from an SMA, in percent; None without SMA."""
    if sma is None or sma == 0.0:
        return None
    return (last - sma) / sma * 100.0


def _safe(
    label: str, fn: Callable[[], _T], default: _T, unknowns: list[str]
) -> _T:
    """Run a ledger accessor; on ANY failure degrade honestly, never raise.

    Loop.md §5.9: unavailable data is an explicit warning — the failure is
    logged and recorded as an uncertainty item, and ``default`` is used.
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 — brief must never raise
        logger.warning(
            "brief: %s unavailable — degrading", label, extra={"error": str(exc)}
        )
        unknowns.append(f"{label} unavailable ({exc}) — brief shown without it")
        return default


# ---------------------------------------------------------- section builders


def _source_freshness(
    name: str, as_of: Optional[datetime], now: datetime
) -> tuple[Optional[float], bool, Optional[str]]:
    """(age_minutes, stale, warning) for one monitor source."""
    if as_of is None:
        return (
            None,
            True,
            f"{name} data is missing — the {name} monitor has not produced "
            "a snapshot",
        )
    age = max(0.0, (now - as_of).total_seconds() / 60.0)
    if age > STALE_AFTER_MINUTES:
        return (
            age,
            True,
            f"{name} data is {age:.0f} minutes old (stale after "
            f"{STALE_AFTER_MINUTES:.0f} minutes) — treat as outdated",
        )
    return age, False, None


def _freshness(
    market: Optional[MarketSnapshot],
    news: Optional[NewsSnapshot],
    portfolio: Optional[PortfolioSnapshot],
    now: datetime,
) -> FreshnessInfo:
    warnings: list[str] = []
    ages: dict[str, Optional[float]] = {}
    stale: dict[str, bool] = {}
    as_ofs: dict[str, Optional[datetime]] = {
        "market": market.ts if market is not None else None,
        "news": news.ts if news is not None else None,
        "portfolio": portfolio.ts if portfolio is not None else None,
    }
    for name, as_of in as_ofs.items():
        age, is_stale, warning = _source_freshness(name, as_of, now)
        ages[name] = age
        stale[name] = is_stale
        if warning is not None:
            warnings.append(warning)
    return FreshnessInfo(
        market_as_of=as_ofs["market"],
        news_as_of=as_ofs["news"],
        portfolio_as_of=as_ofs["portfolio"],
        market_age_minutes=ages["market"],
        news_age_minutes=ages["news"],
        portfolio_age_minutes=ages["portfolio"],
        market_stale=stale["market"],
        news_stale=stale["news"],
        portfolio_stale=stale["portfolio"],
        warnings=warnings,
    )


def _regime_view(market: Optional[MarketSnapshot]) -> Optional[RegimeView]:
    if market is None:
        return None
    return RegimeView(
        risk_on_off=str(market.risk_on_off),
        vix=market.vix,
        breadth_pct_above_50dma=market.breadth_pct_above_50dma,
        indices={sym: dict(vals) for sym, vals in market.indices.items()},
    )


def _stats_dict(stats: TradeStats) -> dict[str, float]:
    return {
        "n_closed": float(stats.n_closed),
        "win_rate": stats.win_rate,
        "expectancy": stats.expectancy,
        "max_drawdown_pct": stats.max_drawdown_pct,
    }


_BREAKER_WARNING = (
    "daily drawdown circuit breaker is TRIPPED — no new entries today"
)


def _risk_view(
    risk_status: Optional[RiskStatus],
    ledger: Ledger,
    mode: Mode,
    stats: dict[str, float],
    unknowns: list[str],
) -> Optional[RiskView]:
    """RiskView from the monitor, else the ledger's last account snapshot."""
    if risk_status is not None:
        snap = risk_status.snapshot
        warnings = list(risk_status.warnings)
        if snap.breaker_state is BreakerState.TRIPPED:
            warnings.append(_BREAKER_WARNING)
        return RiskView(
            equity=snap.equity,
            cash=snap.cash,
            day_pnl=snap.day_pnl,
            drawdown_pct=snap.drawdown_pct,
            breaker_state=snap.breaker_state.value,
            pool_exposure_pct={
                Role(role).value: pct
                for role, pct in risk_status.per_pool_exposure_pct.items()
            },
            warnings=warnings,
            stats=stats,
        )

    snapshots = _safe(
        "ledger account snapshots",
        lambda: ledger.get_snapshots(mode),
        [],
        unknowns,
    )
    if not snapshots:
        return None
    snap = snapshots[-1]
    warnings = [
        "risk monitor has not run — values from the last ledger account "
        f"snapshot (as of {snap.ts.isoformat()})"
    ]
    if snap.breaker_state is BreakerState.TRIPPED:
        warnings.append(_BREAKER_WARNING)
    return RiskView(
        equity=snap.equity,
        cash=snap.cash,
        day_pnl=snap.day_pnl,
        drawdown_pct=snap.drawdown_pct,
        breaker_state=snap.breaker_state.value,
        pool_exposure_pct={},
        warnings=warnings,
        stats=stats,
    )


def _build_movers(
    watch: dict[str, WatchState], lookup: Callable
) -> tuple[MoversView, list[Mover]]:
    movers: list[Mover] = []
    for symbol in sorted(watch):
        state = watch[symbol]
        dist20 = _dist_pct(state.last, state.sma20)
        if dist20 is None:
            logger.debug("brief: no SMA20, excluded from movers", extra={"symbol": symbol})
            continue
        item = lookup(symbol)
        movers.append(
            Mover(
                symbol=symbol,
                last=state.last,
                dist_sma20_pct=dist20,
                dist_sma50_pct=_dist_pct(state.last, state.sma50),
                theme=item.theme if item is not None else "unknown",
                ai_phase=(
                    item.ai_phase.value if item is not None else AiPhase.NONE.value
                ),
                role=item.role.value if item is not None else Role.ROTATION.value,
                region=_region_of(symbol),
            )
        )
    top = sorted(movers, key=lambda m: (-m.dist_sma20_pct, m.symbol))[:TOP_MOVERS]
    bottom = sorted(movers, key=lambda m: (m.dist_sma20_pct, m.symbol))[:TOP_MOVERS]
    return MoversView(top=top, bottom=bottom), movers


def _build_themes(
    watch: dict[str, WatchState], lookup: Callable
) -> list[ThemeView]:
    grouped: dict[str, list[tuple[str, float]]] = {}
    for symbol in sorted(watch):
        state = watch[symbol]
        dist50 = _dist_pct(state.last, state.sma50)
        if dist50 is None:
            continue
        item = lookup(symbol)
        theme = item.theme if item is not None else "unknown"
        grouped.setdefault(theme, []).append((symbol, dist50))
    views: list[ThemeView] = []
    for theme, rows in grouped.items():
        avg = sum(dist for _, dist in rows) / len(rows)
        leaders = [
            sym
            for sym, _ in sorted(rows, key=lambda r: (-r[1], r[0]))[:THEME_LEADERS]
        ]
        views.append(
            ThemeView(
                theme=theme,
                avg_dist_sma50_pct=avg,
                n_symbols=len(rows),
                leaders=leaders,
            )
        )
    views.sort(key=lambda v: (-v.avg_dist_sma50_pct, v.theme))
    return views


def _build_news(news: Optional[NewsSnapshot]) -> NewsSection:
    if news is None:
        return NewsSection()

    def rank_key(raw: dict) -> tuple[float, str]:
        sentiment = raw.get("sentiment")
        magnitude = abs(sentiment) if sentiment is not None else 0.0
        return (-magnitude, str(raw.get("headline", "")))

    ranked = sorted(news.items, key=rank_key)[:TOP_NEWS]
    items = [
        NewsDigestItem(
            headline=str(raw.get("headline", "")),
            source=str(raw.get("source") or ""),
            url=str(raw.get("url") or ""),
            sentiment=raw.get("sentiment"),
            symbol=raw.get("symbol"),
        )
        for raw in ranked
    ]
    return NewsSection(
        items=items,
        per_symbol_sentiment=dict(news.per_symbol_sentiment),
    )


def _build_provenance(items: list[NewsDigestItem]) -> list[ProvenanceLink]:
    """Data-source note + deduped (by URL) citations for the digest items."""
    links = [ProvenanceLink(label=DATA_SOURCE_NOTE, url=DATA_SOURCE_URL)]
    seen: set[str] = {DATA_SOURCE_URL}
    for item in items:
        url = item.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        label = f"{item.source} — {item.headline}" if item.source else item.headline
        links.append(ProvenanceLink(label=_truncate(label, 120), url=url))
    return links


def _signal_views(
    signals: list[Signal], trading_date: str, tz: ZoneInfo
) -> list[SignalView]:
    """Today's (session-tz) signals, debate verdicts first, then confidence."""
    todays = [s for s in signals if _local_date(s.ts, tz) == trading_date]
    todays.sort(
        key=lambda s: (
            0 if s.source_agent == DEBATE_AGENT else 1,
            -s.confidence,
            s.symbol,
        )
    )
    return [
        SignalView(
            symbol=s.symbol,
            direction=s.direction.value,
            confidence=s.confidence,
            source_agent=s.source_agent,
            thesis=_truncate(s.thesis, THESIS_MAX_CHARS),
        )
        for s in todays
    ]


def _candidates_today(
    candidates: list[CandidateOrder], trading_date: str, tz: ZoneInfo
) -> CandidatesToday:
    todays = [c for c in candidates if _local_date(c.ts, tz) == trading_date]
    counts: dict[str, int] = {}
    for cand in todays:
        counts[cand.status.value] = counts.get(cand.status.value, 0) + 1
    pending = [
        PendingCandidate(
            symbol=c.symbol,
            side=c.side.value,
            qty=c.qty,
            confidence=c.confidence,
            status=c.status.value,
        )
        for c in todays
        if c.status in PENDING_CANDIDATE_STATUSES
    ]
    return CandidatesToday(counts=counts, pending=pending)


# ------------------------------------------------------------------- builder


def build_research_brief(
    ledger: Ledger,
    mode: Mode | str,
    *,
    market: Optional[MarketSnapshot] = None,
    portfolio: Optional[PortfolioSnapshot] = None,
    news: Optional[NewsSnapshot] = None,
    risk_status: Optional[RiskStatus] = None,
    llm_enabled: bool = False,
    now: Optional[datetime] = None,
    signals: Optional[list[Signal]] = None,
    candidates: Optional[list[CandidateOrder]] = None,
    watchlist_lookup: Optional[Callable] = None,
    trading_tz: Optional[ZoneInfo] = None,
    include_account: bool = True,
    extra_uncertainty: Optional[list[str]] = None,
    earnings: Optional[list] = None,
) -> ResearchBrief:
    """Build the daily Investment Research brief (Loop.md §7 Phase 0.5).

    ``market``/``portfolio``/``news``/``risk_status`` are the monitor
    snapshot objects; any may be None when the loop has not run, producing a
    DEGRADED brief whose freshness warnings say which sources are missing —
    this function never raises for missing/broken inputs. Deterministic given
    its inputs; ``now`` is injectable for tests.

    US session (defaults): signals and candidates come from the ledger,
    filtered to the current ET trading date, with account risk/stats included
    and the US ``watchlist`` for theme tagging.

    CN research session (Loop.md two-session extension): pass ``signals``
    (in-memory, keeps CN research out of the trading ledger), ``candidates=[]``
    (report-only — no orders), ``include_account=False`` (no CN account),
    ``watchlist_lookup`` = the CN universe lookup, and ``trading_tz`` =
    Asia/Shanghai so "today" is the CN trading date. ``extra_uncertainty``
    prepends session-specific caveats (e.g. "CN session is research-only").
    """
    now = _coerce_now(now)
    mode = Mode(mode)
    tz = trading_tz or ET_ZONE
    lookup = watchlist_lookup or _default_lookup
    trading_date = now.astimezone(tz).date().isoformat()
    unknowns: list[str] = list(extra_uncertainty or [])

    freshness = _freshness(market, news, portfolio, now)
    regime = _regime_view(market)

    if include_account:
        stats = _safe(
            "ledger trade statistics",
            lambda: _stats_dict(ledger.stats(mode)),
            {},
            unknowns,
        )
        risk = _risk_view(risk_status, ledger, mode, stats, unknowns)
    else:
        risk = None  # research-only session: no account/positions to report

    watch = portfolio.watch if portfolio is not None else {}
    movers, all_movers = _build_movers(watch, lookup)
    themes = _build_themes(watch, lookup)
    news_section = _build_news(news)

    if signals is None:
        signals = _safe(
            "ledger signals",
            lambda: ledger.get_signals(mode=mode),
            [],
            unknowns,
        )
    signal_views = _signal_views(signals, trading_date, tz)

    if candidates is None:
        candidates = _safe(
            "ledger candidates",
            lambda: ledger.get_candidates(mode=mode),
            [],
            unknowns,
        )
    candidates_view = _candidates_today(candidates, trading_date, tz)

    if earnings is not None:
        earnings_rows = [
            e.model_dump(mode="json") if hasattr(e, "model_dump") else dict(e)
            for e in earnings
        ]
        notes: list[str] = []
        if not earnings_rows:
            notes.append(
                "no upcoming earnings for the watchlist in the lookahead window"
            )
        events = EventsView(earnings=earnings_rows, notes=notes)
        imminent = [e for e in earnings_rows if e.get("imminent")]
        if imminent:
            names = ", ".join(
                f"{e.get('symbol')} ({e.get('days_until')}d)" for e in imminent
            )
            unknowns.append(
                f"earnings imminent: {names} — avoid opening fresh positions "
                "into the print"
            )
    else:
        events = EventsView(earnings=[], notes=[EARNINGS_NOT_WIRED_NOTE])

    # ---------------------------------------------- auto-collected unknowns
    missing_atr = sorted(
        symbol for symbol, state in watch.items() if state.atr_pct is None
    )
    if missing_atr:
        unknowns.append(
            f"ATR unavailable for {len(missing_atr)} watch symbol(s): "
            f"{', '.join(missing_atr)} — volatility/liquidity checks are "
            "degraded for them"
        )
    if not any(v.source_agent == "fundamental" for v in signal_views):
        unknowns.append(
            "no fundamental signals today — the fundamentals provider may be "
            "empty (Phase 0 default)"
        )
    if earnings is None:
        unknowns.append(EARNINGS_NOT_WIRED_NOTE)
    if llm_enabled:
        unknowns.append(
            "LLM analyst is enabled — its output is analysis-only, "
            "confidence-capped, and never places orders"
        )
    else:
        unknowns.append(
            "LLM analyst is disabled — analysis is rule-based only"
        )

    brief = ResearchBrief(
        as_of=now,
        trading_date=trading_date,
        mode=mode,
        freshness=freshness,
        regime=regime,
        risk=risk,
        movers=movers,
        themes=themes,
        events=events,
        news=news_section,
        signals_today=signal_views,
        candidates_today=candidates_view,
        uncertainty=unknowns,
        provenance=_build_provenance(news_section.items),
    )
    logger.info(
        "research brief built",
        extra={
            "mode": mode.value,
            "trading_date": trading_date,
            "movers": len(all_movers),
            "signals_today": len(signal_views),
            "warnings": len(freshness.warnings),
            "unknowns": len(unknowns),
        },
    )
    return brief
