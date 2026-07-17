"""Reporter / dashboard text views (Loop.md §5.9, §4; backlog item 14).

Read-only over :class:`~swing_trader.ledger.Ledger` and
:class:`~swing_trader.interfaces.BrokerInterface`. Produces small pydantic
view models plus plain-text (Telegram-ready, no HTML) renderings:

- :func:`build_account_view` — account view with paper/live switch: the
  ``mode`` parameter selects which ledger history feeds the stats and which
  broker orders are shown, so paper and live never mix (Loop.md §5.8, §6).
- :func:`render_account` — compact fixed-width-ish account dashboard.
- :func:`morning_summary` — next-day 09:00 ET report (Loop.md §4): overnight
  fills, positions, equity/breaker, cumulative stats, yesterday-candidate
  outcomes, and a one-line safety footer showing the mode.
- :func:`push_window_preamble` — 2-3 market-context lines prefixed to the
  10:30 ET candidate cards (Loop.md §4, §5.6).
- :func:`clamp` — length guard under Telegram's 4096-char message cap.

No secrets ever appear in any output (Loop.md §3): the reporter only reads
ledger rows and broker state, neither of which carries credentials.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, field_validator

from swing_trader.interfaces import BrokerInterface
from swing_trader.ledger import Ledger, TradeStats
from swing_trader.log import get_logger
from swing_trader.schemas import (
    BreakerState,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Role,
    Side,
    market_for_currency,
)

__all__ = [
    "AccountView",
    "DEFAULT_MAX_CHARS",
    "OpenOrderView",
    "PositionView",
    "StatsView",
    "TELEGRAM_MAX_CHARS",
    "TRUNCATION_SUFFIX",
    "build_account_view",
    "clamp",
    "morning_summary",
    "push_window_preamble",
    "render_account",
]

logger = get_logger(__name__)

TELEGRAM_MAX_CHARS: int = 4096
"""Telegram hard cap per message."""

DEFAULT_MAX_CHARS: int = 3800
"""Default clamp target — headroom under the Telegram cap."""

TRUNCATION_SUFFIX: str = "… (truncated)"

_BREAKER_TRIPPED_TEXT: str = "BREAKER TRIPPED — no new entries"


# ------------------------------------------------------------------ view models


class PositionView(BaseModel):
    """One holding row (Loop.md §6 Position, flattened for display)."""

    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    currency: str
    market: str = ""  # US / HK / … derived from currency (clarity vs the role)
    qty: float
    avg_px: float
    mkt_px: Optional[float] = None
    upnl: Optional[float] = None
    pool: Role = Role.ROTATION
    price_as_of: Optional[datetime] = None  # when mkt_px was observed
    price_live: bool = False  # True = fresh feed quote; False = last close mark


class OpenOrderView(BaseModel):
    """One resting/working order row."""

    model_config = ConfigDict(validate_assignment=True)

    symbol: str
    currency: str
    side: Side
    qty: float
    order_type: OrderType
    limit: Optional[float] = None
    stop: Optional[float] = None
    status: OrderStatus


class StatsView(BaseModel):
    """Mirror of :class:`swing_trader.ledger.TradeStats` (Loop.md §5.8)."""

    model_config = ConfigDict(validate_assignment=True)

    n_closed: int
    n_wins: int
    win_rate: float
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    payoff_ratio: Optional[float] = None
    expectancy: float
    total_pnl: float
    avg_hold_days: Optional[float] = None
    max_drawdown_pct: float


class AccountView(BaseModel):
    """Full account view (Loop.md §5.9): account, positions, orders, stats."""

    model_config = ConfigDict(validate_assignment=True)

    mode: Mode
    ts: datetime
    equity: float
    cash: float
    upnl: float
    day_pnl: float
    drawdown_pct: float
    breaker_state: BreakerState
    base_currency: str
    cash_by_currency: dict[str, float]
    equity_by_currency: dict[str, float]
    fx_to_base: dict[str, float]
    positions: list[PositionView]
    open_orders: list[OpenOrderView]
    stats: StatsView
    # Freshness of the position marks the equity/uPnL rest on. When a live feed
    # overlay is applied for display, marks_live=True and marks_as_of=now;
    # otherwise the marks are the last daily close (Loop.md §5.9 honesty).
    marks_live: bool = False
    marks_as_of: Optional[datetime] = None

    @field_validator("ts")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (use UTC)")
        return v


# ------------------------------------------------------------------ formatting


def _fmt_money(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:,.2f}"


def _fmt_signed(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+,.2f}"


def _fmt_qty(v: float) -> str:
    return f"{v:g}"


def _fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _fmt_value(v: object) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.2f}"
    return str(v)


def _breaker_text(state: BreakerState) -> str:
    if state is BreakerState.TRIPPED:
        return _BREAKER_TRIPPED_TEXT
    return f"breaker {state.value}"


def _win_rate_text(stats: StatsView) -> str:
    return f"{stats.win_rate * 100:.1f}%" if stats.n_closed else "n/a"


def _safety_footer(mode: Mode) -> str:
    """One-line footer making the trading mode unmistakable (Loop.md §3)."""
    if mode is Mode.PAPER:
        return "mode=paper — no live orders possible"
    return "mode=live — human confirmation required for every order"


def clamp(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Length guard under Telegram's 4096-char cap: truncate with a marker."""
    if max_chars < 0:
        raise ValueError("max_chars must be >= 0")
    if len(text) <= max_chars:
        return text
    if max_chars <= len(TRUNCATION_SUFFIX):
        return TRUNCATION_SUFFIX[:max_chars]
    return text[: max_chars - len(TRUNCATION_SUFFIX)] + TRUNCATION_SUFFIX


