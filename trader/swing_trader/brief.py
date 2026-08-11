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
from swing_trader.interfaces import NewsItem
from swing_trader.instrument_names import name_for
from swing_trader.discovery import DiscoveryPool
from swing_trader.ledger import Ledger, TradeStats
from swing_trader.log import get_logger
from swing_trader.monitors import (
    MarketSnapshot,
    NewsSnapshot,
    PortfolioSnapshot,
    RiskStatus,
    WatchState,
)
from swing_trader.news_quality import curate_news, source_quality
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
    "ForecastClaim",
    "HoldingView",
    "Mover",
    "MoversView",
    "NewsDigestItem",
    "NewsSection",
    "NarrativeSection",
    "PendingCandidate",
    "ProvenanceLink",
    "RegimeView",
    "ResearchBrief",
    "ResearchNarrative",
    "ThesisAction",
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

#: News digest size (freshness/source quality first, then |sentiment|).
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
    currency: str = "USD"  # the sleeve equity/cash are reported in (HK: HKD)
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
    #: Human name (e.g. "三星电子") so the desk shows more than an opaque code;
    #: "" when unknown (never guessed).
    display_name: str = ""
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
    """One fresh, cited and de-duplicated headline in the digest."""

    headline: str
    source: str = ""
    url: str = ""
    sentiment: Optional[float] = None
    symbol: Optional[str] = None
    published_at: Optional[datetime] = None
    age_hours: Optional[float] = Field(default=None, ge=0.0)
    source_quality: str = "unrated"

    @field_validator("published_at")
    @classmethod
    def _published_at_tz_aware(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is not None and value.tzinfo is None:
            raise ValueError("published_at must be timezone-aware")
        return value


class NewsSection(BaseModel):
    """Current-catalyst digest plus fresh-only per-symbol sentiment."""

    items: list[NewsDigestItem] = Field(default_factory=list)
    per_symbol_sentiment: dict[str, float] = Field(default_factory=dict)
    stale_items_excluded: int = 0
    future_items_excluded: int = 0
    duplicate_items_excluded: int = 0


class SignalView(BaseModel):
    """Analysis/debate signal summary (thesis truncated for the brief)."""

    signal_id: str = ""
    symbol: str
    display_name: str = ""  # human name (or "" when unknown); see instrument_names
    direction: str
    confidence: float
    source_agent: str
    thesis: str
    #: DATA as-of (ISO): the last price-bar date the verdict rests on (Loop.md
    #: §5.10). Distinct from the brief's as_of (when it was generated). None for
    #: signals with no price bar (sentiment/macro).
    as_of_bar: Optional[str] = None
    #: Price used by the source signal when available. This freezes the
    #: revision baseline for later outcome evaluation.
    baseline_value: Optional[float] = None
    #: Structured factors used by the signal.  The primary writer needs these
    #: to reason about persistence/volume/drawdown instead of paraphrasing only
    #: SMA and RSI prose.
    features: dict = Field(default_factory=dict)


class HoldingView(BaseModel):
    """One current position supplied to the research writer (never authority)."""

    symbol: str
    display_name: str = ""
    currency: str = ""
    qty: float
    avg_px: Optional[float] = None
    mkt_px: Optional[float] = None
    unrealized_pct: Optional[float] = None
    role: str = ""
    environment: str = ""
    account_names: list[str] = Field(default_factory=list)
    source: str = "broker"


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


class NarrativeSection(BaseModel):
    """One market-specific analytical section written by the brief model."""

    title: str = Field(min_length=1, max_length=80)
    analysis: str = Field(min_length=1, max_length=1600)


class ForecastClaim(BaseModel):
    """One measurable primary-model claim emitted beside the prose brief.

    A claim is not an order recommendation. It becomes an append-only
    revision in the prediction ledger and is evaluated over explicit trading-
    session horizons without parsing prose after publication.
    """

    entity_type: str = Field(pattern=r"^(market|instrument|theme|event)$")
    entity_key: str = Field(min_length=1, max_length=160)
    claim_type: str = Field(min_length=1, max_length=80)
    direction: str = Field(min_length=1, max_length=32)
    confidence: float = Field(ge=0.0, le=1.0)
    horizons: list[int] = Field(min_length=1, max_length=6)
    thesis: str = Field(min_length=1, max_length=1200)
    invalidation: str = Field(default="", max_length=800)
    benchmark: str = Field(default="", max_length=64)
    expected_condition: str = Field(default="", max_length=800)
    evidence_refs: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("horizons")
    @classmethod
    def _valid_horizons(cls, value: list[int]) -> list[int]:
        allowed = {1, 3, 5, 10, 20, 60}
        normalized = sorted(set(value))
        if not normalized or any(item not in allowed for item in normalized):
            raise ValueError("horizons must use 1/3/5/10/20/60 trading sessions")
        return normalized


class ThesisAction(BaseModel):
    """Human-readable trend stance; research guidance, never an order."""

    symbol: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=160)
    stance: str = Field(
        pattern=(
            r"^(buy_on_confirmation|hold|reduce_on_weakness|"
            r"exit_if_invalidated|watch|avoid)$"
        )
    )
    thesis_state: str = Field(
        pattern=r"^(new|strengthened|unchanged|weakened|invalidated)$"
    )
    confidence: float = Field(ge=0.0, le=1.0)
    horizon_sessions: int = Field(ge=1, le=60)
    what_changed: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=1000)
    invalidation: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


