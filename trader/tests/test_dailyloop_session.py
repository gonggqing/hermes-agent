"""Integration: manual "run trading session now" (Loop.md §4b catch-up).

A session missed while serve was down (or any off-schedule run) must still reach
the approval queue: run_session_now anchors the confirmation window to NOW so the
push isn't refused as WINDOW_CLOSED, while never auto-executing (finalize places
only human-approved candidates).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from swing_trader.api import FinanceRuntime
from swing_trader.dailyloop import DailyLoop
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.simulate import MutableClock, SimFeed, build_sim_series, trading_days

ET = ZoneInfo("America/New_York")
SYMBOLS = ["NVDA", "MU", "ANET"]


@pytest.fixture()
def loop_env(tmp_path):
    days = trading_days(date(2026, 7, 13), 3)
    series, warmup = build_sim_series(SYMBOLS, days)
    feed = SimFeed(series)
    feed.set_day(warmup)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'s.db'}")
    broker = PaperBroker(starting_cash=50_000.0)
    clock = MutableClock(now=datetime.combine(days[0], time(8, 0), tzinfo=ET)
                         .astimezone(timezone.utc))
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=clock)
    loop = DailyLoop(feed, broker, ledger, symbols=SYMBOLS, clock=clock,
                     runtime=runtime, notify=lambda _t: None)
    return loop, runtime, clock, days


class TestRunSessionNow:
    def test_publishes_into_now_anchored_window(self, loop_env):
        loop, runtime, clock, days = loop_env
        # an OFF-schedule time (15:00 ET — well after the 12:30 cutoff)
        run_at = clock.set_et(days[0], 15, 0)
        summary = loop.run_session_now(now=run_at, window_minutes=60)

        # a fresh confirmation exists whose window INCLUDES the run instant,
        # i.e. an off-schedule publish is NOT refused as window_closed
        assert runtime.confirmation is not None
        assert runtime.confirmation.in_window(run_at) is True
        assert summary["cutoff_et"] == "16:00"
        assert "risk_approved" in summary and "entries_halted" in summary
        assert summary["pushed"] == summary["risk_approved"]

    def test_finalize_runs_without_error(self, loop_env):
        loop, runtime, clock, days = loop_env
        run_at = clock.set_et(days[0], 15, 0)
        loop.run_session_now(now=run_at)
        fin = loop.finalize_session_now(now=clock.set_et(days[0], 15, 30))
        assert "approved" in fin and "expired" in fin

    def test_finalize_with_no_session(self, tmp_path):
        # a loop that never ran a session finalizes to a no-op
        days = trading_days(date(2026, 7, 13), 2)
        series, warmup = build_sim_series(SYMBOLS, days)
        feed = SimFeed(series)
        feed.set_day(warmup)
        ledger = Ledger(url=f"sqlite:///{tmp_path/'n.db'}")
        clock = MutableClock(now=datetime(2026, 7, 13, 19, tzinfo=timezone.utc))
        loop = DailyLoop(feed, PaperBroker(starting_cash=1000.0), ledger,
                         symbols=SYMBOLS, clock=clock, notify=lambda _t: None)
        out = loop.finalize_session_now()
        assert out["approved"] == 0 and "no active session" in out["note"]

    def test_late_night_window_does_not_wrap(self, loop_env):
        loop, runtime, clock, days = loop_env
        run_at = clock.set_et(days[0], 23, 40)  # near ET midnight
        loop.run_session_now(now=run_at, window_minutes=60)
        # window clamped to same ET day; still valid at run time
        assert runtime.confirmation.in_window(run_at) is True
