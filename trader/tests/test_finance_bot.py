"""Finance bot (gatekeeper) @mention/DM reply gating + on-demand analysis
(Loop.md Phase 0.75). The finance bot stays quiet in the group except for
confirmations, and replies ONLY on a DM or an @mention — allowlist-gated.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from swing_trader.dailyloop import TelegramSurfaceAdapter
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.on_demand import analyze_symbol, extract_symbols, render_analysis_zh

UTC = timezone.utc
T0 = datetime(2026, 3, 1, tzinfo=UTC)


class MockTransport:
    def __init__(self, updates=None, username="financebot"):
        self.sent: list[tuple[str, str]] = []
        self._updates = list(updates or [])
        self._username = username
        self.me_calls = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((str(chat_id), text))
        return 1

    def get_updates(self, offset=None):
        u, self._updates = self._updates, []
        return u

    def answer_callback(self, cb_id, text=""):
        pass

    def get_me(self):
        self.me_calls += 1
        return {"id": 42, "username": self._username}


def _msg(text, chat_type="private", chat_id=99, user="gongqing", uid=7):
    return {"update_id": 1, "message": {
        "text": text, "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": uid, "username": user}}}


def _adapter(transport, respond=lambda t: f"echo:{t}", allowed={"gongqing"}):
    return TelegramSurfaceAdapter(transport, "99", interactive=True,
                                  allowed_users=allowed, respond_text=respond)


# ---------------------------------------------------------------- gating

def test_dm_from_allowed_user_gets_reply():
    tr = MockTransport([_msg("analyze NVDA", chat_type="private")])
    _adapter(tr).poll(None, datetime.now(UTC))
    assert tr.sent and tr.sent[0][1] == "echo:analyze NVDA"


def test_group_mention_triggers_and_strips_mention():
    tr = MockTransport([_msg("@financebot NVDA please", chat_type="group")])
    _adapter(tr).poll(None, datetime.now(UTC))
    assert tr.sent and tr.sent[0][1] == "echo:NVDA please"  # @mention stripped


def test_group_message_without_mention_is_ignored():
    tr = MockTransport([_msg("random chatter about NVDA", chat_type="group")])
    _adapter(tr).poll(None, datetime.now(UTC))
    assert tr.sent == []  # finance bot stays quiet in the group


def test_unauthorized_user_gets_no_reply():
    tr = MockTransport([_msg("hi", chat_type="private", user="stranger", uid=999)])
    _adapter(tr).poll(None, datetime.now(UTC))
    assert tr.sent == []


def test_no_responder_means_silent():
    tr = MockTransport([_msg("analyze NVDA", chat_type="private")])
    TelegramSurfaceAdapter(tr, "99", interactive=True,
                           allowed_users={"gongqing"}).poll(None, datetime.now(UTC))
    assert tr.sent == []


def test_identity_cached_across_polls():
    # first poll (a group message) identifies the bot; a second poll must not
    # re-call getMe.
    tr = MockTransport([_msg("hello group", chat_type="group")])
    a = _adapter(tr)
    a.poll(None, datetime.now(UTC))
    tr._updates = [_msg("@financebot NVDA", chat_type="group")]
    a.poll(None, datetime.now(UTC))
    assert tr.me_calls == 1


# ---------------------------------------------------------------- on_demand

def test_extract_symbols_filters_stopwords():
    assert extract_symbols("analyze NVDA and 0700.HK now") == ["NVDA", "0700.HK"]
    assert extract_symbols("the AI stock is great") == []  # THE/AI filtered
    assert extract_symbols("check 600519.SS") == ["600519.SS"]


class _Feed(DataFeed):
    def __init__(self):
        base = [Bar(symbol="NVDA", ts=T0 + timedelta(days=i), open=c - 0.2,
                    high=c + 0.3, low=c - 0.3, close=c, volume=5e6)
                for i, c in enumerate(100 + 0.5 * i for i in range(90))]
        self._bars = {"NVDA": base}

    def get_quote(self, symbol):
        if symbol not in self._bars:
            raise DataFeedError("no quote")
        return Quote(symbol=symbol, ts=T0, last=self._bars[symbol][-1].close)

    def get_bars(self, symbol, timeframe="1d", limit=100):
        if symbol not in self._bars:
            raise DataFeedError("no bars")
        return self._bars[symbol][-limit:]

    def get_news(self, symbol=None, limit=20):
        return []


def test_analyze_symbol_and_render_zh():
    result = analyze_symbol(_Feed(), "NVDA", now=T0)
    assert result["symbol"] == "NVDA" and result["verdict"] is not None
    txt = render_analysis_zh(result)
    assert "NVDA" in txt and "结论" in txt


def test_analyze_symbol_unknown_raises_datafeed():
    import pytest
    with pytest.raises(DataFeedError):
        analyze_symbol(_Feed(), "ZZZZ", now=T0)
