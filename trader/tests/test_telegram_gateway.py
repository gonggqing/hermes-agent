"""Tests for swing_trader.telegram_gateway (Loop.md §5.6, §4, §9).

Fully deterministic and network-free (Loop.md §3): HttpTransport gets a fake
session injected; ConfirmationGateway gets an in-test MockTransport that
records calls and feeds scripted updates. Window-boundary UTC instants are
computed explicitly for BOTH DST regimes (July = EDT/UTC-4, January =
EST/UTC-5).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Any, Optional

import pytest
from pydantic import SecretStr

from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    OrderType,
    Side,
    TimeInForce,
)
from swing_trader.telegram_gateway import (
    CALLBACK_ID_LEN,
    ConfirmationGateway,
    GatewayError,
    HttpTransport,
    build_keyboard,
    render_card,
)

UTC = timezone.utc

# ---------------------------------------------------------------- UTC instants
# July regime: America/New_York is EDT (UTC-4) -> 11:30 ET == 15:30 UTC.
JULY_PUSH = datetime(2026, 7, 10, 15, 30, 0, tzinfo=UTC)  # exactly 11:30:00 EDT
JULY_BEFORE = datetime(2026, 7, 10, 15, 29, 59, tzinfo=UTC)  # 11:29:59 EDT
JULY_MID = datetime(2026, 7, 10, 16, 0, 0, tzinfo=UTC)  # 12:00:00 EDT
JULY_LAST_IN = datetime(2026, 7, 10, 16, 29, 59, tzinfo=UTC)  # 12:29:59 EDT
JULY_CUTOFF = datetime(2026, 7, 10, 16, 30, 0, tzinfo=UTC)  # exactly 12:30:00 EDT
JULY_AFTER = datetime(2026, 7, 10, 17, 0, 0, tzinfo=UTC)  # 13:00:00 EDT

# January regime: America/New_York is EST (UTC-5) -> 11:30 ET == 16:30 UTC.
JAN_PUSH = datetime(2026, 1, 9, 16, 30, 0, tzinfo=UTC)  # exactly 11:30:00 EST
JAN_BEFORE = datetime(2026, 1, 9, 16, 29, 59, tzinfo=UTC)  # 11:29:59 EST
JAN_LAST_IN = datetime(2026, 1, 9, 17, 29, 59, tzinfo=UTC)  # 12:29:59 EST
JAN_CUTOFF = datetime(2026, 1, 9, 17, 30, 0, tzinfo=UTC)  # exactly 12:30:00 EST

TOKEN = "1234567:SUPERSECRETBOTTOKEN"


# --------------------------------------------------------------------- fakes


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    """Records post() calls; replays scripted responses. Zero real HTTP."""

    def __init__(self, responses: list[FakeResponse]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post(self, url: str, json: Any = None, timeout: float | None = None) -> FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self._responses.pop(0)


class MockTransport:
    """In-test TelegramTransport: records calls, feeds scripted updates."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.answered: list[dict[str, Any]] = []
        self.get_updates_calls: list[dict[str, Any]] = []
        self.updates_queue: list[list[dict]] = []
        self._next_message_id = 100

    def send_message(
        self, chat_id: str, text: str, reply_markup: dict | None = None
    ) -> int:
        self._next_message_id += 1
        self.sent.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_markup": reply_markup,
                "message_id": self._next_message_id,
            }
        )
        return self._next_message_id

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        if self.updates_queue:
            return self.updates_queue.pop(0)
        return []

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        self.answered.append({"id": callback_query_id, "text": text})


# ------------------------------------------------------------------- helpers


def make_candidate(**overrides: Any) -> CandidateOrder:
    base: dict[str, Any] = dict(
        symbol="NVDA",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.LMT,
        limit=100.0,
        sl=92.0,
        tp=120.0,
        tif=TimeInForce.GTC,
        rationale="Breakout above 100 on strong volume; AI infra momentum.",
        confidence=0.72,
        status=CandidateStatus.RISK_APPROVED,
        risk_note="sized to 1.0% equity risk",
    )
    base.update(overrides)
    return CandidateOrder(**base)


def make_gateway(
    transport: MockTransport | None = None,
) -> tuple[ConfirmationGateway, MockTransport]:
    transport = transport or MockTransport()
    gw = ConfirmationGateway(
        transport=transport,
        chat_id="42",
        push_time_et=time(11, 30),
        cutoff_et=time(12, 30),
        market_tz="America/New_York",
    )
    return gw, transport


