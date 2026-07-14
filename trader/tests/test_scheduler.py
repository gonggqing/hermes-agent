"""Tests for swing_trader.scheduler (Loop.md §4 state machine, §9 quality bar).

Fully deterministic: every instant is an ET wall time converted to UTC via
zoneinfo, and the runner gets an injected FakeClock — no wall clock, no
threads, no sleeps, no network (Loop.md §3).

DST coverage: the same ET wall times are asserted in BOTH regimes —
July 2026 (EDT, UTC-4: 11:30 ET == 15:30 UTC) and January 2026
(EST, UTC-5: 11:30 ET == 16:30 UTC).

2026 calendar facts used below (verified against a wall calendar):
- Wed Jul 8 / Wed Jan 14: ordinary trading days.
- Fri Jul 3: Independence Day observed (Jul 4 is a Saturday) -> closed.
- Thu Jul 2 -> next trading day is Mon Jul 6 (holiday Fri + weekend).
- Fri Jul 10 -> next trading day is Mon Jul 13.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from swing_trader.scheduler import (
    ET,
    EVENT_TIMES_ET,
    NYSE_FULL_DAY_HOLIDAYS_2026,
    DailyLoopRunner,
    Event,
    LoopPhase,
    event_instant,
    is_trading_day,
    next_event,
    phase_at,
)

UTC = timezone.utc


def at_et(y: int, m: int, d: int, h: int, mi: int = 0, s: int = 0) -> datetime:
    """UTC instant for an ET wall time."""
    return datetime(y, m, d, h, mi, s, tzinfo=ET).astimezone(UTC)


class FakeClock:
    """Injectable clock: tests move ``now`` explicitly (Loop.md §3/§9)."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    """Callback set that records firing order."""

    def __init__(self, events: list[Event] | None = None) -> None:
        self.calls: list[Event] = []
        events = events if events is not None else list(Event)
        self.callbacks = {e: self._make(e) for e in events}

    def _make(self, event: Event):
        def _cb() -> None:
            self.calls.append(event)

        return _cb


# ------------------------------------------------------------- DST sanity


class TestDstRegimes:
    def test_july_is_edt_utc_minus_4(self) -> None:
        assert at_et(2026, 7, 8, 11, 30) == datetime(2026, 7, 8, 15, 30, tzinfo=UTC)

    def test_january_is_est_utc_minus_5(self) -> None:
        assert at_et(2026, 1, 14, 11, 30) == datetime(2026, 1, 14, 16, 30, tzinfo=UTC)


# ------------------------------------------------------------ trading days


class TestIsTradingDay:
    def test_ordinary_weekday(self) -> None:
        assert is_trading_day(date(2026, 7, 8))  # Wednesday
        assert is_trading_day(date(2026, 1, 14))  # Wednesday

    def test_weekend_closed(self) -> None:
        assert not is_trading_day(date(2026, 7, 11))  # Saturday
        assert not is_trading_day(date(2026, 7, 12))  # Sunday

    @pytest.mark.parametrize("holiday", sorted(NYSE_FULL_DAY_HOLIDAYS_2026))
    def test_2026_full_day_holidays_closed(self, holiday: date) -> None:
        assert not is_trading_day(holiday)

    def test_july_3_is_the_observed_friday(self) -> None:
        # Jul 4 2026 is a Saturday; the NYSE observes Friday Jul 3.
        assert date(2026, 7, 3).weekday() == 4  # Friday
        assert date(2026, 7, 4).weekday() == 5  # Saturday
        assert not is_trading_day(date(2026, 7, 3))

    def test_holiday_table_has_the_ten_2026_closures(self) -> None:
        assert len(NYSE_FULL_DAY_HOLIDAYS_2026) == 10
        assert all(d.year == 2026 for d in NYSE_FULL_DAY_HOLIDAYS_2026)


# ---------------------------------------------------------------- phase_at

PHASE_BOUNDARIES: list[tuple[tuple[int, int, int], LoopPhase]] = [
    ((0, 0, 0), LoopPhase.OFF_HOURS),
    ((8, 59, 59), LoopPhase.OFF_HOURS),  # MORNING_REPORT time is still OFF_HOURS
    ((9, 0, 0), LoopPhase.OFF_HOURS),
    ((9, 29, 59), LoopPhase.OFF_HOURS),
    ((9, 30, 0), LoopPhase.MONITORING),
    ((10, 59, 59), LoopPhase.MONITORING),
    ((11, 0, 0), LoopPhase.DECIDING),
    ((11, 29, 59), LoopPhase.DECIDING),
    ((11, 30, 0), LoopPhase.CONFIRM_WINDOW),
    ((12, 29, 59), LoopPhase.CONFIRM_WINDOW),
    ((12, 30, 0), LoopPhase.SET_AND_FORGET),
    ((15, 59, 59), LoopPhase.SET_AND_FORGET),
    ((16, 0, 0), LoopPhase.AFTER_CLOSE),
    ((23, 59, 59), LoopPhase.AFTER_CLOSE),
]


class TestPhaseAt:
    @pytest.mark.parametrize(("hms", "expected"), PHASE_BOUNDARIES)
    def test_boundaries_july_edt(
        self, hms: tuple[int, int, int], expected: LoopPhase
    ) -> None:
        assert phase_at(at_et(2026, 7, 8, *hms)) is expected

    @pytest.mark.parametrize(("hms", "expected"), PHASE_BOUNDARIES)
    def test_boundaries_january_est(
        self, hms: tuple[int, int, int], expected: LoopPhase
    ) -> None:
        assert phase_at(at_et(2026, 1, 14, *hms)) is expected

    def test_same_utc_instant_maps_differently_across_dst(self) -> None:
        # 15:30 UTC is 11:30 ET in July (EDT) but 10:30 ET in January (EST).
        assert phase_at(datetime(2026, 7, 8, 15, 30, tzinfo=UTC)) is LoopPhase.CONFIRM_WINDOW
        assert phase_at(datetime(2026, 1, 14, 15, 30, tzinfo=UTC)) is LoopPhase.MONITORING

    def test_weekend_is_off_hours_all_day(self) -> None:
        assert phase_at(at_et(2026, 7, 11, 12, 0)) is LoopPhase.OFF_HOURS  # Saturday
        assert phase_at(at_et(2026, 7, 12, 10, 0)) is LoopPhase.OFF_HOURS  # Sunday

    def test_holiday_is_off_hours_mid_session(self) -> None:
        assert phase_at(at_et(2026, 7, 3, 11, 45)) is LoopPhase.OFF_HOURS

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            phase_at(datetime(2026, 7, 8, 15, 30))


# --------------------------------------------------------------- next_event


class TestNextEvent:
    def test_mid_morning_july(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 10, 0))
        assert event is Event.DECIDE_START
        assert instant == datetime(2026, 7, 8, 15, 0, tzinfo=UTC)  # 11:00 EDT

    def test_mid_morning_january_est_offset(self) -> None:
        event, instant = next_event(at_et(2026, 1, 14, 10, 0))
        assert event is Event.DECIDE_START
        assert instant == datetime(2026, 1, 14, 16, 0, tzinfo=UTC)  # 11:00 EST

    def test_exactly_at_event_returns_the_following_one(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 11, 0, 0))
        assert event is Event.PUSH_CANDIDATES
        assert instant == at_et(2026, 7, 8, 11, 30)

    def test_one_second_before_event(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 11, 29, 59))
        assert event is Event.PUSH_CANDIDATES
        assert instant == datetime(2026, 7, 8, 15, 30, tzinfo=UTC)  # 11:30 EDT

    def test_before_morning_report_same_day(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 5, 0))
        assert (event, instant) == (Event.MORNING_REPORT, at_et(2026, 7, 8, 9, 0))

    def test_after_close_rolls_to_next_day_morning_report(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 17, 0))
        assert event is Event.MORNING_REPORT
        assert instant == datetime(2026, 7, 9, 13, 0, tzinfo=UTC)  # Thu 09:00 EDT

    def test_friday_after_close_rolls_to_monday(self) -> None:
        event, instant = next_event(at_et(2026, 7, 10, 16, 30))
        assert (event, instant) == (Event.MORNING_REPORT, at_et(2026, 7, 13, 9, 0))

    def test_day_before_observed_holiday_skips_to_monday(self) -> None:
        # Thu Jul 2 after close -> Fri Jul 3 (holiday), Sat, Sun all skipped.
        event, instant = next_event(at_et(2026, 7, 2, 18, 0))
        assert (event, instant) == (Event.MORNING_REPORT, at_et(2026, 7, 6, 9, 0))

    def test_saturday_any_time_points_to_monday(self) -> None:
        event, instant = next_event(at_et(2026, 7, 11, 3, 0))
        assert (event, instant) == (Event.MORNING_REPORT, at_et(2026, 7, 13, 9, 0))

    def test_after_midnight_before_report_same_calendar_day(self) -> None:
        event, instant = next_event(at_et(2026, 7, 8, 0, 30))
        assert (event, instant) == (Event.MORNING_REPORT, at_et(2026, 7, 8, 9, 0))

    def test_full_day_event_sequence(self) -> None:
        """Walking the day yields all six events in §4 order, then next day."""
        now = at_et(2026, 7, 8, 8, 0)
        seen: list[tuple[Event, datetime]] = []
        for _ in range(7):
            event, instant = next_event(now)
            seen.append((event, instant))
            now = instant
        assert [e for e, _ in seen] == [
            Event.MORNING_REPORT,
            Event.MONITOR_START,
            Event.DECIDE_START,
            Event.PUSH_CANDIDATES,
            Event.CONFIRM_CUTOFF,
            Event.MARKET_CLOSE,
            Event.MORNING_REPORT,  # next trading day
        ]
        assert seen[-1][1] == at_et(2026, 7, 9, 9, 0)
        instants = [i for _, i in seen]
        assert instants == sorted(instants)

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            next_event(datetime(2026, 7, 8, 15, 30))

    def test_event_instant_matches_table(self) -> None:
        d = date(2026, 1, 14)
        for event, t in EVENT_TIMES_ET.items():
            instant = event_instant(d, event)
            assert instant.tzinfo is not None
            assert instant.astimezone(ET).time() == t


# ------------------------------------------------------------------- runner


class TestDailyLoopRunner:
    def test_fires_each_event_once_stepping_through_the_day(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        expected_order = [
            Event.MORNING_REPORT,
            Event.MONITOR_START,
            Event.DECIDE_START,
            Event.PUSH_CANDIDATES,
            Event.CONFIRM_CUTOFF,
            Event.MARKET_CLOSE,
        ]
        for event in expected_order:
            clock.now = event_instant(date(2026, 7, 8), event) + timedelta(seconds=1)
            fired = runner.run_pending()
            assert [e for e, _ in fired] == [event]
        assert rec.calls == expected_order

    def test_no_double_fire_on_repeated_polls(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 9, 15))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 9, 45)
        assert [e for e, _ in runner.run_pending()] == [Event.MONITOR_START]
        assert runner.run_pending() == []  # same instant again
        clock.now = at_et(2026, 7, 8, 10, 59)  # still MONITORING, nothing new due
        assert runner.run_pending() == []
        assert rec.calls == [Event.MONITOR_START]

    def test_catches_up_multiple_missed_events_in_order(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 12, 0)  # idle all morning
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [
            Event.MORNING_REPORT,
            Event.MONITOR_START,
            Event.DECIDE_START,
            Event.PUSH_CANDIDATES,
        ]
        assert rec.calls == [e for e, _ in fired]

    def test_morning_report_fires_before_monitor_start(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 55))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 9, 31)
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.MORNING_REPORT, Event.MONITOR_START]
        assert fired[0][1] == at_et(2026, 7, 8, 9, 0)
        assert fired[1][1] == at_et(2026, 7, 8, 9, 30)

    def test_events_before_construction_never_fire(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 12, 0))  # built mid-day
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        assert runner.run_pending() == []
        clock.now = at_et(2026, 7, 8, 12, 45)
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.CONFIRM_CUTOFF]  # morning skipped
        assert rec.calls == [Event.CONFIRM_CUTOFF]

    def test_fires_at_the_exact_instant(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 9, 0, 1))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 9, 30, 0)  # exactly 09:30:00 ET
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.MONITOR_START]
        assert fired[0][1] == at_et(2026, 7, 8, 9, 30, 0)

    def test_same_event_fires_again_on_the_next_trading_day(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 30))
        rec = Recorder([Event.MORNING_REPORT])
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 9, 5)
        runner.run_pending()
        clock.now = at_et(2026, 7, 9, 9, 5)
        runner.run_pending()
        assert rec.calls == [Event.MORNING_REPORT, Event.MORNING_REPORT]

    def test_weekend_gap_fires_only_monday_report(self) -> None:
        # Built Friday Jul 10 after close; polled again Monday 09:05 ET.
        clock = FakeClock(at_et(2026, 7, 10, 17, 0))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 13, 9, 5)
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.MORNING_REPORT]
        assert fired[0][1] == at_et(2026, 7, 13, 9, 0)

    def test_holiday_gap_thursday_to_monday(self) -> None:
        # Built Thu Jul 2 mid-afternoon; Fri Jul 3 observed holiday, then
        # weekend. Polling Monday noon catches Thu close + Monday morning.
        clock = FakeClock(at_et(2026, 7, 2, 14, 0))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 6, 12, 0)
        fired = runner.run_pending()
        assert [(e, i.astimezone(ET).date()) for e, i in fired] == [
            (Event.MARKET_CLOSE, date(2026, 7, 2)),
            (Event.MORNING_REPORT, date(2026, 7, 6)),
            (Event.MONITOR_START, date(2026, 7, 6)),
            (Event.DECIDE_START, date(2026, 7, 6)),
            (Event.PUSH_CANDIDATES, date(2026, 7, 6)),
        ]

    def test_est_regime_runner_january(self) -> None:
        clock = FakeClock(datetime(2026, 1, 14, 16, 29, 59, tzinfo=UTC))  # 11:29:59 ET
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = datetime(2026, 1, 14, 16, 30, 0, tzinfo=UTC)  # 11:30:00 EST
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.PUSH_CANDIDATES]

    def test_unregistered_events_advance_without_callbacks(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        rec = Recorder([Event.PUSH_CANDIDATES])  # only one callback registered
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 16, 30)
        fired = runner.run_pending()
        assert len(fired) == 6  # all six events counted as fired
        assert rec.calls == [Event.PUSH_CANDIDATES]  # but only one invoked

    def test_raising_callback_is_logged_not_retried_and_does_not_block(self) -> None:
        calls: list[str] = []

        def boom() -> None:
            calls.append("boom")
            raise RuntimeError("kaput")

        def ok() -> None:
            calls.append("ok")

        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        runner = DailyLoopRunner(
            {Event.MORNING_REPORT: boom, Event.MONITOR_START: ok}, clock
        )
        clock.now = at_et(2026, 7, 8, 10, 0)
        fired = runner.run_pending()
        assert [e for e, _ in fired] == [Event.MORNING_REPORT, Event.MONITOR_START]
        assert calls == ["boom", "ok"]
        assert runner.run_pending() == []  # the raiser is NOT re-fired
        assert calls == ["boom", "ok"]

    def test_explicit_now_overrides_clock(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        # clock still says 08:00; explicit now says 09:10.
        fired = runner.run_pending(now=at_et(2026, 7, 8, 9, 10))
        assert [e for e, _ in fired] == [Event.MORNING_REPORT]

    def test_naive_now_rejected(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        runner = DailyLoopRunner({}, clock)
        with pytest.raises(ValueError, match="timezone-aware"):
            runner.run_pending(now=datetime(2026, 7, 8, 13, 0))

    def test_non_event_callback_key_rejected(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 0))
        with pytest.raises(TypeError, match="Event"):
            DailyLoopRunner({"PUSH_CANDIDATES": lambda: None}, clock)  # type: ignore[dict-item]

    def test_run_forever_polls_then_sleeps(self) -> None:
        clock = FakeClock(at_et(2026, 7, 8, 8, 55))
        rec = Recorder()
        runner = DailyLoopRunner(rec.callbacks, clock)
        clock.now = at_et(2026, 7, 8, 9, 5)
        sleeps: list[float] = []

        def stub_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            raise StopIteration  # break out after one iteration

        with pytest.raises(StopIteration):
            runner.run_forever(sleep_fn=stub_sleep, poll_seconds=7.0)
        assert rec.calls == [Event.MORNING_REPORT]  # run_pending ran first
        assert sleeps == [7.0]
