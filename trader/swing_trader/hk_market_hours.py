"""HKEX securities-market intraday hours — Hong Kong session microstructure
(Loop.md §5.7 / docs/finance-hk-ibkr-rollout.md: "Use the Hong Kong trading
calendar and Asia/Hong_Kong sessions, including the lunch break, auctions,
holidays and half-days").

Unlike NYSE (one continuous 09:30–16:00 session), the HKEX securities market
has a **lunch break** and **auction** sessions around the continuous phases:

    09:00–09:30  Pre-opening auction (POS)
    09:30–12:00  Morning continuous trading
    12:00–13:00  Lunch break (market closed to continuous trading)
    13:00–16:00  Afternoon continuous trading
    16:00–16:10  Closing auction (CAS)

The daily loop needs this to answer "is an HK limit order live right now?" and
to flag stale quotes during the lunch break — a US ``09:30–16:00`` assumption
would treat the lunch hour as tradable. Full-day holidays come from the
scheduler's ``HK_HOLIDAYS_2026`` table; half-day (early-close) sessions that end
at 12:00 are a TODO, mirroring the US half-day handling.

Pure ``zoneinfo`` wall-time math with an injected UTC instant — no wall clock,
no network (Loop.md §3).
"""

from __future__ import annotations

from datetime import datetime, time
from enum import Enum

from swing_trader.scheduler import HK_SCHEDULE, HONG_KONG, is_trading_day

__all__ = [
    "HKPhase",
    "hk_phase",
    "is_hk_continuous_trading",
    "is_hk_order_acceptable",
    "is_hk_lunch_break",
]


class HKPhase(str, Enum):
    """Where the HKEX securities market sits at a given instant."""

    CLOSED = "CLOSED"  # outside all sessions, or a non-trading day
    PRE_AUCTION = "PRE_AUCTION"  # 09:00–09:30 pre-opening auction
    MORNING = "MORNING"  # 09:30–12:00 continuous
    LUNCH = "LUNCH"  # 12:00–13:00 break
    AFTERNOON = "AFTERNOON"  # 13:00–16:00 continuous
    CLOSING_AUCTION = "CLOSING_AUCTION"  # 16:00–16:10 CAS


# HKEX securities-market session boundaries (Asia/Hong_Kong wall time).
_PRE_OPEN = time(9, 0)
_MORNING_OPEN = time(9, 30)
_LUNCH_START = time(12, 0)
_LUNCH_END = time(13, 0)
_AFTERNOON_CLOSE = time(16, 0)
_CAS_END = time(16, 10)


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError(f"now must be timezone-aware; got naive {now!r}")


def hk_phase(now_utc: datetime) -> HKPhase:
    """Return the HKEX securities-market :class:`HKPhase` at ``now_utc``.

    Boundaries are half-open ``[start, end)``: 09:30:00 is already MORNING,
    12:00:00 is LUNCH, 16:00:00 is CLOSING_AUCTION. A non-trading day (weekend
    or full-day HK holiday) is CLOSED all day.
    """
    _require_aware(now_utc)
    local = now_utc.astimezone(HONG_KONG)
    if not is_trading_day(local.date(), HK_SCHEDULE):
        return HKPhase.CLOSED
    t = local.time()
    if t < _PRE_OPEN:
        return HKPhase.CLOSED
    if t < _MORNING_OPEN:
        return HKPhase.PRE_AUCTION
    if t < _LUNCH_START:
        return HKPhase.MORNING
    if t < _LUNCH_END:
        return HKPhase.LUNCH
    if t < _AFTERNOON_CLOSE:
        return HKPhase.AFTERNOON
    if t < _CAS_END:
        return HKPhase.CLOSING_AUCTION
    return HKPhase.CLOSED


def is_hk_continuous_trading(now_utc: datetime) -> bool:
    """True only during continuous trading (morning or afternoon) — the phases
    in which an ordinary limit order can fill. Auctions and lunch return False."""
    return hk_phase(now_utc) in (HKPhase.MORNING, HKPhase.AFTERNOON)


def is_hk_lunch_break(now_utc: datetime) -> bool:
    """True during the 12:00–13:00 lunch break (continuous trading suspended)."""
    return hk_phase(now_utc) is HKPhase.LUNCH


def is_hk_order_acceptable(now_utc: datetime) -> bool:
    """True when HKEX accepts order input: continuous trading OR either auction.

    (The pre-opening and closing auctions accept order entry even though no
    continuous matching occurs.) Lunch break and outside-hours return False.
    """
    return hk_phase(now_utc) in (
        HKPhase.PRE_AUCTION,
        HKPhase.MORNING,
        HKPhase.AFTERNOON,
        HKPhase.CLOSING_AUCTION,
    )