def callback_update(
    update_id: int, key: str, action: str, cq_id: str = "cbq1"
) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cq_id,
            "data": json.dumps({"id": key, "a": action}),
            "message": {"message_id": 101},
        },
    }


def message_update(update_id: int, text: str, reply_to: int | None = None) -> dict:
    msg: dict[str, Any] = {"message_id": 900 + update_id, "text": text, "chat": {"id": 42}}
    if reply_to is not None:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def push_one(
    gw: ConfirmationGateway,
    transport: MockTransport,
    candidate: Optional[CandidateOrder] = None,
    now: datetime = JULY_PUSH,
) -> tuple[CandidateOrder, str]:
    candidate = candidate or make_candidate()
    pushed = gw.push([candidate], now)
    assert len(pushed) == 1
    return pushed[0], pushed[0].id[:CALLBACK_ID_LEN]


# ------------------------------------------------------------- HttpTransport


class TestHttpTransport:
    def test_send_message_builds_url_and_returns_message_id(self) -> None:
        session = FakeSession(
            [FakeResponse(200, {"ok": True, "result": {"message_id": 7}})]
        )
        t = HttpTransport(SecretStr(TOKEN), session=session)
        msg_id = t.send_message("42", "hello", reply_markup={"inline_keyboard": []})
        assert msg_id == 7
        call = session.calls[0]
        assert call["url"] == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        assert call["json"]["chat_id"] == "42"
        assert call["json"]["text"] == "hello"
        assert call["json"]["reply_markup"] == {"inline_keyboard": []}

    def test_get_updates_passes_offset_and_timeout(self) -> None:
        session = FakeSession([FakeResponse(200, {"ok": True, "result": [{"update_id": 1}]})])
        t = HttpTransport(SecretStr(TOKEN), session=session)
        updates = t.get_updates(offset=5, timeout=10)
        assert updates == [{"update_id": 1}]
        call = session.calls[0]
        assert call["url"].endswith("/getUpdates")
        assert call["json"] == {"timeout": 10, "offset": 5}
        # request timeout must exceed the long-poll timeout
        assert call["timeout"] > 10

    def test_answer_callback_posts_query_id(self) -> None:
        session = FakeSession([FakeResponse(200, {"ok": True, "result": True})])
        t = HttpTransport(SecretStr(TOKEN), session=session)
        t.answer_callback("cbq9", "done")
        call = session.calls[0]
        assert call["url"].endswith("/answerCallbackQuery")
        assert call["json"] == {"callback_query_id": "cbq9", "text": "done"}

    def test_http_error_raises_without_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = FakeSession([FakeResponse(500, {"ok": False})])
        t = HttpTransport(SecretStr(TOKEN), session=session)
        with caplog.at_level(logging.DEBUG, logger="swing_trader.telegram_gateway"):
            with pytest.raises(GatewayError) as ei:
                t.send_message("42", "hi")
        assert "sendMessage" in str(ei.value)
        assert TOKEN not in str(ei.value)
        for record in caplog.records:
            assert TOKEN not in record.getMessage()
            assert TOKEN not in str(record.__dict__)

    def test_api_not_ok_raises_with_description_no_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = FakeSession(
            [FakeResponse(200, {"ok": False, "description": "Bad Request: chat not found"})]
        )
        t = HttpTransport(SecretStr(TOKEN), session=session)
        with caplog.at_level(logging.DEBUG, logger="swing_trader.telegram_gateway"):
            with pytest.raises(GatewayError) as ei:
                t.get_updates()
        assert "getUpdates" in str(ei.value)
        assert "chat not found" in str(ei.value)
        assert TOKEN not in str(ei.value)
        for record in caplog.records:
            assert TOKEN not in str(record.__dict__)

    def test_transport_exception_chained_from_none(self) -> None:
        class BoomSession:
            def post(self, url: str, json: Any = None, timeout: float | None = None):
                raise RuntimeError(f"connection refused for {url}")  # embeds token

        t = HttpTransport(SecretStr(TOKEN), session=BoomSession())
        with pytest.raises(GatewayError) as ei:
            t.send_message("42", "hi")
        assert TOKEN not in str(ei.value)
        assert ei.value.__cause__ is None  # `from None`: cause would leak the URL


# ------------------------------------------------------------- card rendering


