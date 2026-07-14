"""Offline simulation harness (backlog 17: Phase-0 E2E paper dry run).

Provides a deterministic :class:`SimFeed` and a multi-day driver that runs
the REAL daily loop (monitors → agents → decision core → RiskEngine →
ConfirmationService → ExecutionEngine → PaperBroker → Ledger → reporter)
against synthetic bars, with a simulated clock stepping through the §4
schedule.

SIMULATION-ONLY APPROVAL: ``run_simulation(approve="auto")`` approves pending
candidates through the ConfirmationService with actor ``sim-user`` so the
loop can be demonstrated end-to-end without a human. Like the backtester's
bypass, this exists ONLY here, only against a PaperBroker (Loop.md §3; the
production path always requires a real human action).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from swing_trader.api import FinanceRuntime
from swing_trader.confirmation import Surface
from swing_trader.dailyloop import DailyLoop
from swing_trader.datafeed import DataFeedError
from swing_trader.decision import DecisionParams, RuleBasedDecisionCore
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.paper_broker import PaperBroker
from swing_trader.risk import RiskParams
from swing_trader.scheduler import is_trading_day

logger = get_logger(__name__)

__all__ = ["MutableClock", "SimFeed", "make_series", "run_simulation"]

ET = ZoneInfo("America/New_York")
_CLOSE_ET = time(16, 0)


def _close_ts(d: date) -> datetime:
    return datetime.combine(d, _CLOSE_ET, tzinfo=ET).astimezone(timezone.utc)


def trading_days(start: date, n: int) -> list[date]:
    days: list[date] = []
    cur = start
    while len(days) < n:
        if is_trading_day(cur):
            days.append(cur)
        cur += timedelta(days=1)
    return days


def trading_days_ending_before(end: date, n: int) -> list[date]:
    """The ``n`` trading days strictly before ``end`` (warmup history)."""
    days: list[date] = []
    cur = end - timedelta(days=1)
    while len(days) < n:
        if is_trading_day(cur):
            days.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(days))


def build_sim_series(
    symbols: list[str],
    sim_days: list[date],
    warmup_days: int = 90,
    crash_day: Optional[int] = None,
) -> tuple[dict[str, list[Bar]], int]:
    """Series with warmup history so indicator agents (≥60 bars) can fire.

    Returns ``(series, warmup)`` where sim day *i* corresponds to bar index
    ``warmup + i``; a crash lands at sim-relative ``crash_day``.
    """
    history = trading_days_ending_before(sim_days[0], warmup_days)
    all_days = history + sim_days
    crash_abs = warmup_days + crash_day if crash_day is not None else None
    series = {
        sym: make_series(sym, all_days, base=100.0 + 10 * i, crash_at=crash_abs)
        for i, sym in enumerate(symbols)
    }
    for idx in ("SPY", "QQQ", "DIA"):
        series[idx] = make_series(idx, all_days, base=500.0, drift=0.002,
                                  crash_at=crash_abs)
    return series, warmup_days


def make_series(
    symbol: str,
    days: list[date],
    base: float = 100.0,
    drift: float = 0.004,
    wobble: float = 0.012,
    volume: float = 5_000_000.0,
    crash_at: Optional[int] = None,
) -> list[Bar]:
    """Deterministic trending series with pullbacks; optional −12% crash."""
    bars: list[Bar] = []
    level = base
    for i, d in enumerate(days):
        if crash_at is not None and i == crash_at:
            level *= 0.88
        elif crash_at is not None and i > crash_at:
            level *= 0.98
        else:
            level *= 1 + drift
        cycle = [0.0, 1.0, -1.0, 0.5, -0.8][i % 5]
        close = level * (1 + wobble * cycle / 2)
        prev = bars[-1].close if bars else close
        o = prev
        hi = max(o, close) * 1.004
        lo = min(o, close) * 0.996
        bars.append(Bar(symbol=symbol, ts=_close_ts(d),
                        open=o, high=hi, low=lo, close=close, volume=volume))
    return bars


class SimFeed(DataFeed):
    """As-of-aware fake feed. During day *i* only bars ``[:i]`` are visible
    (the live loop sees yesterday's completed daily bar in the morning);
    :meth:`bar_for_day` hands day *i*'s bar to ``on_close``."""

    def __init__(self, series: dict[str, list[Bar]], vix: float = 18.0,
                 vix_crash_at: Optional[int] = None) -> None:
        self._series = {s.upper(): b for s, b in series.items()}
        self._base_vix = vix
        self._vix_crash_at = vix_crash_at
        self._day = 1

    def set_day(self, index: int) -> None:
        self._day = max(1, index)

    def _visible(self, symbol: str) -> list[Bar]:
        bars = self._series.get(symbol.upper())
        if not bars:
            raise DataFeedError(f"no sim data for {symbol}")
        return bars[: self._day]

    def bar_for_day(self, symbol: str, index: int) -> Bar:
        return self._series[symbol.upper()][index]

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if timeframe != "1d":
            raise ValueError("SimFeed supports only 1d bars")
        visible = self._visible(symbol)
        if not visible:
            raise DataFeedError(f"no visible bars for {symbol}")
        return visible[-limit:]

    def get_quote(self, symbol: str) -> Quote:
        if symbol.upper() == "^VIX":
            vix = self._base_vix
            if self._vix_crash_at is not None and self._day > self._vix_crash_at:
                vix = 36.0
            return Quote(symbol="^VIX", ts=datetime.now(timezone.utc), last=vix)
        last = self._visible(symbol)[-1]
        return Quote(symbol=symbol.upper(), ts=last.ts, last=last.close,
                     volume=last.volume)

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        if symbol is None or symbol.upper() not in self._series:
            return []
        last = self._visible(symbol)[-1]
        return [
            NewsItem(symbol=symbol.upper(), ts=last.ts,
                     headline=f"{symbol} beats expectations with record strong quarter {n}",
                     source="sim", url="https://example.invalid/sim")
            for n in range(3)
        ][:limit]


@dataclass
class MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def set_et(self, d: date, hh: int, mm: int, ss: int = 0) -> datetime:
        self.now = datetime.combine(d, time(hh, mm, ss), tzinfo=ET).astimezone(
            timezone.utc
        )
        return self.now


@dataclass
class SimulationResult:
    days: list[date]
    ledger: Ledger
    broker: PaperBroker
    runtime: FinanceRuntime
    morning_reports: list[str] = field(default_factory=list)


def run_simulation(
    n_days: int = 22,
    db_url: str = "sqlite:///:memory:",
    starting_cash: float = 5_000.0,
    start: date = date(2026, 7, 13),
    crash_day: Optional[int] = None,
    approve: str = "auto",  # "auto" | "none"
    symbols: Optional[list[str]] = None,
) -> SimulationResult:
    """Drive the real daily loop for ``n_days`` simulated trading days."""
    symbols = symbols or ["NVDA", "MU", "ANET"]
    days = trading_days(start, n_days)
    series, warmup = build_sim_series(symbols, days, crash_day=crash_day)
    feed = SimFeed(
        series,
        vix_crash_at=(warmup + crash_day) if crash_day is not None else None,
    )

    ledger = Ledger(url=db_url)
    broker = PaperBroker(starting_cash=starting_cash)
    clock = MutableClock(now=datetime.combine(
        days[0], time(8, 0), tzinfo=ET).astimezone(timezone.utc))
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=clock)
    reports: list[str] = []
    loop = DailyLoop(
        feed, broker, ledger,
        symbols=symbols, clock=clock, runtime=runtime,
        notify=reports.append,
        decision_core=RuleBasedDecisionCore(
            params=DecisionParams(), risk_params=RiskParams()
        ),
    )

    morning: list[str] = []
    for i, d in enumerate(days):
        feed.set_day(warmup + i)  # morning: yesterday's bars visible
        clock.set_et(d, 9, 0)
        before = len(reports)
        loop.on_morning_report()
        morning.extend(reports[before:])

        clock.set_et(d, 9, 30)
        loop.on_monitor()
        clock.set_et(d, 11, 0)
        loop.on_decide()
        clock.set_et(d, 11, 30, 30)
        loop.on_push()

        if approve == "auto" and runtime.confirmation is not None:
            clock.set_et(d, 11, 45)
            for cand, version in runtime.confirmation.pending():
                # SIMULATION-ONLY bypass; see module docstring.
                runtime.confirmation.act(
                    cand.id, "approve", actor="sim-user", surface=Surface.WEB,
                    idempotency_key=f"sim:{d.isoformat()}:{cand.id[:8]}",
                    now_utc=clock.now, expected_version=version,
                )

        clock.set_et(d, 12, 30, 30)
        loop.on_cutoff()

        clock.set_et(d, 16, 0, 30)
        bars = {}
        for sym in series:
            try:
                bars[sym] = feed.bar_for_day(sym, warmup + i)
            except (KeyError, IndexError):
                continue
        loop.on_close(bars=bars)
        feed.set_day(warmup + i + 1)

    return SimulationResult(
        days=days, ledger=ledger, broker=broker, runtime=runtime,
        morning_reports=morning,
    )
