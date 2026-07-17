"""Multi-market Telegram routing (HK order-capable session).

One Telegram poller serves both the US and HK confirmation windows: a callback
must reach the ConfirmationService that OWNS the candidate, even when the poll is
driven with a different market's service. Verified via the per-candidate service
registry populated by push_cards.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from swing_trader.dailyloop import CALLBACK_ID_LEN, TelegramSurfaceAdapter
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    OrderType,
    Side,
    TimeInForce,
)

NOW = datetime(2026, 7, 15, 2, 30, tzinfo=timezone.utc)


class _Transport:
    def __init__(self):
        self.queue = []
        self.answered = []

    def send_message(self, chat_id, text, reply_markup=None):
        return 1

    def get_updates(self, offset=None, timeout=0):
        batch, self.queue = self.queue, []
        return batch

    def answer_callback(self, callback_query_id, text=""):
        self.answered.append((callback_query_id, text))


class _Result:
    def __init__(self, candidate):
        self.ok = True
        self.candidate = candidate


class _Service:
    """Minimal ConfirmationService stand-in recording act() calls."""

    def __init__(self, cand):
        self._cand = cand
        self.acted = []

    def get(self, cid):
        return (self._cand, 1) if cid == self._cand.id else None

    def act(self, full_id, action, **kw):
        self.acted.append((full_id, action))
        return _Result(self._cand)


def _cand(symbol, market, currency):
    return CandidateOrder(
        symbol=symbol, market=market, currency=currency, side=Side.BUY, qty=1,
        order_type=OrderType.BRACKET, limit=100.0, stop=95.0, tp=110.0,
        tif=TimeInForce.DAY, rationale="r", confidence=0.7, ref_px=100.0,
        status=CandidateStatus.PUSHED,
    )


def _callback(cand, action="ok"):
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb1",
            "from": {"username": "gongqing"},
            "message": {"chat": {"id": "42"}},
            "data": json.dumps({"id": cand.id[:CALLBACK_ID_LEN], "a": action}),
        },
    }


def _adapter(transport):
    return TelegramSurfaceAdapter(
        transport, "42", interactive=True, allowed_users={"gongqing"}
    )


def test_hk_callback_routes_to_hk_service_under_us_poll():
    transport = _Transport()
    adapter = _adapter(transport)
    us, hk = _cand("NVDA", "US", "USD"), _cand("0700.HK", "HK", "HKD")
    us_svc, hk_svc = _Service(us), _Service(hk)
    adapter.push_cards([us], service=us_svc)
    adapter.push_cards([hk], service=hk_svc)

    # a tap on the HK card arrives while the poll is driven with the US service
    transport.queue.append(_callback(hk))
    adapter.poll(us_svc, NOW)

    assert hk_svc.acted == [(hk.id, "approve")]
    assert us_svc.acted == []  # never mis-routed to the US window


def test_us_callback_still_routes_to_us_service():
    transport = _Transport()
    adapter = _adapter(transport)
    us, hk = _cand("NVDA", "US", "USD"), _cand("0700.HK", "HK", "HKD")
    us_svc, hk_svc = _Service(us), _Service(hk)
    adapter.push_cards([us], service=us_svc)
    adapter.push_cards([hk], service=hk_svc)

    transport.queue.append(_callback(us))
    adapter.poll(us_svc, NOW)

    assert us_svc.acted == [(us.id, "approve")]
    assert hk_svc.acted == []