class TestCard:
    def test_card_contains_all_fields_and_no_token(self) -> None:
        c = make_candidate()
        card = render_card(c)
        assert "NVDA" in card
        assert "BUY" in card
        assert "10" in card
        assert "LMT" in card
        assert "GTC" in card
        assert "limit=100" in card
        assert "stop=-" in card
        assert "tp=120" in card
        assert "sl=92" in card
        assert "72%" in card
        assert c.rationale in card
        assert c.risk_note in card
        assert TOKEN not in card

    def test_card_truncates_rationale_to_300_chars(self) -> None:
        c = make_candidate(rationale="x" * 400)
        card = render_card(c)
        assert "x" * 300 + "..." in card
        assert "x" * 301 not in card

    def test_callback_data_within_64_bytes(self) -> None:
        c = make_candidate()
        keyboard = build_keyboard(c)
        rows = keyboard["inline_keyboard"]
        assert len(rows) == 1
        for button in rows[0]:
            data = button["callback_data"]
            assert len(data.encode("utf-8")) <= 64
            parsed = json.loads(data)
            assert parsed["id"] == c.id[:CALLBACK_ID_LEN]
            assert parsed["a"] in ("ok", "edit", "no")

    def test_keyboard_single_row_approve_edit_reject(self) -> None:
        keyboard = build_keyboard(make_candidate())
        labels = [b["text"] for b in keyboard["inline_keyboard"][0]]
        assert labels == ["Approve", "Edit", "Reject"]


# -------------------------------------------------------------------- window


class TestWindow:
    def test_boundaries_july_edt(self) -> None:
        gw, _ = make_gateway()
        assert gw.in_window(JULY_PUSH) is True  # exactly 11:30:00 -> in
        assert gw.in_window(JULY_BEFORE) is False  # 11:29:59 -> out
        assert gw.in_window(JULY_LAST_IN) is True  # 12:29:59 -> in
        assert gw.in_window(JULY_CUTOFF) is False  # exactly 12:30:00 -> out

    def test_boundaries_january_est(self) -> None:
        gw, _ = make_gateway()
        assert gw.in_window(JAN_PUSH) is True  # exactly 11:30:00 -> in
        assert gw.in_window(JAN_BEFORE) is False  # 11:29:59 -> out
        assert gw.in_window(JAN_LAST_IN) is True  # 12:29:59 -> in
        assert gw.in_window(JAN_CUTOFF) is False  # exactly 12:30:00 -> out

    def test_naive_datetime_rejected(self) -> None:
        gw, _ = make_gateway()
        with pytest.raises(GatewayError):
            gw.in_window(datetime(2026, 7, 10, 15, 30, 0))

    def test_bad_window_ordering_rejected(self) -> None:
        with pytest.raises(GatewayError):
            ConfirmationGateway(
                MockTransport(), "42", push_time_et=time(12, 30), cutoff_et=time(11, 30)
            )


# ---------------------------------------------------------------------- push


