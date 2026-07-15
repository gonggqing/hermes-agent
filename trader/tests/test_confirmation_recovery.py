"""Durable approval recovery across Finance service restarts."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from swing_trader.api import FinanceRuntime
from swing_trader.confirmation import ConfirmationService, Surface
from swing_trader.dailyloop import DailyLoop
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Side,
)
from swing_trader.simulate import MutableClock, SimFeed, build_sim_series, trading_days

ET = ZoneInfo("America/New_York")
SYMBOLS = ["NVDA", "MU", "ANET"]


@pytest.fixture()
def recovery_env(tmp_path):
    days = trading_days(date(2026, 7, 13), 3)
    series, warmup = build_sim_series(SYMBOLS, days)
    feed = SimFeed(series)
    feed.set_day(warmup)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'recovery.db'}")
    broker = PaperBroker(starting_cash=50_000.0)
    clock = MutableClock(datetime.combine(days[0], time(8), tzinfo=ET)
                         .astimezone(timezone.utc))
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=clock)
    loop = DailyLoop(
        feed, broker, ledger, symbols=SYMBOLS, clock=clock,
        runtime=runtime, notify=lambda _text: None,
    )
    return loop, runtime, ledger, broker, feed, clock, days[0]


def _candidate(feed, clock, day, *, status=CandidateStatus.RISK_APPROVED):
    clock.set_et(day, 11, 0)
    last = feed.get_quote("NVDA").last
    return CandidateOrder(
        ts=clock(), symbol="NVDA", side=Side.BUY, qty=1,
        order_type=OrderType.BRACKET, limit=last * 0.995,
        stop=last * 0.92, tp=last * 1.10, rationale="restart test",
        confidence=0.7, ref_px=last, status=status,
    )


def _seed_approved(ledger, feed, clock, day):
    candidate = _candidate(feed, clock, day)
    ledger.record_candidate(candidate, Mode.PAPER)
    service = ConfirmationService(ledger, mode=Mode.PAPER)
    pushed = service.publish([candidate], clock.set_et(day, 11, 30, 1))[0]
    result = service.act(
        pushed.id, "approve", "human", Surface.TELEGRAM, "approve-1",
        clock.set_et(day, 11, 45),
    )
    assert result.ok
    return candidate.id, service


def test_restart_before_push_restores_risk_approved_for_scheduled_push(recovery_env):
    loop, runtime, ledger, _, feed, clock, day = recovery_env
    candidate = _candidate(feed, clock, day)
    ledger.record_candidate(candidate, Mode.PAPER)

    summary = loop.recover_confirmation_state(clock.set_et(day, 11, 20))
    loop.on_push()  # still before 11:30: refused, state remains risk-approved
    clock.set_et(day, 11, 30, 1)
    loop.on_push()

    assert summary["status"] == "recovered"
    assert ledger.get_candidates(status=CandidateStatus.PUSHED)[0].id == candidate.id
    assert runtime.confirmation is not None


def test_restart_before_cutoff_restores_human_approval(recovery_env):
    loop, runtime, ledger, _, feed, clock, day = recovery_env
    candidate_id, _ = _seed_approved(ledger, feed, clock, day)

    summary = loop.recover_confirmation_state(clock.set_et(day, 12, 10))

    assert summary["restored"] == 1
    assert runtime.confirmation.get(candidate_id)[0].status is CandidateStatus.APPROVED
    assert runtime.confirmation.finalized().human_approved


def test_restart_after_cutoff_rerisks_and_places_once(recovery_env):
    loop, _, ledger, broker, feed, clock, day = recovery_env
    candidate_id, _ = _seed_approved(ledger, feed, clock, day)
    now = clock.set_et(day, 12, 45)

    first = loop.recover_confirmation_state(now)
    second = loop.recover_confirmation_state(now)

    assert first["placed"] == 1
    assert second["placed"] == 0
    assert len([o for o in broker.get_orders() if o.parent_order_id is None]) == 1
    stored = {c.id: c for c in ledger.get_candidates(mode=Mode.PAPER)}
    assert stored[candidate_id].status is CandidateStatus.PLACED
    actions = [a.action for a in ledger.get_audit(candidate_id=candidate_id)]
    assert "finalize" in actions and "execute" in actions


def test_restart_after_close_expires_without_replaying(recovery_env):
    loop, _, ledger, broker, feed, clock, day = recovery_env
    candidate_id, _ = _seed_approved(ledger, feed, clock, day)

    summary = loop.recover_confirmation_state(clock.set_et(day, 16, 1))

    assert summary["expired"] == 1
    assert broker.get_orders() == []
    stored = {c.id: c for c in ledger.get_candidates(mode=Mode.PAPER)}
    assert stored[candidate_id].status is CandidateStatus.EXPIRED
    assert "missed execution" in stored[candidate_id].risk_note
    audit = ledger.get_audit(candidate_id=candidate_id)
    assert audit[-1].action == "expire_missed_execution"


def test_pre_cutoff_warning_is_sent_once(recovery_env):
    loop, _, ledger, _, feed, clock, day = recovery_env
    _, service = _seed_approved(ledger, feed, clock, day)
    loop._confirmation = service

    class TelegramStub:
        def __init__(self):
            self.notices = []

        def poll(self, _service, _now):
            return None

        def push_recovery_notice(self, text):
            self.notices.append(text)

    telegram = TelegramStub()
    loop.telegram = telegram
    clock.set_et(day, 12, 0)

    loop.on_confirm_poll()
    loop.on_confirm_poll()

    assert len(telegram.notices) == 1
    assert "不足 30 分钟" in telegram.notices[0]