# ------------------------------------------------------------------ view builder


def _to_stats_view(stats: TradeStats) -> StatsView:
    return StatsView(
        n_closed=stats.n_closed,
        n_wins=stats.n_wins,
        win_rate=stats.win_rate,
        avg_win=stats.avg_win,
        avg_loss=stats.avg_loss,
        payoff_ratio=stats.payoff_ratio,
        expectancy=stats.expectancy,
        total_pnl=stats.total_pnl,
        avg_hold_days=stats.avg_hold_days,
        max_drawdown_pct=stats.max_drawdown_pct,
    )


def _to_position_view(
    pos: Position,
    *,
    live_px: Optional[float] = None,
    as_of: Optional[datetime] = None,
) -> PositionView:
    """One holding row. When ``live_px`` is given it overrides the broker's
    last-close mark FOR DISPLAY and uPnL is recomputed off it (the broker's own
    marks/equity/breaker are untouched)."""
    mkt = live_px if live_px is not None else pos.mkt_px
    upnl = (mkt - pos.avg_px) * pos.qty if mkt is not None else pos.upnl
    return PositionView(
        symbol=pos.symbol,
        currency=pos.currency,
        market=market_for_currency(pos.currency),
        qty=pos.qty,
        avg_px=pos.avg_px,
        mkt_px=mkt,
        upnl=upnl,
        pool=pos.pool,
        price_as_of=as_of if live_px is not None else None,
        price_live=live_px is not None,
    )


def _to_order_view(order: Order) -> OpenOrderView:
    return OpenOrderView(
        symbol=order.symbol,
        currency=order.currency,
        side=order.side,
        qty=order.qty,
        order_type=order.order_type,
        limit=order.limit,
        stop=order.stop,
        status=order.status,
    )


