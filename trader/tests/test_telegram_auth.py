"""Tests for TelegramSurfaceAdapter authentication (Loop.md §5.6, P0.5-6)."""

import json
from datetime import datetime, timezone

from swing_trader.confirmation import ConfirmationService
from swing_trader.dailyloop import TelegramSurfaceAdapter
from swing_trader.ledger import Ledger
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Side,
)

IN_WINDOW = datetime(2026, 7, 13, 15, 45, tzinfo=timezone.utc)


class MockTransport:
    def __init__(self):
        self.queue, self.answered, self._mid = [], [], 0

    def send_message(self, chat_id, text, reply_markup=None):
        self._mid += 1
        return self._mid

    def get_updates(self, offset=None, timeout=0):
        batch, self.queue = self.queue, []
        return batch

    def answer_callback(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))


def setup(tmp_path, allowed):
    ledger = Ledger(url=f"sqlite:///{tmp_path/'a.db'}")
    service = ConfirmationService(ledger, mode=Mode.PAPER)
    c = CandidateOrder(
        symbol="NVDA", side=Side.BUY, qty=2, order_type=OrderType.BRACKET,
        limit=99.5, stop=91.5, tp=111.5, rationale="t", confidence=0.7,
        status=CandidateStatus.RISK_APPROVED,
    )
    ledger.record_candidate(c, Mode.PAPER)
    published = service.publish([c], IN_WINDOW)[0]
    transport = MockTransport()
    adapter = TelegramSurfaceAdapter(transport, "42", allowed_users=allowed)
    adapter.push_cards([published])
    return ledger, service, transport, adapter, published


def callback(cand, sender):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "data": json.dumps({"id": cand.id[:16], "a": "ok"},
                               separators=(",", ":")),
            "from": sender,
            "message": {"message_id": 1},
        },
    }


def test_allowed_username_approves(tmp_path):
    ledger, service, transport, adapter, c = setup(tmp_path, {"@GongQing"})
    transport.queue.append(callback(c, {"id": 1, "username": "gongqing"}))
    adapter.poll(service, IN_WINDOW)
    assert service.get(c.id)[0].status is CandidateStatus.APPROVED


def test_allowed_numeric_id_approves(tmp_path):
    ledger, service, transport, adapter, c = setup(tmp_path, {"777"})
    transport.queue.append(callback(c, {"id": 777}))
    adapter.poll(service, IN_WINDOW)
    assert service.get(c.id)[0].status is CandidateStatus.APPROVED


def test_unknown_user_refused(tmp_path):
    ledger, service, transport, adapter, c = setup(tmp_path, {"gongqing"})
    transport.queue.append(callback(c, {"id": 999, "username": "stranger"}))
    adapter.poll(service, IN_WINDOW)
    assert service.get(c.id)[0].status is CandidateStatus.PUSHED  # unchanged
    assert any("not authorized" in t for _, t in transport.answered)
    # no audit row was written for the unauthorized attempt at adapter level,
    # and the candidate has no approve entry
    assert not [a for a in ledger.get_audit(candidate_id=c.id)
                if a.action == "approve"]


def test_empty_allowlist_refuses_everyone(tmp_path):
    """Interactive auth must be explicit — an empty allowlist is closed."""
    ledger, service, transport, adapter, c = setup(tmp_path, set())
    transport.queue.append(callback(c, {"id": 1, "username": "gongqing"}))
    adapter.poll(service, IN_WINDOW)
    assert service.get(c.id)[0].status is CandidateStatus.PUSHED


def test_outbound_only_never_reads_updates(tmp_path):
    ledger, service, transport, adapter, c = setup(tmp_path, {"gongqing"})
    adapter.interactive = False
    transport.queue.append(callback(c, {"id": 1, "username": "gongqing"}))
    adapter.poll(service, IN_WINDOW)
    assert transport.queue  # untouched: get_updates never called
    assert service.get(c.id)[0].status is CandidateStatus.PUSHED