class TestPush:
    def test_push_outside_window_refused(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        gw, transport = make_gateway()
        with caplog.at_level(logging.WARNING, logger="swing_trader.telegram_gateway"):
            result = gw.push([make_candidate()], JULY_BEFORE)
        assert result == []
        assert transport.sent == []
        assert gw.pending == {}
        assert any("refused" in r.getMessage() for r in caplog.records)

    def test_only_risk_approved_pushed(self) -> None:
        gw, transport = make_gateway()
        approved = make_candidate()
        proposed = make_candidate(status=CandidateStatus.PROPOSED)
        vetoed = make_candidate(status=CandidateStatus.RISK_VETOED)
        pushed = gw.push([proposed, approved, vetoed], JULY_PUSH)
        assert [c.id for c in pushed] == [approved.id]
        assert len(transport.sent) == 1
        assert list(gw.pending) == [approved.id[:CALLBACK_ID_LEN]]

    def test_push_marks_pushed_and_keys_match_callback_data(self) -> None:
        gw, transport = make_gateway()
        pushed, key = push_one(gw, transport)
        assert pushed.status is CandidateStatus.PUSHED
        assert gw.pending[key].status is CandidateStatus.PUSHED
        sent = transport.sent[0]
        button_data = json.loads(
            sent["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        )
        assert button_data["id"] == key  # same truncated id as pending key

    def test_push_sends_one_card_per_candidate(self) -> None:
        gw, transport = make_gateway()
        c1, c2 = make_candidate(), make_candidate(symbol="MU")
        pushed = gw.push([c1, c2], JULY_PUSH)
        assert len(pushed) == 2
        assert len(transport.sent) == 2
        assert "NVDA" in transport.sent[0]["text"]
        assert "MU" in transport.sent[1]["text"]
        for sent in transport.sent:
            assert sent["reply_markup"] is not None


# ------------------------------------------------------------ approve/reject


class TestApproveReject:
    def test_approve_flow(self) -> None:
        gw, transport = make_gateway()
        pushed, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, key, "ok")])
        finalized = gw.poll_responses(JULY_MID)
        assert len(finalized) == 1
        assert finalized[0].status is CandidateStatus.APPROVED
        assert finalized[0].id == pushed.id
        assert gw.pending == {}
        assert [c.id for c in gw.finalized().approved] == [pushed.id]
        assert gw.finalized().human_approved[0].id == pushed.id
        assert transport.answered[-1]["id"] == "cbq1"
        assert "Approved" in transport.answered[-1]["text"]

    def test_reject_flow(self) -> None:
        gw, transport = make_gateway()
        pushed, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, key, "no")])
        finalized = gw.poll_responses(JULY_MID)
        assert finalized[0].status is CandidateStatus.REJECTED
        assert gw.pending == {}
        assert [c.id for c in gw.finalized().rejected] == [pushed.id]
        assert gw.finalized().human_approved == []

    def test_approval_after_cutoff_refused(self) -> None:
        gw, transport = make_gateway()
        pushed, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, key, "ok")])
        finalized = gw.poll_responses(JULY_CUTOFF)  # exactly 12:30 -> closed
        assert finalized == []
        assert key in gw.pending  # still pending; will expire
        assert gw.finalized().approved == []
        assert "window closed" in transport.answered[-1]["text"].lower()

    def test_malformed_callback_ignored(self) -> None:
        gw, transport = make_gateway()
        _, key = push_one(gw, transport)
        transport.updates_queue.append(
            [
                {
                    "update_id": 1,
                    "callback_query": {"id": "cbqX", "data": "not-json{{"},
                },
                {
                    "update_id": 2,
                    "callback_query": {"id": "cbqY", "data": json.dumps({"x": 1})},
                },
            ]
        )
        finalized = gw.poll_responses(JULY_MID)
        assert finalized == []
        assert key in gw.pending  # untouched
        assert len(transport.answered) == 2  # both answered politely

    def test_unknown_id_callback_answered_and_ignored(self) -> None:
        gw, transport = make_gateway()
        _, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, "deadbeefdeadbeef", "ok")])
        finalized = gw.poll_responses(JULY_MID)
        assert finalized == []
        assert key in gw.pending
        assert len(transport.answered) == 1
        assert gw.finalized().approved == []

    def test_unknown_action_answered_and_ignored(self) -> None:
        gw, transport = make_gateway()
        _, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, key, "maybe")])
        finalized = gw.poll_responses(JULY_MID)
        assert finalized == []
        assert key in gw.pending

    def test_offset_advances_across_polls(self) -> None:
        gw, transport = make_gateway()
        _, key = push_one(gw, transport)
        transport.updates_queue.append(
            [
                {"update_id": 7, "callback_query": {"id": "a", "data": "junk"}},
                callback_update(8, key, "ok", cq_id="b"),
            ]
        )
        gw.poll_responses(JULY_MID)
        gw.poll_responses(JULY_MID)
        assert transport.get_updates_calls[0]["offset"] is None
        assert transport.get_updates_calls[1]["offset"] == 9  # max update_id + 1


# ----------------------------------------------------------------------- edit