def build_account_view(
    broker: BrokerInterface,
    ledger: Ledger,
    mode: Mode | str,
    quotes: Optional[dict[str, float]] = None,
    price_as_of: Optional[datetime] = None,
) -> AccountView:
    """Assemble the account view for one mode (paper/live switch, Loop.md §5.9).

    Read-only: broker supplies account state, positions and working orders; the
    ledger supplies cumulative trade statistics. Everything is filtered to
    ``mode`` so paper and live histories never mix.

    ``quotes`` (symbol -> last price) overlays LIVE marks for display only: each
    position's ``mkt_px``/``upnl`` and the equity/uPnL aggregates are recomputed
    off the live price, while the broker's own marks, ``day_open_equity`` and
    breaker stay on the last-close basis. ``marks_live``/``marks_as_of`` tell the
    UI which basis it is looking at, so a stale close is never shown as current.
    """
    m = Mode(mode)
    snap = broker.get_account()
    raw = broker.get_positions()
    open_orders = [
        _to_order_view(o) for o in broker.get_orders(active_only=True) if o.mode is m
    ]
    stats = _to_stats_view(ledger.stats(m))

    if quotes:
        positions = [
            _to_position_view(p, live_px=quotes.get(p.symbol), as_of=price_as_of)
            for p in raw
        ]
        fx = snap.fx_to_base
        # per-currency equity = cash sleeve + Σ qty·live_mark; cash is unchanged
        equity_by_currency = dict(snap.cash_by_currency)
        upnl_base = 0.0
        for p in raw:
            mark = quotes.get(p.symbol)
            mark = mark if mark is not None else p.mkt_px
            if mark is None:
                continue
            equity_by_currency[p.currency] = (
                equity_by_currency.get(p.currency, 0.0) + p.qty * mark
            )
            upnl_base += (mark - p.avg_px) * p.qty * fx.get(p.currency, 0.0)
        equity_base = sum(
            v * fx.get(c, 0.0) for c, v in equity_by_currency.items()
        )
        equity, upnl, equity_by_cur = equity_base, upnl_base, equity_by_currency
        marks_live, marks_as_of = True, price_as_of
    else:
        positions = [_to_position_view(p) for p in raw]
        equity, upnl = snap.equity, snap.upnl
        equity_by_cur = snap.equity_by_currency
        marks_live, marks_as_of = False, snap.ts

    return AccountView(
        mode=m,
        ts=snap.ts,
        equity=equity,
        cash=snap.cash,
        upnl=upnl,
        day_pnl=snap.day_pnl,  # session metric — keeps the broker's close basis
        drawdown_pct=snap.drawdown_pct,
        breaker_state=snap.breaker_state,
        base_currency=snap.base_currency,
        cash_by_currency=snap.cash_by_currency,
        equity_by_currency=equity_by_cur,
        fx_to_base=snap.fx_to_base,
        positions=positions,
        open_orders=open_orders,
        stats=stats,
        marks_live=marks_live,
        marks_as_of=marks_as_of,
    )


# ------------------------------------------------------------------ renderers


def _position_lines(positions: list[PositionView]) -> list[str]:
    if not positions:
        return ["  (none)"]
    return [
        (
            f"  {p.symbol:<6} qty {_fmt_qty(p.qty):>6} @ {_fmt_money(p.avg_px):>10}"
            f"  mkt {_fmt_money(p.mkt_px):>10}  upnl {_fmt_signed(p.upnl):>10}"
            f"  [{p.pool.value}]"
        )
        for p in positions
    ]


def _order_lines(orders: list[OpenOrderView]) -> list[str]:
    if not orders:
        return ["  (none)"]
    return [
        (
            f"  {o.symbol:<6} {o.side.value:<4} {_fmt_qty(o.qty):>6}"
            f" {o.order_type.value:<4} lim {_fmt_money(o.limit):>10}"
            f"  stop {_fmt_money(o.stop):>10}  {o.status.value}"
        )
        for o in orders
    ]


