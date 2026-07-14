"""Tests for in-Telegram portfolio-draft confirmation (Loop.md P0.9, boundary #4).

The user records a trade, a draft card is pushed to Telegram, and an allowlisted
HUMAN taps ✅/❌ to confirm/reject WITHOUT a portal round-trip ("telegram 直接确认").
The hard contract carries over from the portal path: only an authenticated human
finalizes (the LLM never does), an INCOMPLETE draft is refused, and confirm goes
through the SAME PortfolioDraftService that the portal uses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from swing_trader.dailyloop import TelegramSurfaceAdapter
from swing_trader.portfolio import DraftStatus, EventType, MarketScope
from swing_trader.portfolio_draft import PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal

NOW = datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


class MockTransport:
    """Captures sent cards (text + keyboard) and answered callbacks; feeds
    queued updates back through get_updates (mirrors test_telegram_auth)."""

    def __init__(self):
        self.sent: list[tuple[str, dict | None]] = []
        self.queue: list[dict] = []
        self.answered: list[tuple[str, str]] = []
        self._mid = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        self.sent.append((text, reply_markup))
        return self._mid

    def get_updates(self, offset=None, timeout=0):
        batch, self.queue = self.queue, []
        return batch

    def answer_callback(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))


def _setup(tmp_path, allowed, interactive=True):
    journal = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    account = journal.create_account(
        name="平安证券", market_scope="CN", base_currency="CNY"
    )
    svc = PortfolioDraftService(journal, clock=_Clock())
    transport = MockTransport()
    adapter = TelegramSurfaceAdapter(
        transport, "42", interactive=interactive, allowed_users=allowed
    )
    adapter.set_draft_service(svc)
    return journal, account, svc, transport, adapter


def _complete_buy(svc, account_id, **over):
    # Mirrors the user's real 平安证券 ETF fill: 513310 × 200 @ 5.762.
    kw = dict(
        account_id=account_id, event_type=EventType.BUY, symbol="513310",
        market=MarketScope.CN, currency="CNY", qty=200.0, price=5.762,
        occurred_at=NOW, original_text="513310 成交 5.762 200 股",
    )
    kw.update(over)
    return svc.create_draft(**kw)


def _draft_cb(draft, sender, action="ok", cb_id="cb1"):
    return {
        "update_id": int(cb_id[-1]) if cb_id[-1].isdigit() else 1,
        "callback_query": {
            "id": cb_id,
            "data": json.dumps(
                {"id": draft.id[:16], "a": action, "t": "d"},
                separators=(",", ":"),
            ),
            "from": sender,
            "message": {"message_id": 1},
        },
    }


# --------------------------------------------------------------- push / render


def test_push_registers_and_sends_card(tmp_path):
    _, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d, account_label="平安证券")
    assert transport.sent, "a draft card should have been sent"
    text, kb = transport.sent[-1]
    assert "买入" in text and "513310" in text and "平安证券" in text
    assert kb and "inline_keyboard" in kb
    # callback payload carries the draft type tag and stays <= 64 bytes.
    data = kb["inline_keyboard"][0][0]["callback_data"]
    assert '"t":"d"' in data and len(data.encode()) <= 64


def test_outbound_only_adapter_does_not_push(tmp_path):
    _, account, svc, transport, adapter = _setup(
        tmp_path, {"gongqing"}, interactive=False
    )
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    assert not transport.sent  # only the interactive bot long-polls callbacks


# ------------------------------------------------------------------- confirm


def test_allowlisted_confirm_appends_event(tmp_path):
    journal, account, svc, transport, adapter = _setup(tmp_path, {"@GongQing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "ok"))
    adapter.poll(None, NOW)  # draft path never touches the ConfirmationService

    assert svc.get_draft(d.id).status is DraftStatus.CONFIRMED
    (pos,) = journal.holdings(account.id).holdings
    assert pos.symbol == "513310" and pos.qty == 200.0
    # confirmer recorded as the telegram human, NOT the LLM (boundary #4).
    ev = journal.get_events(account.id)[-1]
    assert ev.actor == "telegram:gongqing" and ev.surface == "telegram"
    # a persistent 已入账 line is posted (toasts vanish; the chat is the audit).
    assert any("已入账" in t for t, _ in transport.sent)


def test_reject_marks_rejected_no_event(tmp_path):
    journal, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "no"))
    adapter.poll(None, NOW)

    assert svc.get_draft(d.id).status is DraftStatus.REJECTED
    assert not journal.holdings(account.id).holdings  # nothing appended
    # a persistent group line records the rejection (symmetric with confirm):
    # the standing trade + who acted, so the group has an audit, not just a toast.
    line = next((t for t, _ in transport.sent if "已拒绝" in t), None)
    assert line is not None and "513310" in line and "@gongqing" in line
    assert any("已拒绝" in txt for _, txt in transport.answered)  # toast kept too


def test_confirm_group_line_has_trade_and_actor(tmp_path):
    _, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "ok"))
    adapter.poll(None, NOW)
    line = next((t for t, _ in transport.sent if "已入账" in t), None)
    assert line is not None and "513310" in line and "200" in line and "@gongqing" in line


def test_stale_tap_after_confirm_is_inert(tmp_path):
    _, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "ok"))
    adapter.poll(None, NOW)
    # a second tap on the now-settled card: short-id was popped -> inert.
    transport.queue.append(
        _draft_cb(d, {"id": 1, "username": "gongqing"}, "ok", cb_id="cb2")
    )
    adapter.poll(None, NOW)
    assert any("未知或已处理" in t for _, t in transport.answered)


# ---------------------------------------------------------------- guardrails


def test_unauthorized_user_refused(tmp_path):
    journal, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 999, "username": "stranger"}, "ok"))
    adapter.poll(None, NOW)

    assert svc.get_draft(d.id).status is DraftStatus.DRAFT  # unchanged
    assert not journal.holdings(account.id).holdings
    assert any("权限" in t for _, t in transport.answered)


def test_incomplete_draft_cannot_confirm(tmp_path):
    _, account, svc, transport, adapter = _setup(tmp_path, {"gongqing"})
    # No quantity -> INCOMPLETE (draft_missing_fields flags "quantity").
    d = _complete_buy(svc, account.id, qty=None)
    assert d.needs_clarification
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "ok"))
    adapter.poll(None, NOW)

    assert svc.get_draft(d.id).status is DraftStatus.DRAFT  # still awaiting
    assert any(("补全" in t or "门户" in t) for _, t in transport.answered)


def test_empty_allowlist_refuses_everyone(tmp_path):
    _, account, svc, transport, adapter = _setup(tmp_path, set())
    d = _complete_buy(svc, account.id)
    adapter.push_draft_card(d)
    transport.queue.append(_draft_cb(d, {"id": 1, "username": "gongqing"}, "ok"))
    adapter.poll(None, NOW)
    assert svc.get_draft(d.id).status is DraftStatus.DRAFT