class ResearchNarrative(BaseModel):
    """Model-written synthesis of the complete structured research snapshot.

    This is deliberately separate from the deterministic measurements below.
    The measurements remain the source of truth; this object explains their
    relationships and significance without acquiring any trading authority.
    """

    generated_at: datetime
    market: str = Field(min_length=1, max_length=32)
    language: str = Field(min_length=2, max_length=16)
    model: str = Field(min_length=1, max_length=120)
    prompt_version: str = Field(default="legacy", min_length=1, max_length=80)
    evidence_hash: str = Field(default="", max_length=128)
    edition: str = Field(default="", max_length=24)
    headline: str = Field(min_length=1, max_length=180)
    summary: str = Field(min_length=1, max_length=2400)
    change_summary: list[str] = Field(default_factory=list, max_length=6)
    action_views: list[ThesisAction] = Field(default_factory=list, max_length=12)
    sections: list[NarrativeSection] = Field(min_length=4, max_length=7)
    watch_next: list[str] = Field(default_factory=list, max_length=6)
    claims: list[ForecastClaim] = Field(default_factory=list, max_length=20)

    @field_validator("generated_at")
    @classmethod
    def _generated_at_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        return v


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
    holdings: list[HoldingView] = Field(default_factory=list)
    movers: MoversView = Field(default_factory=MoversView)
    themes: list[ThemeView] = Field(default_factory=list)
    events: EventsView = Field(default_factory=EventsView)
    news: NewsSection = Field(default_factory=NewsSection)
    signals_today: list[SignalView] = Field(default_factory=list)
    candidates_today: CandidatesToday = Field(default_factory=CandidatesToday)
    # Phase 0.95: research-only symbols discovered outside the static
    # watchlist. This object deliberately contains no order/candidate state.
    discovery: Optional[DiscoveryPool] = None
    # Optional for old archives, degraded runs, or a failed/unconfigured model.
    # Never substitute deterministic template prose for a missing narrative.
    narrative: Optional[ResearchNarrative] = None
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
    currency: Optional[str] = None,
) -> Optional[RiskView]:
    """RiskView from the monitor, else the ledger's last account snapshot.

    ``currency`` scopes equity/cash to ONE market's sleeve (e.g. "HKD" for HK)
    so a per-market brief shows that market's net value, not the FX-blended base
    total."""
    if risk_status is not None:
        snap = risk_status.snapshot
        warnings = list(risk_status.warnings)
        if snap.breaker_state is BreakerState.TRIPPED:
            warnings.append(_BREAKER_WARNING)
        equity = (
            snap.equity_by_currency.get(currency, snap.equity)
            if currency else snap.equity
        )
        cash = (
            snap.cash_by_currency.get(currency, snap.cash)
            if currency else snap.cash
        )
        return RiskView(
            equity=equity,
            cash=cash,
            currency=currency or snap.base_currency,
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
        equity=(
            snap.equity_by_currency.get(currency, snap.equity)
            if currency else snap.equity
        ),
        cash=(
            snap.cash_by_currency.get(currency, snap.cash)
            if currency else snap.cash
        ),
        currency=currency or snap.base_currency,
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
                display_name=name_for(symbol),
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


def _raw_news_item(raw: dict) -> Optional[NewsItem]:
    try:
        ts = raw.get("ts")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if not isinstance(ts, datetime):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return NewsItem(
            symbol=raw.get("symbol"),
            ts=ts,
            headline=str(raw.get("headline") or ""),
            source=str(raw.get("source") or ""),
            url=str(raw.get("url") or ""),
            sentiment=raw.get("sentiment"),
        )
    except (TypeError, ValueError):
        return None


def _build_news(news: Optional[NewsSnapshot], now: datetime) -> NewsSection:
    if news is None:
        return NewsSection()

    parsed = [item for raw in news.items if (item := _raw_news_item(raw)) is not None]
    per_symbol = curate_news(parsed, as_of=now, across_symbols=False)
    digest = curate_news(per_symbol.items, as_of=now, across_symbols=True)

    def rank_key(item: NewsItem) -> tuple[float, float, float, str]:
        published = item.ts.astimezone(timezone.utc)
        age_hours = max(0.0, (now.astimezone(timezone.utc) - published).total_seconds() / 3600)
        magnitude = abs(item.sentiment) if item.sentiment is not None else 0.0
        return (age_hours, -source_quality(item.source)[1], -magnitude, item.headline)

    ranked = sorted(digest.items, key=rank_key)[:TOP_NEWS]
    items = [
        NewsDigestItem(
            headline=item.headline,
            source=item.source,
            url=item.url,
            sentiment=item.sentiment,
            symbol=item.symbol,
            published_at=item.ts.astimezone(timezone.utc),
            age_hours=max(
                0.0,
                (
                    now.astimezone(timezone.utc) - item.ts.astimezone(timezone.utc)
                ).total_seconds()
                / 3600,
            ),
            source_quality=source_quality(item.source)[0],
        )
        for item in ranked
    ]

    sentiments: dict[str, list[float]] = {}
    for item in per_symbol.items:
        if item.sentiment is None:
            continue
        key = item.symbol or "MARKET"
        sentiments.setdefault(key, []).append(float(item.sentiment))

    return NewsSection(
        items=items,
        per_symbol_sentiment={
            key: sum(values) / len(values) for key, values in sentiments.items()
        },
        stale_items_excluded=per_symbol.stale_excluded,
        future_items_excluded=per_symbol.future_excluded,
        duplicate_items_excluded=(
            per_symbol.duplicates_excluded
            + max(0, len(per_symbol.items) - len(digest.items))
        ),
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
            signal_id=s.id,
            symbol=s.symbol,
            display_name=name_for(s.symbol),
            direction=s.direction.value,
            confidence=s.confidence,
            source_agent=s.source_agent,
            thesis=_truncate(s.thesis, THESIS_MAX_CHARS),
            as_of_bar=s.as_of_bar.isoformat() if s.as_of_bar is not None else None,
            baseline_value=(
                float(s.features_json["close"])
                if isinstance(s.features_json.get("close"), (int, float))
                else None
            ),
            features=dict(s.features_json),
        )
        for s in todays
    ]


