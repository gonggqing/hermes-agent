"""Tests for swing_trader.confirmation (Loop.md §5.6, §3 authority guardrails)."""

from datetime import datetime, timezone

import pytest

from swing_trader.confirmation import (
    ActResult,
    ConfirmationService,
    ResultCode,
    Surface,
)
from swing_trader.ledger import Ledger
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Side,
)

IN_WINDOW = datetime(2026, 7, 13, 15, 45, tzinfo=timezone.utc)  # 11:45 EDT
BEFORE_WINDOW = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)  # 11:00 EDT
AFTER_CUTOFF = datetime(2026, 7, 13, 16, 31, tzinfo=timezone.utc)  # 12:31 EDT
IN_WINDOW_EST = datetime(2026, 1, 12, 16, 45, tzinfo=timezone.utc)  # 11:45 EST


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA", side=Side.BUY, qty=2, order_type=OrderType.BRACKET,
        limit=99.5, stop=91.5, tp=111.5, rationale="test", confidence=0.7,
        ref_px=100.0, status=CandidateStatus.RISK_APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path/'c.db'}")
    service = ConfirmationService(ledger, mode=Mode.PAPER)
    return ledger, service


def publish_one(ledger, service, now=IN_WINDOW, **kw) -> CandidateOrder:
    c = candidate(**kw)
    ledger.record_candidate(c, Mode.PAPER)
    published = service.publish([c], now)
    assert len(published) == 1
    return published[0]


class TestPublish:
    def test_publish_marks_pushed_and_audits(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        assert c.status is CandidateStatus.PUSHED
        assert ledger.get_candidates(status=CandidateStatus.PUSHED)
        audit = ledger.get_audit(candidate_id=c.id)
        assert audit[0].action == "publish"
        assert audit[0].surface == "system"
        assert audit[0].applied is True

    def test_only_risk_approved_published(self, env):
        ledger, service = env
        c = candidate(status=CandidateStatus.PROPOSED)
        ledger.record_candidate(c, Mode.PAPER)
        assert service.publish([c], IN_WINDOW) == []

    def test_publish_refused_outside_window(self, env):
        ledger, service = env
        c = candidate()
        ledger.record_candidate(c, Mode.PAPER)
        assert service.publish([c], BEFORE_WINDOW) == []
        assert service.publish([c], AFTER_CUTOFF) == []

    def test_est_window_also_works(self, env):
        ledger, service = env
        c = publish_one(ledger, service, now=IN_WINDOW_EST)
        assert c.status is CandidateStatus.PUSHED


class TestApproveRejectFlow:
    def test_approve_from_web(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "approve", "user@local", Surface.WEB, "k1", IN_WINDOW)
        assert res.ok and res.code is ResultCode.APPLIED
        assert res.candidate.status is CandidateStatus.APPROVED
        assert service.finalized().human_approved
        audit = ledger.get_audit(candidate_id=c.id, idempotency_key="k1")
        assert len(audit) == 1
        assert audit[0].actor == "user@local"
        assert audit[0].surface == "web"

    def test_reject_from_desktop(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "reject", "user@local", "desktop", "k2", IN_WINDOW)
        assert res.ok
        assert res.candidate.status is CandidateStatus.REJECTED
        assert service.finalized().rejected

    def test_idempotent_replay_same_key(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        first = service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW)
        replay = service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW)
        assert replay.code is ResultCode.REPLAYED
        assert replay.ok
        # exactly ONE applied audit row for that key -> no double transition
        applied = [e for e in ledger.get_audit(candidate_id=c.id)
                   if e.idempotency_key == "k1" and e.applied]
        assert len(applied) == 1

    def test_replay_survives_restart_via_ledger(self, env, tmp_path):
        ledger, service = env
        c = publish_one(ledger, service)
        service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW)
        # a fresh service instance over the same ledger sees the audit trail
        service2 = ConfirmationService(ledger, mode=Mode.PAPER)
        replay = service2.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW)
        assert replay.code is ResultCode.REPLAYED

    def test_double_approve_different_surfaces_blocked(self, env):
        """Two surfaces can never double-approve (Loop.md §5.6)."""
        ledger, service = env
        c = publish_one(ledger, service)
        assert service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW).ok
        second = service.act(c.id, "approve", "u", Surface.TELEGRAM, "k2", IN_WINDOW)
        assert not second.ok
        assert second.code is ResultCode.TERMINAL
        refused = [e for e in ledger.get_audit(candidate_id=c.id) if not e.applied]
        assert len(refused) == 1

    def test_window_closed_refused_and_audited(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "approve", "u", Surface.WEB, "k1", AFTER_CUTOFF)
        assert not res.ok and res.code is ResultCode.WINDOW_CLOSED
        assert any(not e.applied for e in ledger.get_audit(candidate_id=c.id))

    def test_unknown_candidate(self, env):
        _, service = env
        res = service.act("nope", "approve", "u", Surface.WEB, "k", IN_WINDOW)
        assert res.code is ResultCode.UNKNOWN_CANDIDATE

    def test_system_surface_cannot_approve(self, env):
        """Loop.md §3: model/system tools are not approval authority."""
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "approve", "agent", Surface.SYSTEM, "k", IN_WINDOW)
        assert not res.ok and res.code is ResultCode.INVALID_ACTION
        entry = service.get(c.id)
        assert entry[0].status is CandidateStatus.PUSHED  # unchanged

    def test_version_conflict_on_stale_card(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW,
                          expected_version=99)
        assert not res.ok and res.code is ResultCode.VERSION_CONFLICT

    def test_invalid_action(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "yeet", "u", Surface.WEB, "k1", IN_WINDOW)
        assert res.code is ResultCode.INVALID_ACTION