class TestEdit:
    def start_edit(
        self, gw: ConfirmationGateway, transport: MockTransport
    ) -> tuple[CandidateOrder, str]:
        pushed, key = push_one(gw, transport)
        transport.updates_queue.append([callback_update(1, key, "edit")])
        assert gw.poll_responses(JULY_MID) == []
        assert gw.is_awaiting_edit(pushed.id)
        return pushed, key

    def test_edit_request_answers_with_instructions(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        assert key in gw.pending  # still pending until the edit lands
        answer = transport.answered[-1]["text"]
        assert "qty=N" in answer and "sl=Y" in answer

    def test_edit_happy_path_qty_and_sl(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "qty=5 sl=95")])
        finalized = gw.poll_responses(JULY_MID)
        assert len(finalized) == 1
        edited = finalized[0]
        assert edited.status is CandidateStatus.EDITED
        assert edited.qty == 5.0
        assert edited.sl == 95.0
        assert edited.limit == 100.0  # untouched fields preserved
        assert isinstance(edited, CandidateOrder)  # re-validated model
        assert gw.pending == {}
        assert [c.id for c in gw.finalized().edited] == [pushed.id]
        assert gw.finalized().human_approved[0].id == pushed.id  # Loop.md §4
        assert "Edited and approved" in transport.sent[-1]["text"]

    def test_edit_stripping_sl_refused(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "sl=0")])
        finalized = gw.poll_responses(JULY_MID)
        assert finalized == []
        assert key in gw.pending  # still pending
        assert gw.is_awaiting_edit(pushed.id)  # still awaiting a valid edit
        assert gw.pending[key].sl == 92.0  # unchanged
        assert "refused" in transport.sent[-1]["text"].lower()

    def test_edit_qty_zero_refused(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "qty=0")])
        finalized = gw.poll_responses(JULY_MID)
        assert finalized == []
        assert gw.is_awaiting_edit(pushed.id)
        assert gw.pending[key].qty == 10.0
        assert "refused" in transport.sent[-1]["text"].lower()

    def test_edit_garbage_text_refused_keeps_awaiting(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "just make it bigger")])
        assert gw.poll_responses(JULY_MID) == []
        assert gw.is_awaiting_edit(pushed.id)
        assert "refused" in transport.sent[-1]["text"].lower()

    def test_edit_unknown_key_refuses_whole_edit(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "qty=5 side=SELL")])
        assert gw.poll_responses(JULY_MID) == []
        assert gw.pending[key].qty == 10.0  # nothing half-applied
        assert gw.is_awaiting_edit(pushed.id)

    def test_edit_after_cutoff_refused(self) -> None:
        gw, transport = make_gateway()
        pushed, key = self.start_edit(gw, transport)
        transport.updates_queue.append([message_update(2, "qty=5")])
        assert gw.poll_responses(JULY_AFTER) == []
        assert key in gw.pending
        assert "window closed" in transport.sent[-1]["text"].lower()

    def test_edit_reply_targets_correct_card(self) -> None:
        gw, transport = make_gateway()
        c1, c2 = make_candidate(), make_candidate(symbol="MU")
        gw.push([c1, c2], JULY_PUSH)
        key1 = c1.id[:CALLBACK_ID_LEN]
        key2 = c2.id[:CALLBACK_ID_LEN]
        msg_id_2 = transport.sent[1]["message_id"]
        transport.updates_queue.append(
            [callback_update(1, key1, "edit", "a"), callback_update(2, key2, "edit", "b")]
        )
        gw.poll_responses(JULY_MID)
        transport.updates_queue.append(
            [message_update(3, "qty=3", reply_to=msg_id_2)]
        )
        finalized = gw.poll_responses(JULY_MID)
        assert len(finalized) == 1
        assert finalized[0].symbol == "MU"
        assert finalized[0].qty == 3.0
        assert key1 in gw.pending  # NVDA edit still awaiting
        assert key2 not in gw.pending


# --------------------------------------------------------------------- expiry


class TestExpiry:
    def test_expire_stale_after_cutoff(self) -> None:
        gw, transport = make_gateway()
        c1, c2 = make_candidate(), make_candidate(symbol="MU")
        gw.push([c1, c2], JULY_PUSH)
        expired = gw.expire_stale(JULY_CUTOFF)  # exactly 12:30 ET -> expired
        assert len(expired) == 2
        assert all(c.status is CandidateStatus.EXPIRED for c in expired)
        assert gw.pending == {}
        assert len(gw.finalized().expired) == 2
        assert gw.finalized().human_approved == []

    def test_expire_stale_before_cutoff_noop(self) -> None:
        gw, transport = make_gateway()
        push_one(gw, transport)
        assert gw.expire_stale(JULY_LAST_IN) == []
        assert len(gw.pending) == 1

    def test_expire_after_approval_only_expires_remaining(self) -> None:
        gw, transport = make_gateway()
        c1, c2 = make_candidate(), make_candidate(symbol="MU")
        gw.push([c1, c2], JULY_PUSH)
        transport.updates_queue.append(
            [callback_update(1, c1.id[:CALLBACK_ID_LEN], "ok")]
        )
        gw.poll_responses(JULY_MID)
        expired = gw.expire_stale(JULY_CUTOFF + timedelta(minutes=5))
        assert [c.symbol for c in expired] == ["MU"]
        result = gw.finalized()
        assert len(result.approved) == 1
        assert len(result.expired) == 1
