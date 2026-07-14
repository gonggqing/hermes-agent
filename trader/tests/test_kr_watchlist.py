"""Tests for the KR semiconductor research universe + schedule (Loop.md
two-session extension; human directive 2026-07-14: KR semi sentiment leads CN)."""

from __future__ import annotations

from datetime import date

from swing_trader.kr_watchlist import (
    KR_INDEX_SYMBOLS,
    KR_UNIVERSE,
    build_kr_watchlist,
)
from swing_trader.scheduler import KR_SCHEDULE, SEOUL, Event, is_trading_day
from swing_trader.schemas import AiPhase, Role


class TestKrUniverse:
    def test_semiconductor_giants_present(self):
        syms = {i.symbol for i in KR_UNIVERSE}
        assert "005930.KS" in syms  # Samsung Electronics
        assert "000660.KS" in syms  # SK Hynix

    def test_all_semiconductor_focused(self):
        # Narrow by directive: every name is a semi theme, no off-topic sectors.
        assert all(i.theme.startswith("kr-") for i in KR_UNIVERSE)
        assert all("memory" in i.theme or "hbm" in i.theme or "foundry" in i.theme
                   for i in KR_UNIVERSE)

    def test_giants_are_conviction_memory(self):
        giants = [i for i in KR_UNIVERSE if i.theme == "kr-memory-giant"]
        assert len(giants) == 2
        assert all(i.role is Role.CONVICTION and i.ai_phase is AiPhase.MEMORY
                   for i in giants)

    def test_index_is_kospi(self):
        assert KR_INDEX_SYMBOLS == ("^KS11",)


class TestBuildKrWatchlist:
    def test_default_universe(self):
        wl = build_kr_watchlist()
        assert wl.symbols == [i.symbol for i in KR_UNIVERSE]
        assert wl.lookup("005930.KS").theme == "kr-memory-giant"
        assert wl.lookup("nope") is None

    def test_override_restricts_and_tags_unknown(self):
        wl = build_kr_watchlist("005930.KS, 999999.KS")
        assert wl.symbols == ["005930.KS", "999999.KS"]
        assert wl.lookup("005930.KS").theme == "kr-memory-giant"  # known tag kept
        assert wl.lookup("999999.KS").theme == "kr-custom"        # unknown tagged

    def test_override_is_case_insensitive(self):
        wl = build_kr_watchlist("005930.ks")
        assert wl.lookup("005930.KS") is not None


class TestKrSchedule:
    def test_seoul_tz_and_research_events(self):
        assert KR_SCHEDULE.tz == SEOUL and KR_SCHEDULE.market_id == "KR"
        # research-only: monitor/decide/push, no confirmation/close events
        assert set(KR_SCHEDULE.event_times) == {
            Event.MONITOR_START, Event.DECIDE_START, Event.PUSH_CANDIDATES}

    def test_push_is_near_kr_close(self):
        assert KR_SCHEDULE.event_times[Event.PUSH_CANDIDATES].hour == 15

    def test_holidays_exclude_new_year(self):
        assert not is_trading_day(date(2026, 1, 1), KR_SCHEDULE)  # New Year
        assert is_trading_day(date(2026, 1, 2), KR_SCHEDULE)      # normal Friday
