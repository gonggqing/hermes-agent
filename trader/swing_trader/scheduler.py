"""ET-aware daily scheduler (Loop.md §4 state machine; §9 testing bar; backlog #13).

Drives the daily loop: monitors start at 09:30 ET, decision core at 11:00,
candidates pushed to Telegram at 11:30, confirmation cutoff at 12:30, market
close at 16:00, and the morning report at 09:00 the next trading day.

DESIGN DECISION (Loop.md §8 allows substitution with justification): this
module uses pure ``zoneinfo`` time math plus an injectable-clock runner
instead of APScheduler. Rationale:

- **Deterministic, DST-safe unit tests.** Every instant is computed as an ET
  wall time converted to UTC via ``zoneinfo``, so the EDT/EST switch is
  covered by plain assertions (11:30 ET == 15:30 UTC in July, == 16:30 UTC in
  January) with no threads, no sleeps, and no wall clock — tests never touch
  the network or real time (Loop.md §3, §9).
- **Pure core.** ``phase_at`` / ``next_event`` / ``DailyLoopRunner.run_pending``
  are side-effect-free given a clock; APScheduler (or any cron-like host) can
  wrap ``run_pending`` later without touching this core.

Calendar notes:

- ``is_trading_day`` covers Mon–Fri minus NYSE **full-day** holidays for
  2026. Early-close half days (e.g. 2026-11-27, 2026-12-24) are deliberately
  treated as normal trading days for the daily loop — the user window
  (09:30–12:30 ET) ends well before any 13:00 ET early close; only the
  16:00 MARKET_CLOSE event is nominally late on those days.
- All public functions take and return timezone-aware **UTC** datetimes;
  naive datetimes are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from time import sleep as _default_sleep
from typing import Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = [
    "CN_HK_HOLIDAYS_2026",
    "CN_SCHEDULE",
    "ET",
    "EVENT_TIMES_ET",
    "KR_HOLIDAYS_2026",
    "KR_SCHEDULE",
    "SEOUL",
    "SHANGHAI",
    "US_SCHEDULE",
    "DailyLoopRunner",
    "Event",
    "LoopPhase",
    "NYSE_FULL_DAY_HOLIDAYS_2026",
    "SessionSchedule",
    "event_instant",
    "is_trading_day",
    "next_event",
    "phase_at",
]

ET = ZoneInfo("America/New_York")
SHANGHAI = ZoneInfo("Asia/Shanghai")
SEOUL = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

# --------------------------------------------------------------------- events


class Event(str, Enum):
    """Scheduled instants of the daily loop (Loop.md §4, times in ET)."""

    MORNING_REPORT = "MORNING_REPORT"  # 09:00 — overnight fills, ledger, summary
    MONITOR_START = "MONITOR_START"  # 09:30 — monitors poll, sub-agents build theses
    DECIDE_START = "DECIDE_START"  # 11:00 — decision core aggregates, risk validates
    PUSH_CANDIDATES = "PUSH_CANDIDATES"  # 11:30 — push approved candidates to Telegram
    CONFIRM_CUTOFF = "CONFIRM_CUTOFF"  # 12:30 — user confirmation window closes
    MARKET_CLOSE = "MARKET_CLOSE"  # 16:00 — MOC/LOC fill, resting GTC may fill


EVENT_TIMES_ET: Mapping[Event, time] = {
    Event.MORNING_REPORT: time(9, 0),
    Event.MONITOR_START: time(9, 30),
    Event.DECIDE_START: time(11, 0),
    Event.PUSH_CANDIDATES: time(11, 30),
    Event.CONFIRM_CUTOFF: time(12, 30),
    Event.MARKET_CLOSE: time(16, 0),
}

#: Events in the order they occur within a single trading day.
_EVENTS_CHRONOLOGICAL: tuple[Event, ...] = tuple(
    sorted(EVENT_TIMES_ET, key=lambda e: EVENT_TIMES_ET[e])
)


class LoopPhase(str, Enum):
    """Where the daily state machine sits right now (Loop.md §4)."""

    OFF_HOURS = "OFF_HOURS"  # non-trading day, or before 09:30 ET
    MONITORING = "MONITORING"  # 09:30–11:00 ET
    DECIDING = "DECIDING"  # 11:00–11:30 ET
    CONFIRM_WINDOW = "CONFIRM_WINDOW"  # 11:30–12:30 ET
    SET_AND_FORGET = "SET_AND_FORGET"  # 12:30–16:00 ET (user offline; orders rest)
    AFTER_CLOSE = "AFTER_CLOSE"  # 16:00–24:00 ET


# ------------------------------------------------------------------- calendar

# NYSE full-day holidays for 2026.
# Source: NYSE holidays & trading hours calendar
# (https://www.nyse.com/markets/hours-calendars), 2026 column.
# Independence Day 2026 (Jul 4) falls on a Saturday -> observed Friday Jul 3.
# TODO(2027): extend this table before the 2027 trading year begins; consider
# switching to a maintained calendar source (e.g. `pandas_market_calendars`)
# behind this same function.
NYSE_FULL_DAY_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day (Thu)
        date(2026, 1, 19),  # Martin Luther King, Jr. Day (Mon)
        date(2026, 2, 16),  # Washington's Birthday / Presidents' Day (Mon)
        date(2026, 4, 3),  # Good Friday
        date(2026, 5, 25),  # Memorial Day (Mon)
        date(2026, 6, 19),  # Juneteenth National Independence Day (Fri)
        date(2026, 7, 3),  # Independence Day, observed (Jul 4 is a Saturday)
        date(2026, 9, 7),  # Labor Day (Mon)
        date(2026, 11, 26),  # Thanksgiving Day (Thu)
        date(2026, 12, 25),  # Christmas Day (Fri)
    }
)

_MAX_SCAN_DAYS = 366  # next_event lookahead bound; > longest possible gap


# ---------------------------------------------------------------- sessions


@dataclass(frozen=True)
class SessionSchedule:
    """A named market session: timezone + event wall-times + holiday table.

    The daily loop is now MULTI-SESSION (Loop.md two-session extension): the
    US evening trading session and the China morning research session run in
    the same process, each with its own zone, calendar, and event offsets.
    Every public scheduler function takes an optional ``schedule`` defaulting
    to :data:`US_SCHEDULE`, so the original ET behaviour (and its tests) is
    unchanged; the CN session passes :data:`CN_SCHEDULE`.

    - ``event_times`` may hold a SUBSET of :class:`Event` (the CN research
      session only uses MONITOR_START / DECIDE_START / PUSH_CANDIDATES —
      it never executes orders, so it has no confirmation/close events).
    - ``phase_at`` maps the full 6-event US confirmation-window state machine
      and is only meaningful for a schedule that defines every event.
    """

    market_id: str
    tz: ZoneInfo
    event_times: Mapping[Event, time]
    holidays: frozenset[date]

    def events_chronological(self) -> tuple[Event, ...]:
        """Events defined for this session, ordered by wall-time."""
        return tuple(sorted(self.event_times, key=lambda e: self.event_times[e]))


#: US evening session (Loop.md §4): NYSE hours, ET, full six-event day.
US_SCHEDULE = SessionSchedule(
    market_id="US",
    tz=ET,
    event_times=EVENT_TIMES_ET,
    holidays=NYSE_FULL_DAY_HOLIDAYS_2026,
)

#: CN morning RESEARCH session (Asia/Shanghai): monitors 09:30 -> build 11:00
#: -> push a lighter research brief 11:30 (local). Report-only: NO confirmation
#: window, NO cutoff, NO close/execution events (Loop.md two-session extension:
#: "not place order in CN for now, but build the ability for future").
CN_EVENT_TIMES_LOCAL: Mapping[Event, time] = {
    Event.MONITOR_START: time(9, 30),
    Event.DECIDE_START: time(11, 0),
    Event.PUSH_CANDIDATES: time(11, 30),
}

# Approximate COMBINED mainland A-share (SSE/SZSE) + HKEX full-day closures
# for 2026 (weekends already handled). Report-only, so an imperfect entry only
# risks sending a brief over stale data (freshness-flagged) — never an order.
# TODO(calendar): replace with an authoritative SSE/HKEX 2026 calendar (and
# extend for 2027) before the CN session ever gains order authority.
CN_HK_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),  # New Year's Day (mainland + HK)
        date(2026, 2, 16),  # Spring Festival eve / CNY week (mainland)
        date(2026, 2, 17),  # Lunar New Year's Day
        date(2026, 2, 18),  # CNY (mainland + HK)
        date(2026, 2, 19),  # CNY (mainland + HK)
        date(2026, 2, 20),  # CNY (mainland)
        date(2026, 4, 3),  # Good Friday (HK)
        date(2026, 4, 6),  # Qingming / Easter Monday (mainland + HK)
        date(2026, 5, 1),  # Labour Day (mainland + HK)
        date(2026, 5, 25),  # Buddha's Birthday (HK)
        date(2026, 6, 19),  # Dragon Boat Festival (mainland + HK)
        date(2026, 9, 25),  # Mid-Autumn Festival (mainland)
        date(2026, 10, 1),  # National Day golden week (mainland)
        date(2026, 10, 2),  # National Day (mainland)
        date(2026, 10, 5),  # National Day (mainland)
        date(2026, 10, 6),  # National Day (mainland) / day after Mid-Autumn (HK)
        date(2026, 10, 7),  # National Day (mainland)
        date(2026, 12, 25),  # Christmas (HK)
    }
)

#: CN morning research session schedule.
CN_SCHEDULE = SessionSchedule(
    market_id="CN",
    tz=SHANGHAI,
    event_times=CN_EVENT_TIMES_LOCAL,
    holidays=CN_HK_HOLIDAYS_2026,
)

#: KR (KRX) research session (Asia/Seoul): monitor at the open (09:30 KST),
#: build near the close (14:30) and push at 15:00 — the near-full KR
#: semiconductor day is what leads/transfers to the CN tape (human directive
#: 2026-07-14). Report-only: NO confirmation window / cutoff / execution.
KR_EVENT_TIMES_LOCAL: Mapping[Event, time] = {
    Event.MONITOR_START: time(9, 30),
    Event.DECIDE_START: time(14, 30),
    Event.PUSH_CANDIDATES: time(15, 0),
}

# Best-effort KRX 2026 full-day WEEKDAY closures (weekends already handled).
# Report-only, so an imperfect entry only risks a stale-flagged brief, never an
# order. TODO(calendar): replace with an authoritative KRX 2026/2027 calendar
# before the KR session could ever gain order authority.
KR_HOLIDAYS_2026: frozenset[date] = frozenset(
    {
        date(2026, 1, 1),   # New Year's Day
        date(2026, 2, 16),  # Seollal (Lunar New Year) holiday
        date(2026, 2, 17),  # Seollal
        date(2026, 2, 18),  # Seollal
        date(2026, 3, 2),   # Independence Movement Day (Mar 1 Sun → substitute)
        date(2026, 5, 1),   # Labour Day (KRX closed)
        date(2026, 5, 5),   # Children's Day
        date(2026, 5, 25),  # Buddha's Birthday (May 24 Sun → substitute)
        date(2026, 9, 24),  # Chuseok holiday
        date(2026, 9, 25),  # Chuseok
        date(2026, 9, 28),  # Chuseok (Sep 26 Sat → substitute)
        date(2026, 10, 9),  # Hangul Day
        date(2026, 12, 25),  # Christmas
        date(2026, 12, 31),  # Year-end market closure
    }
)

#: KR afternoon research session schedule.
KR_SCHEDULE = SessionSchedule(
    market_id="KR",
    tz=SEOUL,
    event_times=KR_EVENT_TIMES_LOCAL,
    holidays=KR_HOLIDAYS_2026,
)


def is_trading_day(d: date, schedule: SessionSchedule = US_SCHEDULE) -> bool:
    """True when ``schedule``'s market is open on ``d``.

    Mon–Fri minus that session's full-day holidays. (US half days count as
    trading days — see module docstring.)
    """
    return d.weekday() < 5 and d not in schedule.holidays


# --------------------------------------------------------------------- helpers


def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        raise ValueError(f"{name} must be timezone-aware (UTC); got naive {dt!r}")


def event_instant(
    d: date, event: Event, schedule: SessionSchedule = US_SCHEDULE
) -> datetime:
    """UTC instant at which ``event`` occurs on ``schedule``'s calendar day ``d``.

    Pure wall-time -> UTC conversion; does NOT check ``is_trading_day``.
    US event times (09:00–16:00 ET) and CN event times (09:30–11:30 CST) are
    never ambiguous/nonexistent under DST — the US transitions happen at 02:00
    local and China observes no DST — so no fold handling is needed.
    """
    return datetime.combine(
        d, schedule.event_times[event], tzinfo=schedule.tz
    ).astimezone(UTC)


# ------------------------------------------------------------------ phase math


def phase_at(
    now_utc: datetime, schedule: SessionSchedule = US_SCHEDULE
) -> LoopPhase:
    """Map a UTC instant onto the Loop.md §4 confirmation-window state machine.

    Boundaries are half-open ``[start, end)``: 09:30:00 ET is already
    MONITORING, 11:00:00 is DECIDING, and so on. Non-trading days (weekends
    and full-day holidays) are OFF_HOURS all day, as is 00:00–09:30 ET on a
    trading day. Only meaningful for a schedule that defines the full six-event
    US day (the CN research session has no confirmation window).
    """
    _require_aware(now_utc, "now_utc")
    times = schedule.event_times
    local = now_utc.astimezone(schedule.tz)
    if not is_trading_day(local.date(), schedule):
        return LoopPhase.OFF_HOURS
    t = local.time()
    if t < times[Event.MONITOR_START]:
        return LoopPhase.OFF_HOURS
    if t < times[Event.DECIDE_START]:
        return LoopPhase.MONITORING
    if t < times[Event.PUSH_CANDIDATES]:
        return LoopPhase.DECIDING
    if t < times[Event.CONFIRM_CUTOFF]:
        return LoopPhase.CONFIRM_WINDOW
    if t < times[Event.MARKET_CLOSE]:
        return LoopPhase.SET_AND_FORGET
    return LoopPhase.AFTER_CLOSE


def next_event(
    now_utc: datetime, schedule: SessionSchedule = US_SCHEDULE
) -> tuple[Event, datetime]:
    """The next upcoming ``(event, utc_instant)`` strictly after ``now_utc``.

    Non-trading days are skipped entirely — MORNING_REPORT also fires only on
    trading days (there is nothing to report into a closed market, and the
    previous session's report already ran). An event exactly at ``now_utc``
    is considered fired/firing NOW, so the *following* one is returned. Only
    events defined by ``schedule`` are considered.
    """
    _require_aware(now_utc, "now_utc")
    events = schedule.events_chronological()
    d = now_utc.astimezone(schedule.tz).date()
    for _ in range(_MAX_SCAN_DAYS):
        if is_trading_day(d, schedule):
            for event in events:
                instant = event_instant(d, event, schedule)
                if instant > now_utc:
                    return event, instant
        d += timedelta(days=1)
    raise RuntimeError(  # pragma: no cover - unreachable with a sane calendar
        f"no trading day within {_MAX_SCAN_DAYS} days of {now_utc.isoformat()}; "
        "holiday table exhausted? (see TODO(2027) in scheduler.py)"
    )


# --------------------------------------------------------------------- runner


class DailyLoopRunner:
    """Fires each :class:`Event`'s callback exactly once per trading day.

    Pure polling core: no threads, no sleeps — the caller invokes
    :meth:`run_pending` as often as it likes (APScheduler, a cron job, or a
    dumb ``while`` loop via :meth:`run_forever`). Missed events are caught up
    in chronological order, so a runner polled at 12:00 ET after being idle
    since 08:00 fires MORNING_REPORT, MONITOR_START, DECIDE_START and
    PUSH_CANDIDATES back to back.

    - ``callbacks``: zero-argument callables keyed by :class:`Event`. Events
      without a callback still advance the schedule (and are reported in the
      return value of :meth:`run_pending`), they just invoke nothing.
    - ``clock``: zero-argument callable returning the current tz-aware UTC
      time. Injectable for deterministic tests (Loop.md §3, §9).

    Exactly-once is tracked per ``(event, ET-date)``; a callback that raises
    is logged and counted as fired (at-most-once — a crashing handler must
    not be retried into a double order push), and later events still run.
    Events whose instant is at or before construction time never fire.
    """

    def __init__(
        self,
        callbacks: Mapping[Event, Callable[[], None]],
        clock: Callable[[], datetime],
        schedule: SessionSchedule = US_SCHEDULE,
    ) -> None:
        for key in callbacks:
            if not isinstance(key, Event):
                raise TypeError(f"callback key must be an Event, got {key!r}")
        self._callbacks: dict[Event, Callable[[], None]] = dict(callbacks)
        self._clock = clock
        self._schedule = schedule
        start = clock()
        _require_aware(start, "clock()")
        self._watermark: datetime = start.astimezone(UTC)
        self._fired: set[tuple[Event, date]] = set()

    def run_pending(self, now: Optional[datetime] = None) -> list[tuple[Event, datetime]]:
        """Fire every event whose instant passed since the last firing.

        Returns the ``(event, utc_instant)`` pairs fired on this call, in
        chronological order (empty list when nothing was due). An event whose
        instant equals ``now`` exactly DOES fire.
        """
        if now is None:
            now = self._clock()
        _require_aware(now, "now")
        now = now.astimezone(UTC)

        fired: list[tuple[Event, datetime]] = []
        cursor = self._watermark
        while True:
            event, instant = next_event(cursor, self._schedule)
            if instant > now:
                break
            key = (event, instant.astimezone(self._schedule.tz).date())
            if key not in self._fired:
                self._fired.add(key)  # before the call: at-most-once even on raise
                callback = self._callbacks.get(event)
                if callback is not None:
                    try:
                        callback()
                    except Exception:
                        logger.exception(
                            "scheduler callback raised; event marked fired",
                            extra={"event": event.value, "instant": instant.isoformat()},
                        )
                fired.append((event, instant))
                logger.info(
                    "scheduler event fired",
                    extra={
                        "event": event.value,
                        "instant": instant.isoformat(),
                        "had_callback": callback is not None,
                    },
                )
            cursor = instant
        if now > self._watermark:
            self._watermark = now
        return fired

    def run_forever(
        self,
        sleep_fn: Callable[[float], None] = _default_sleep,
        poll_seconds: float = 30.0,
    ) -> None:
        """Convenience loop: ``run_pending`` then ``sleep_fn(poll_seconds)``.

        Never returns on its own; the injected ``sleep_fn`` (or a signal /
        KeyboardInterrupt) is the only way out. Kept trivial on purpose —
        all logic lives in the pure :meth:`run_pending`.
        """
        while True:
            self.run_pending()
            sleep_fn(poll_seconds)
