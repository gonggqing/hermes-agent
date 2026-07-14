"""CN morning research session + two-session scheduler + two-bot split
(Loop.md two-session extension).

Fully deterministic: in-test fakes implement the DataFeed; the injected clock
drives the scheduler; nothing touches the network or real time (Loop.md §3).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from swing_trader.brief import build_research_brief
from swing_trader.brief_telegram import render_research_brief
from swing_trader.cn_watchlist import CN_UNIVERSE, build_cn_watchlist
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.ledger import Ledger
from swing_trader.research_session import ResearchSession
from swing_trader.scheduler import (
    CN_SCHEDULE,
    US_SCHEDULE,
    DailyLoopRunner,
    Event,
    event_instant,
    is_trading_day,
    next_event,
)
from swing_trader.schemas import Mode

SH = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


# --------------------------------------------------------------------- fakes


class FakeFeed(DataFeed):
    def __init__(self, bars=None, quotes=None, news=None):
        self.bars = bars or {}
        self.quotes = quotes or {}
        self.news = news or {}

    def get_quote(self, symbol: str) -> Quote:
        if symbol not in self.quotes:
            raise DataFeedError(f"no quote for {symbol}")
        return self.quotes[symbol]

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100):
        if symbol not in self.bars:
            raise DataFeedError(f"no bars for {symbol}")
        return self.bars[symbol][-limit:]

    def get_news(self, symbol: Optional[str] = None, limit: int = 20):
        return self.news.get(symbol, [])[:limit]


def rising_bars(symbol: str, n: int = 90, start: float = 10.0, step: float = 0.15):
    base = datetime(2026, 3, 1, tzinfo=UTC)
    out = []
    for i in range(n):
        c = start + step * i
        out.append(Bar(symbol=symbol, ts=base + timedelta(days=i),
                       open=c - 0.05, high=c + 0.1, low=c - 0.1, close=c,
                       volume=5_000_000.0))
    return out


def make_cn_feed():
    syms = ["0700.HK", "0981.HK", "9988.HK"]
    bars = {s: rising_bars(s) for s in syms}
    news = {
        "0700.HK": [NewsItem(symbol="0700.HK", ts=datetime(2026, 7, 13, tzinfo=UTC),
                             headline="Tencent beats on strong cloud growth",
                             source="Reuters", url="https://x/1", sentiment=0.8)],
    }
    return FakeFeed(bars=bars, news=news)


# ---------------------------------------------------------------- scheduler


def test_cn_schedule_trading_days_and_holidays():
    # A plain CN weekday is a trading day; weekend and a CNY holiday are not.
    assert is_trading_day(date(2026, 7, 13), CN_SCHEDULE)  # Monday
    assert not is_trading_day(date(2026, 7, 11), CN_SCHEDULE)  # Saturday
    assert not is_trading_day(date(2026, 2, 18), CN_SCHEDULE)  # CNY
    # The US schedule is unaffected: 2026-02-18 IS a US trading day.
    assert is_trading_day(date(2026, 2, 18), US_SCHEDULE)


def test_cn_event_instant_is_shanghai_local():
    # 09:30 Shanghai == 01:30 UTC (China has no DST).
    inst = event_instant(date(2026, 7, 13), Event.MONITOR_START, CN_SCHEDULE)
    assert inst == datetime(2026, 7, 13, 1, 30, tzinfo=UTC)
    push = event_instant(date(2026, 7, 13), Event.PUSH_CANDIDATES, CN_SCHEDULE)
    assert push == datetime(2026, 7, 13, 3, 30, tzinfo=UTC)


def test_cn_next_event_has_only_research_events():
    start = datetime(2026, 7, 13, 0, 0, tzinfo=UTC)
    seen = []
    cursor = start
    for _ in range(3):
        ev, inst = next_event(cursor, CN_SCHEDULE)
        seen.append(ev)
        cursor = inst
    assert seen == [Event.MONITOR_START, Event.DECIDE_START, Event.PUSH_CANDIDATES]
    # No confirmation/close events exist for the research session.
    ev4, _ = next_event(cursor, CN_SCHEDULE)
    assert ev4 == Event.MONITOR_START  # next CN trading day, not CONFIRM_CUTOFF


def test_cn_runner_fires_three_callbacks_in_order():
    fired: list[Event] = []
    now = {"t": datetime(2026, 7, 13, 0, 0, tzinfo=UTC)}
    runner = DailyLoopRunner(
        {
            Event.MONITOR_START: lambda: fired.append(Event.MONITOR_START),
            Event.DECIDE_START: lambda: fired.append(Event.DECIDE_START),
            Event.PUSH_CANDIDATES: lambda: fired.append(Event.PUSH_CANDIDATES),
        },
        clock=lambda: now["t"],
        schedule=CN_SCHEDULE,
    )
    now["t"] = datetime(2026, 7, 13, 4, 0, tzinfo=UTC)  # past all CN morning events
    runner.run_pending()
    assert fired == [Event.MONITOR_START, Event.DECIDE_START, Event.PUSH_CANDIDATES]
    runner.run_pending()  # exactly-once per day
    assert fired == [Event.MONITOR_START, Event.DECIDE_START, Event.PUSH_CANDIDATES]


# ---------------------------------------------------------------- watchlist


def test_cn_watchlist_default_and_lookup():
    wl = build_cn_watchlist("")
    assert wl.symbols == [i.symbol for i in CN_UNIVERSE]
    item = wl.lookup("0981.HK")
    assert item is not None and item.theme == "cn-semiconductor"
    assert wl.lookup("nope") is None


def test_cn_watchlist_override_tags_unknown_custom():
    wl = build_cn_watchlist("0700.HK, 9999.HK")
    assert wl.symbols == ["0700.HK", "9999.HK"]
    assert wl.lookup("0700.HK").theme == "cn-internet-platform"  # known tag kept
    assert wl.lookup("9999.HK").theme == "cn-custom"  # unknown tagged


# ------------------------------------------------------------------- brief


def test_brief_cn_params_no_account_cn_date_cn_tags():
    from swing_trader.monitors import PortfolioSnapshot, WatchState

    wl = build_cn_watchlist("")
    watch = {
        "0981.HK": WatchState(last=110.0, sma20=100.0, sma50=95.0),
        "0700.HK": WatchState(last=90.0, sma20=100.0, sma50=105.0),
    }
    portfolio = PortfolioSnapshot(ts=datetime(2026, 7, 13, 3, 0, tzinfo=UTC), watch=watch)
    ledger = Ledger(url="sqlite:///:memory:")
    now = datetime(2026, 7, 13, 3, 0, tzinfo=UTC)  # 11:00 Shanghai
    brief = build_research_brief(
        ledger, Mode.PAPER,
        portfolio=portfolio,
        now=now,
        signals=[], candidates=[],
        watchlist_lookup=wl.lookup,
        trading_tz=SH,
        include_account=False,
        extra_uncertainty=["China / HK session is RESEARCH-ONLY"],
    )
    # CN trading date (not ET, which would be 2026-07-12).
    assert brief.trading_date == "2026-07-13"
    # No account/positions in a research-only brief.
    assert brief.risk is None
    # Movers carry CN theme tags via the CN lookup.
    tags = {m.symbol: m.theme for m in brief.movers.top + brief.movers.bottom}
    assert tags.get("0981.HK") == "cn-semiconductor"
    assert any("RESEARCH-ONLY" in u for u in brief.uncertainty)


# --------------------------------------------------------------- brief text


def test_render_research_brief_zh_and_en():
    from swing_trader.monitors import PortfolioSnapshot, WatchState

    wl = build_cn_watchlist("")
    watch = {"0981.HK": WatchState(last=110.0, sma20=100.0, sma50=95.0)}
    portfolio = PortfolioSnapshot(ts=datetime(2026, 7, 13, 3, 0, tzinfo=UTC), watch=watch)
    brief = build_research_brief(
        Ledger(url="sqlite:///:memory:"), Mode.PAPER, portfolio=portfolio,
        now=datetime(2026, 7, 13, 3, 0, tzinfo=UTC),
        signals=[], candidates=[], watchlist_lookup=wl.lookup,
        trading_tz=SH, include_account=False,
    )
    zh = render_research_brief(brief, market_label="China / HK", focus_note="聚焦科技")
    assert "投资研究简报" in zh and "China / HK" in zh and "2026-07-13" in zh
    assert "聚焦科技" in zh
    en = render_research_brief(brief, market_label="China / HK", lang="en")
    assert "Investment Research Brief" in en
    # Clamped under the Telegram cap.
    assert len(render_research_brief(brief, market_label="X", max_chars=50)) <= 50


# ---------------------------------------------------------------- session


def _make_session(feed, runtime=None, sent=None, ledger=None):
    wl = build_cn_watchlist("0700.HK,0981.HK,9988.HK")
    return ResearchSession(
        market_id="CN",
        market_label="China / HK",
        feed=feed,
        ledger=ledger or Ledger(url="sqlite:///:memory:"),
        symbols=wl.symbols,
        watchlist_lookup=wl.lookup,
        trading_tz=SH,
        index_symbols=[],
        mode=Mode.PAPER,
        runtime=runtime,
        notify=(sent.append if sent is not None else None),
        focus_note="聚焦科技",
        clock=lambda: datetime(2026, 7, 13, 3, 0, tzinfo=UTC),
    )


def test_research_session_publishes_brief_and_sends():
    from swing_trader.api import FinanceRuntime

    runtime = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER)
    sent: list[str] = []
    session = _make_session(make_cn_feed(), runtime=runtime, sent=sent)

    session.on_monitor()
    session.on_research()
    session.on_send()

    # Brief exposed to the API (?market=cn) and pushed by the REPORTER bot.
    assert runtime.latest_brief_cn
    assert runtime.latest_brief_cn["trading_date"] == "2026-07-13"
    assert runtime.latest_brief_cn["risk"] is None  # research-only
    assert len(sent) == 1 and "China / HK" in sent[0]


def test_research_session_run_now_refreshes_brief():
    from swing_trader.api import FinanceRuntime

    runtime = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER)
    sent: list[str] = []
    session = _make_session(make_cn_feed(), runtime=runtime, sent=sent)

    summary = session.run_now()  # off-schedule manual refresh (no push)
    assert summary["market"] == "CN" and summary["brief_ready"] is True
    assert summary["sent"] is False
    assert runtime.latest_briefs["cn"]  # per-market slot populated
    assert runtime.latest_brief_cn  # CN back-compat slot too
    assert sent == []  # send=False → no group push

    summary2 = session.run_now(send=True)  # refresh AND push
    assert summary2["sent"] is True
    assert len(sent) == 1 and "China / HK" in sent[0]


def test_movers_carry_region_cn_vs_hk():
    from swing_trader.api import FinanceRuntime

    runtime = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER)
    session = _make_session(make_cn_feed(), runtime=runtime)  # 0700.HK/0981.HK/9988.HK
    session.run_now()
    brief = runtime.latest_briefs["cn"]
    movers = brief["movers"]["top"] + brief["movers"]["bottom"]
    assert movers  # some movers present
    assert all(m["region"] == "HK" for m in movers)  # all .HK in this fixture


def test_research_session_skips_empty_push_on_mid_day_restart():
    # If on_monitor/on_research never ran (service restarted AFTER the CN
    # events already passed), on_send must NOT push a contentless brief to the
    # group — but it still refreshes the (degraded) API brief.
    from swing_trader.api import FinanceRuntime

    runtime = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER)
    sent: list[str] = []
    session = _make_session(make_cn_feed(), runtime=runtime, sent=sent)
    session.on_send()  # no monitors ran first
    assert sent == []  # nothing pushed to the group
    assert runtime.latest_brief_cn  # but the API brief is still refreshed


def test_research_session_never_touches_trading_ledger():
    # Report-only: the session must not record signals/candidates/orders into
    # the (shared) trading ledger — CN research stays out of it entirely.
    ledger = Ledger(url="sqlite:///:memory:")
    session = _make_session(make_cn_feed(), ledger=ledger)
    session.on_monitor()
    session.on_research()
    session.on_send()
    assert ledger.get_signals(mode=Mode.PAPER) == []
    assert ledger.get_candidates(mode=Mode.PAPER) == []
    assert ledger.get_orders(mode=Mode.PAPER) == []


def test_research_session_survives_broken_feed():
    # Every symbol fails to load -> no signals, but the brief still publishes
    # (degraded) and the session never raises.
    from swing_trader.api import FinanceRuntime

    runtime = FinanceRuntime(ledger=Ledger(url="sqlite:///:memory:"), mode=Mode.PAPER)
    sent: list[str] = []
    session = _make_session(FakeFeed(), runtime=runtime, sent=sent)  # empty feed
    session.on_monitor()
    session.on_research()
    session.on_send()
    assert runtime.latest_brief_cn  # still produced a brief
    assert len(sent) == 1


# --------------------------------------------------------------- bot split


def test_gatekeeper_pushes_cards_without_preamble():
    from swing_trader.dailyloop import TelegramSurfaceAdapter

    class MockTransport:
        def __init__(self):
            self.messages: list[tuple[str, str]] = []

        def send_message(self, chat_id, text, reply_markup=None):
            self.messages.append((chat_id, text))
            return {"ok": True}

        def get_updates(self, offset=None):
            return []

        def answer_callback(self, cb_id, text=""):
            return {"ok": True}

    from swing_trader.schemas import (
        CandidateOrder, OrderType, Side, TimeInForce,
    )

    cand = CandidateOrder(
        symbol="NVDA", side=Side.BUY, qty=1, order_type=OrderType.BRACKET,
        limit=100.0, stop=95.0, tp=110.0, tif=TimeInForce.GTC,
        rationale="t", confidence=0.7,
    )
    transport = MockTransport()
    adapter = TelegramSurfaceAdapter(transport, "chat", interactive=True,
                                     allowed_users={"gongqing"})
    adapter.push_cards([cand])  # gatekeeper: cards only, NO preamble
    assert len(transport.messages) == 1  # exactly the one card, no preamble line
    assert "NVDA" in transport.messages[0][1]