def render_account(view: AccountView, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Compact fixed-width-ish text dashboard (Telegram-ready, no HTML)."""
    s = view.stats
    stats_line = (
        f"stats: {s.n_closed} closed | win {_win_rate_text(s)}"
        f" | expectancy {_fmt_signed(s.expectancy)}"
        f" | payoff {_fmt_money(s.payoff_ratio)}"
        f" | max DD {s.max_drawdown_pct:.2f}%"
    )
    lines = [
        f"[{view.mode.value.upper()}] account @ {_fmt_ts(view.ts)}",
        (
            f"equity {_fmt_money(view.equity)} | cash {_fmt_money(view.cash)}"
            f" | upnl {_fmt_signed(view.upnl)} | day {_fmt_signed(view.day_pnl)}"
            f" | dd {view.drawdown_pct:.2f}%"
        ),
        _breaker_text(view.breaker_state),
        "positions:",
        *_position_lines(view.positions),
        "open orders:",
        *_order_lines(view.open_orders),
        stats_line,
    ]
    return clamp("\n".join(lines), max_chars)


def morning_summary(
    broker: BrokerInterface,
    ledger: Ledger,
    mode: Mode | str,
    since_utc: datetime,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Next-day 09:00 report (Loop.md §4): overnight fills, ledger update.

    Sections: fills since ``since_utc`` (MOC/LOC at yesterday's close plus
    resting GTC fills), positions with upnl, equity/day-pnl/breaker line,
    cumulative stats, yesterday-candidate outcomes (only when the ledger has
    candidates in the window), and the mode safety footer.
    """
    if since_utc.tzinfo is None:
        raise ValueError("since_utc must be timezone-aware (use UTC)")
    m = Mode(mode)
    snap = broker.get_account()
    lines: list[str] = [
        f"Morning report [{m.value}] — as of {_fmt_ts(snap.ts)}",
        f"Overnight fills since {_fmt_ts(since_utc)}:",
    ]

    fills = [f for f in ledger.get_fills(m) if f.ts >= since_utc]
    if fills:
        lines.extend(
            f"  {f.symbol} {f.side.value} {_fmt_qty(f.qty)} @ {_fmt_money(f.px)}"
            f" (comm {_fmt_money(f.commission)})"
            for f in fills
        )
    else:
        lines.append("  no fills overnight")

    lines.append("Positions:")
    lines.extend(_position_lines([_to_position_view(p) for p in broker.get_positions()]))

    lines.append(
        f"Equity {_fmt_money(snap.equity)} | day_pnl {_fmt_signed(snap.day_pnl)}"
        f" | {_breaker_text(snap.breaker_state)}"
    )

    s = _to_stats_view(ledger.stats(m))
    lines.append(
        f"Stats: {s.n_closed} trades | win rate {_win_rate_text(s)}"
        f" | expectancy {_fmt_signed(s.expectancy)}"
        f" | max DD {s.max_drawdown_pct:.2f}%"
    )

    candidates = [c for c in ledger.get_candidates(mode=m) if c.ts >= since_utc]
    if candidates:
        counts: dict[str, int] = {}
        for c in candidates:
            counts[c.status.value] = counts.get(c.status.value, 0) + 1
        outcome = " | ".join(f"{status} {n}" for status, n in sorted(counts.items()))
        lines.append(f"Candidates: {outcome}")

    lines.append(_safety_footer(m))
    return clamp("\n".join(lines), max_chars)


def push_window_preamble(market: dict) -> str:
    """2-3 context lines prefixed to the 10:30 ET candidate push (Loop.md §4).

    ``market`` is a plain dict (e.g. from the MarketMonitor snapshot) with
    optional keys ``risk_on_off``, ``vix``, ``breadth``; missing values
    render as ``n/a``.
    """
    risk = _fmt_value(market.get("risk_on_off"))
    vix = _fmt_value(market.get("vix"))
    breadth = _fmt_value(market.get("breadth"))
    return "\n".join(
        [
            "10:30 ET push — candidates below, confirm by 11:30 ET",
            f"Market: {risk} | VIX {vix} | breadth {breadth}",
        ]
    )
