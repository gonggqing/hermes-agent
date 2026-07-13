"""Integration: the Phase 0.8 health model wired into the DailyLoop.

Proves the dead-man's switch is *actually* enforced by the loop (not just the
RiskEngine in isolation): when the monitor snapshots go stale between decide
cycles, ``on_decide`` assesses UNHEALTHY, halts new entries, alerts the
reporter bot, and publishes the health status on the runtime for the Finance
tab (Loop.md §5.10). Exits are never gated by it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from swing_trader.api import FinanceRuntime
from swing_trader.dailyloop import DailyLoop
from swing_trader.health import HealthLevel
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import Side
from swing_trader.simulate import (
    MutableClock,
    SimFeed,
    build_sim_series,
    trading_days,
)

ET = ZoneInfo("America/New_York")
SYMBOLS = ["NVDA", "MU", "ANET"]


@pytest.fixture()
def loop_env(tmp_path):
    days = trading_days(date(2026, 7, 13), 3)
    series, warmup = build_sim_series(SYMBOLS, days)
    feed = SimFeed(series)
    feed.set_day(warmup)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'health.db'}")
    broker = PaperBroker(starting_cash=5_000.0)
    clock = MutableClock(now=datetime.combine(days[0], time(8, 0), tzinfo=ET)
                         .astimezone(timezone.utc))
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=clock)
    reports: list[str] = []
    loop = DailyLoop(feed, broker, ledger, symbols=SYMBOLS, clock=clock,
                     runtime=runtime, notify=reports.append)
    return loop, runtime, clock, days, reports


def test_healthy_decide_allows_entries_and_publishes_status(loop_env):
    loop, runtime, clock, days, reports = loop_env
    monitored_at = clock.set_et(days[0], 9, 30)
    loop.on_monitor()  # monitors stamp snapshots with the sim clock (09:30)
    clock.now = monitored_at + timedelta(minutes=30)  # decide → snapshots fresh
    loop.on_decide()

    assert runtime.health is not None
    assert runtime.health.level is HealthLevel.OK
    assert runtime.health.entries_allowed is True
    assert loop._health.entries_allowed is True
    # No halt alert when healthy.
    assert not any("dead-man" in r for r in reports)


def test_stale_snapshots_halt_new_entries_and_alert(loop_env):
    loop, runtime, clock, days, reports = loop_env
    monitored_at = clock.set_et(days[0], 9, 30)
    loop.on_monitor()  # snapshots stamped 09:30 (sim clock)

    # Jump the clock hours ahead WITHOUT re-monitoring: the stored snapshots
    # are now stale (> STALE_AFTER_MINUTES). on_decide keeps the old snapshots
    # (portfolio is not None, so it does not re-poll).
    clock.now = monitored_at + timedelta(hours=3)  # +3h → stale
    before = len(reports)
    loop.on_decide()

    assert runtime.health.level is HealthLevel.UNHEALTHY
    assert runtime.health.entries_allowed is False
    # Every risk-approved item must be an exit, never a new BUY entry.
    assert all(c.side is Side.SELL for c in loop._risk_approved)
    # The reporter bot was alerted with a plain-language halt reason.
    alerts = reports[before:]
    assert any("dead-man" in a and "paused" in a for a in alerts)


def test_edit_revalidation_blocked_when_unhealthy(loop_env):
    """A human edit at cutoff must also fail closed when unhealthy (§5.10)."""
    from swing_trader.schemas import CandidateOrder, OrderType, Role

    loop, runtime, clock, days, reports = loop_env
    monitored_at = clock.set_et(days[0], 9, 30)
    loop.on_monitor()
    clock.now = monitored_at + timedelta(hours=3)
    loop.on_decide()  # sets loop._health unhealthy

    cand = CandidateOrder(
        symbol="NVDA", side=Side.BUY, qty=5, order_type=OrderType.LMT,
        limit=100.0, sl=95.0, rationale="edit", confidence=0.9, pool=Role.ROTATION,
    )
    ok, note = loop._revalidate_edit(cand)
    assert ok is False
    assert "dead-man's switch" in note