class TestEdit:
    def test_valid_edit_counts_as_approval_and_bumps_version(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "edit", "u", Surface.DESKTOP, "k1", IN_WINDOW,
                          edits={"qty": 1, "stop": 93.0})
        assert res.ok and res.code is ResultCode.APPLIED
        assert res.candidate.status is CandidateStatus.EDITED
        assert res.candidate.qty == 1
        assert res.candidate.stop == 93.0
        assert res.version == 2
        assert service.finalized().human_approved

    def test_edit_cannot_touch_symbol_or_side(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        for bad in ({"symbol": "TSLA"}, {"side": "SELL"}, {"confidence": 1.0}):
            res = service.act(c.id, "edit", "u", Surface.WEB, f"k{bad}", IN_WINDOW,
                              edits=bad)
            assert not res.ok and res.code is ResultCode.INVALID_EDIT
        assert service.get(c.id)[0].status is CandidateStatus.PUSHED

    def test_edit_cannot_strip_protection(self, env):
        """Loop.md §4: never leave a position without a resting stop."""
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "edit", "u", Surface.WEB, "k1", IN_WINDOW,
                          edits={"stop": None})
        assert not res.ok and res.code is ResultCode.INVALID_EDIT

    def test_edit_qty_zero_refused(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "edit", "u", Surface.WEB, "k1", IN_WINDOW,
                          edits={"qty": 0})
        assert not res.ok and res.code is ResultCode.INVALID_EDIT

    def test_edit_reruns_risk_hook(self, env, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'r.db'}")
        service = ConfirmationService(
            ledger, mode=Mode.PAPER,
            revalidate=lambda c: (c.qty <= 2, "qty too large for risk cap"),
        )
        c = candidate()
        ledger.record_candidate(c, Mode.PAPER)
        service.publish([c], IN_WINDOW)
        bad = service.act(c.id, "edit", "u", Surface.WEB, "k1", IN_WINDOW,
                          edits={"qty": 50})
        assert not bad.ok and "risk re-validation" in bad.message
        good = service.act(c.id, "edit", "u", Surface.WEB, "k2", IN_WINDOW,
                           edits={"qty": 1})
        assert good.ok

    def test_empty_edit_refused(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        res = service.act(c.id, "edit", "u", Surface.WEB, "k1", IN_WINDOW, edits={})
        assert res.code is ResultCode.INVALID_EDIT


class TestExpiry:
    def test_expire_before_cutoff_is_noop(self, env):
        ledger, service = env
        publish_one(ledger, service)
        assert service.expire(IN_WINDOW) == []

    def test_expire_after_cutoff(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        expired = service.expire(AFTER_CUTOFF)
        assert len(expired) == 1
        assert expired[0].status is CandidateStatus.EXPIRED
        assert service.finalized().expired
        audit = ledger.get_audit(candidate_id=c.id)
        assert audit[-1].action == "expire"

    def test_settled_candidates_do_not_expire(self, env):
        ledger, service = env
        c = publish_one(ledger, service)
        service.act(c.id, "approve", "u", Surface.WEB, "k1", IN_WINDOW)
        assert service.expire(AFTER_CUTOFF) == []


def test_naive_datetime_rejected(env):
    ledger, service = env
    with pytest.raises(ValueError, match="timezone-aware"):
        service.in_window(datetime(2026, 7, 13, 15, 45))
