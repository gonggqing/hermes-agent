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


class TestTelegramPollBeforeDecide:
    def test_on_confirm_poll_polls_telegram_with_no_confirmation(self, loop_env):
        """A portfolio-draft card can be tapped at ANY time, so on_confirm_poll
        must poll Telegram even before the decide phase creates the candidate
        ConfirmationService. Regression: the old `_confirmation is None` guard
        skipped the poll, so a tapped draft card spun forever until ~10:00 ET."""
        loop, runtime, clock, days = loop_env
        calls = []

        class _Tg:
            interactive = True

            def poll(self, service, now):
                calls.append(service)

        loop.telegram = _Tg()
        assert loop._confirmation is None  # decide phase has not run
        loop.on_confirm_poll()
        assert calls == [None]  # polled anyway, forwarding the None service

    def test_telegram_network_failure_does_not_crash_loop(self, loop_env):
        loop, _runtime, _clock, _days = loop_env

        class _BrokenTelegram:
            interactive = True

            def poll(self, _service, _now):
                raise TimeoutError("temporary getUpdates timeout")

        loop.telegram = _BrokenTelegram()
        loop.on_confirm_poll()  # isolated transport failure, no process restart


class TestRunSessionNow:
    def test_publishes_into_now_anchored_window(self, loop_env):
        loop, runtime, clock, days = loop_env
        # an OFF-schedule time (15:00 ET — well after the 11:30 cutoff)
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


class TestResearchRefresh:
    def test_core_brief_publishes_before_optional_enrichment(self, loop_env):
        loop, _, _, _ = loop_env
        calls = []
        loop._ingest_news = lambda: calls.append("news")
        loop._compute_earnings = lambda: calls.append("earnings")
        loop._ingest_research = lambda: calls.append("research")
        loop._publish_brief = lambda: calls.append("publish")

        loop.on_monitor()

        assert calls == ["news", "publish", "earnings", "research", "publish"]

    def test_intraday_us_refresh_does_not_archive_a_canonical_brief(self, loop_env):
        loop, runtime, _, _ = loop_env
        saved = []

        class _Store:
            def save(self, market, payload):
                saved.append((market, payload))

        runtime.brief_store = _Store()
        loop._publish_brief()

        # Only BriefCycleCoordinator archives the primary-model morning/evening
        # publication. Intraday monitor/decision refreshes update runtime state
        # without creating duplicate snapshots or forecast runs.
        assert saved == []
        assert runtime.latest_brief is not None

    def test_run_research_now_never_enters_decision_path(self, loop_env):
        loop, runtime, _, _ = loop_env
        decided = []
        signal_builds = []
        loop.on_decide = lambda: decided.append(True)
        loop._build_signals = (
            lambda **options: signal_builds.append(options) or ([], {})
        )

        result = loop.run_research_now()

        assert result["market"] == "US"
        assert result["brief_ready"] is True
        assert decided == []
        assert signal_builds == [{"persist": False, "include_llm": False}]


class TestSessionParameterization:
    """A DailyLoop built with HK_TRADING_SCHEDULE runs on HK time and market
    scoping, while the US default is unchanged (session parameterization)."""

    def test_hk_loop_derives_hk_tz_and_window(self, tmp_path):
        from swing_trader.scheduler import HK_TRADING_SCHEDULE, HONG_KONG
        from datetime import time as _t
        days = trading_days(date(2026, 7, 13), 2)
        series, warmup = build_sim_series(SYMBOLS, days)
        feed = SimFeed(series)
        feed.set_day(warmup)
        ledger = Ledger(url=f"sqlite:///{tmp_path/'hk.db'}")
        clock = MutableClock(now=datetime(2026, 7, 13, 2, tzinfo=timezone.utc))
        loop = DailyLoop(feed, PaperBroker(starting_cash=50_000.0), ledger,
                         symbols=SYMBOLS, clock=clock,
                         schedule=HK_TRADING_SCHEDULE, notify=lambda _t: None)
        assert loop.market_id == "HK"
        assert loop._tz is HONG_KONG
        assert loop._tz_name == "Asia/Hong_Kong"
        assert loop._push_time == _t(10, 30)
        assert loop._cutoff_time == _t(11, 30)   # 10:30-11:30 HKT window
        assert loop._market_close_time == _t(16, 0)
        assert loop._pre_cutoff_reminder == _t(11, 0)
        assert loop._market_label == "Hong Kong"

    def test_us_default_unchanged(self, loop_env):
        loop, _, _, _ = loop_env
        from swing_trader.scheduler import ET
        assert loop.market_id == "US"
        assert loop._tz is ET


class TestHKOrderCapableWiring:
    def test_hk_loop_exposes_execution_callbacks(self, tmp_path):
        """An order-capable HK loop wires CONFIRM_CUTOFF + MARKET_CLOSE (the
        research session deliberately lacks them)."""
        from swing_trader.scheduler import HK_TRADING_SCHEDULE, Event
        days = trading_days(date(2026, 7, 13), 2)
        series, warmup = build_sim_series(SYMBOLS, days)
        feed = SimFeed(series)
        feed.set_day(warmup)
        ledger = Ledger(url=f"sqlite:///{tmp_path/'hk.db'}")
        clock = MutableClock(now=datetime(2026, 7, 13, 2, tzinfo=timezone.utc))
        loop = DailyLoop(feed, PaperBroker(starting_cash=50_000.0), ledger,
                         symbols=SYMBOLS, clock=clock,
                         schedule=HK_TRADING_SCHEDULE, notify=lambda _t: None)
        cbs = loop.callbacks()
        assert Event.CONFIRM_CUTOFF in cbs and Event.MARKET_CLOSE in cbs
        assert Event.PUSH_CANDIDATES in cbs