def _holding_views(
    portfolio: Optional[PortfolioSnapshot], environment: str
) -> list[HoldingView]:
    if portfolio is None:
        return []
    rows: list[HoldingView] = []
    for position in portfolio.positions:
        unrealized_pct = None
        if position.avg_px > 0 and position.mkt_px is not None:
            unrealized_pct = (position.mkt_px / position.avg_px - 1.0) * 100.0
        rows.append(
            HoldingView(
                symbol=position.symbol,
                display_name=name_for(position.symbol),
                currency=position.currency,
                qty=position.qty,
                avg_px=position.avg_px,
                mkt_px=position.mkt_px,
                unrealized_pct=unrealized_pct,
                role=position.pool.value,
                environment=environment,
                source="broker",
            )
        )
    return sorted(rows, key=lambda row: row.symbol)


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
    discovery: Optional[DiscoveryPool] = None,
    currency: Optional[str] = None,
    additional_holdings: Optional[list[HoldingView]] = None,
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
    (report-only — no orders), ``include_account=False`` (no broker risk),
    ``watchlist_lookup`` = the CN universe lookup, and ``trading_tz`` =
    Asia/Shanghai so "today" is the CN trading date. ``extra_uncertainty``
    prepends session-specific caveats (e.g. "CN session is research-only").
    ``additional_holdings`` may supply a read-only projection of the user's
    real portfolio journal without granting this builder any mutation path.
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
        risk = _risk_view(risk_status, ledger, mode, stats, unknowns, currency)
    else:
        risk = None  # research-only session: no account/positions to report

    watch = portfolio.watch if portfolio is not None else {}
    movers, all_movers = _build_movers(watch, lookup)
    themes = _build_themes(watch, lookup)
    news_section = _build_news(news, now)
    holdings = _holding_views(portfolio, mode.value) if include_account else []
    holdings.extend(row.model_copy(deep=True) for row in (additional_holdings or []))
    holdings.sort(key=lambda row: (row.symbol, row.environment, row.source))

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
    if news_section.stale_items_excluded:
        unknowns.append(
            f"excluded {news_section.stale_items_excluded} news item(s) older than "
            "72 hours; they are historical context, not current catalysts"
        )
    if news_section.future_items_excluded:
        unknowns.append(
            f"excluded {news_section.future_items_excluded} future-dated news item(s)"
        )
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
        holdings=holdings,
        movers=movers,
        themes=themes,
        events=events,
        news=news_section,
        signals_today=signal_views,
        candidates_today=candidates_view,
        discovery=discovery,
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
