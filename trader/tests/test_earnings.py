"""Earnings/events calendar (Loop.md Phase 0.75) — provider + brief wiring."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from swing_trader.brief import EARNINGS_NOT_WIRED_NOTE, build_research_brief
from swing_trader.earnings import YFinanceEarnings, upcoming_earnings
from swing_trader.ledger import Ledger
from swing_trader.schemas import Mode

UTC = timezone.utc
NOW = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)  # ET noon 2026-07-13 -> today 07-13


def _fn(dates, calls=None):
    def f(symbol):
        if calls is not None:
            calls.append(symbol)
        d = dates.get(symbol.upper())
        if isinstance(d, Exception):
            raise d
        return d
    return f


# ------------------------------------------------------------------- provider

def test_get_next_earnings_and_cache():
    calls: list[str] = []
    prov = YFinanceEarnings(next_earnings_fn=_fn({"NVDA": date(2026, 7, 15)}, calls),
                            clock=lambda: NOW)
    assert prov.get_next_earnings("nvda") == date(2026, 7, 15)
    assert prov.get_next_earnings("NVDA") == date(2026, 7, 15)
    assert calls == ["nvda"]  # second call served from cache


def test_provider_fail_none():
    prov = YFinanceEarnings(next_earnings_fn=_fn({"X": RuntimeError("net")}),
                            clock=lambda: NOW)
    assert prov.get_next_earnings("X") is None
    prov2 = YFinanceEarnings(next_earnings_fn=_fn({"X": "notadate"}), clock=lambda: NOW)
    assert prov2.get_next_earnings("X") is None


# --------------------------------------------------------------- upcoming list

def test_upcoming_earnings_filters_sorts_flags():
    prov = YFinanceEarnings(next_earnings_fn=_fn({
        "NVDA": date(2026, 7, 15),   # +2d, imminent
        "MSFT": date(2026, 7, 25),   # +12d
        "AAPL": date(2026, 7, 1),    # past -> dropped
        "TSLA": None,                # unknown -> dropped
    }), clock=lambda: NOW)
    evs = upcoming_earnings(prov, ["NVDA", "MSFT", "AAPL", "TSLA"],
                            now=NOW, within_days=14)
    assert [e.symbol for e in evs] == ["NVDA", "MSFT"]  # sorted by days_until
    assert evs[0].days_until == 2 and evs[0].imminent is True
    assert evs[1].days_until == 12 and evs[1].imminent is False
    # within_days trims MSFT
    near = upcoming_earnings(prov, ["NVDA", "MSFT"], now=NOW, within_days=5)
    assert [e.symbol for e in near] == ["NVDA"]


# ------------------------------------------------------------------- brief

def _brief(earnings):
    return build_research_brief(
        Ledger(url="sqlite:///:memory:"), Mode.PAPER, now=NOW,
        signals=[], candidates=[], include_account=False, earnings=earnings,
    )


def test_brief_earnings_populated_and_warns_imminent():
    prov = YFinanceEarnings(next_earnings_fn=_fn({"NVDA": date(2026, 7, 15)}),
                            clock=lambda: NOW)
    evs = upcoming_earnings(prov, ["NVDA"], now=NOW, within_days=14)
    brief = _brief(evs)
    assert [e["symbol"] for e in brief.events.earnings] == ["NVDA"]
    assert any("earnings imminent" in u for u in brief.uncertainty)
    assert EARNINGS_NOT_WIRED_NOTE not in brief.uncertainty  # feed IS wired now


def test_brief_empty_earnings_note():
    brief = _brief([])  # provider wired but nothing upcoming
    assert brief.events.earnings == []
    assert any("no upcoming earnings" in n for n in brief.events.notes)
    assert EARNINGS_NOT_WIRED_NOTE not in brief.uncertainty


def test_brief_no_earnings_keeps_not_wired_note():
    brief = _brief(None)  # backward compat: no provider
    assert EARNINGS_NOT_WIRED_NOTE in brief.uncertainty
