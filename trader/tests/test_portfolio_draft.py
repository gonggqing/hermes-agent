"""Tests for the Phase 0.9 draft→confirm workflow (Loop.md P0.9, boundary #4).

The hard contract: free-form conversation cannot mutate holdings — only an
authenticated HUMAN confirmation turns a draft into an append-only event; the
LLM/system is refused; incomplete/ambiguous drafts cannot confirm; confirms are
idempotent + version-checked; and every attempt (applied or refused) is audited.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.ledger import Ledger
from swing_trader.portfolio import DraftStatus, EventType, MarketScope
from swing_trader.portfolio_draft import DraftResultCode, PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.schemas import Mode

NOW = datetime(2026, 7, 10, 15, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture()
def setup(tmp_path):
    url = f"sqlite:///{tmp_path/'p.db'}"
    journal = PortfolioJournal(url=url)
    account = journal.create_account(name="IBKR US", market_scope="US", base_currency="USD")
    clock = _Clock()
    svc = PortfolioDraftService(journal, clock=clock)
    return journal, account, svc, clock, url


def _complete_buy(svc, account_id, **over):
    kw = dict(
        account_id=account_id, event_type=EventType.BUY, symbol="NVDA",
        market=MarketScope.US, currency="USD", qty=3.0, price=208.5,
        commission=1.0, occurred_at=NOW, original_text="今天 208.5 买了 3 股 NVDA，手续费 1 美元",
    )
    kw.update(over)
    return svc.create_draft(**kw)


# ---------------------------------------------------------------- happy path


class TestConfirmHappyPath:
    def test_human_confirm_appends_event(self, setup):
        journal, account, svc, clock, _ = setup
        d = _complete_buy(svc, account.id)
        assert d.needs_clarification is False
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k1")
        assert r.ok and r.code is DraftResultCode.APPLIED
        assert r.event.symbol == "NVDA" and r.event.qty == 3.0 and r.event.actor == "gongqing"
        # draft is terminal + holdings updated
        assert svc.get_draft(d.id).status is DraftStatus.CONFIRMED
        (pos,) = journal.holdings(account.id).holdings
        assert pos.qty == 3.0 and pos.avg_cost == pytest.approx((3 * 208.5 + 1) / 3)

    def test_every_attempt_is_audited(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k1")
        actions = [e.action for e in journal.get_audit(draft_id=d.id)]
        assert "draft" in actions and "confirm" in actions


# ------------------------------------------------------- boundary #4: human-only


class TestHumanOnly:
    @pytest.mark.parametrize("actor,surface", [
        ("system", "web"), ("llm", "web"), ("hermes", "telegram"),
        ("gongqing", "system"),
    ])
    def test_system_or_llm_cannot_confirm(self, setup, actor, surface):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        r = svc.confirm_draft(d.id, actor=actor, surface=surface, idempotency_key="k")
        assert not r.ok and r.code is DraftResultCode.NOT_HUMAN
        assert svc.get_draft(d.id).status is DraftStatus.DRAFT  # unchanged
        assert journal.holdings(account.id).holdings == []  # no event
        # the refused attempt was audited
        refused = [e for e in journal.get_audit(draft_id=d.id) if not e.applied]
        assert any("not_human" in e.detail for e in refused)


# ----------------------------------------------------- incomplete / ambiguous


class TestIncompleteBlocksConfirm:
    def test_missing_qty_blocks_confirm(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id, qty=None)
        assert "quantity" in d.missing
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k")
        assert not r.ok and r.code is DraftResultCode.INCOMPLETE
        assert journal.holdings(account.id).holdings == []

    def test_unknown_account_flagged_missing(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, "ghost-account")
        assert "account" in d.missing

    def test_ambiguity_blocks_confirm(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id,
                          ambiguities=["order not yet filled?"])
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k")
        assert not r.ok and r.code is DraftResultCode.INCOMPLETE

    def test_price_unknown_still_confirms(self, setup):
        """Unknown price is allowed (cost basis unknown), not a blocker."""
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id, price=None, commission=None)
        assert d.needs_clarification is False
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k")
        assert r.ok
        (pos,) = journal.holdings(account.id).holdings
        assert pos.avg_cost is None  # never guessed


# --------------------------------------------------------- idempotency/version


class TestIdempotencyAndVersion:
    def test_replayed_confirm_no_double_event(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        r1 = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="same")
        r2 = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="same")
        assert r1.code is DraftResultCode.APPLIED and r2.code is DraftResultCode.REPLAYED
        assert len(journal.get_events(account.id)) == 1
        (pos,) = journal.holdings(account.id).holdings
        assert pos.qty == 3.0  # not 6

    def test_version_conflict_on_stale_confirm(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.edit_draft(d.id, actor="gongqing", surface="web", edits={"qty": 5.0})
        # confirm with the pre-edit version → conflict
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web",
                              idempotency_key="k", expected_version=1)
        assert not r.ok and r.code is DraftResultCode.VERSION_CONFLICT

    def test_terminal_draft_cannot_reconfirm_new_key(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k1")
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k2")
        assert not r.ok and r.code is DraftResultCode.TERMINAL
        assert len(journal.get_events(account.id)) == 1


# --------------------------------------------------------------- edit/reject


class TestEditReject:
    def test_edit_bumps_version_and_recomputes_gaps(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id, qty=None)
        assert "quantity" in d.missing
        r = svc.edit_draft(d.id, actor="gongqing", surface="web", edits={"qty": 4.0})
        assert r.ok and r.draft.version == 2 and r.draft.needs_clarification is False

    def test_non_editable_field_rejected(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        r = svc.edit_draft(d.id, actor="gongqing", surface="web",
                           edits={"status": "confirmed"})
        assert not r.ok and r.code is DraftResultCode.INVALID_EDIT
        assert svc.get_draft(d.id).status is DraftStatus.DRAFT

    def test_reject_makes_terminal(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.reject_draft(d.id, actor="gongqing", surface="web")
        assert svc.get_draft(d.id).status is DraftStatus.REJECTED
        r = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k")
        assert r.code is DraftResultCode.TERMINAL

    def test_edit_on_terminal_refused(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.reject_draft(d.id, actor="gongqing", surface="web")
        r = svc.edit_draft(d.id, actor="gongqing", surface="web", edits={"qty": 1.0})
        assert r.code is DraftResultCode.TERMINAL

    def test_unknown_draft(self, setup):
        _, _, svc, _, _ = setup
        assert svc.confirm_draft("ghost", actor="g", surface="web",
                                 idempotency_key="k").code is DraftResultCode.UNKNOWN_DRAFT

    def test_edit_unknown_draft(self, setup):
        _, _, svc, _, _ = setup
        r = svc.edit_draft("ghost", actor="g", surface="web", edits={"qty": 1.0})
        assert r.code is DraftResultCode.UNKNOWN_DRAFT

    def test_reject_unknown_draft(self, setup):
        _, _, svc, _, _ = setup
        r = svc.reject_draft("ghost", actor="g", surface="web")
        assert r.code is DraftResultCode.UNKNOWN_DRAFT

    def test_edit_invalid_value_rejected(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        r = svc.edit_draft(d.id, actor="gongqing", surface="web", edits={"qty": -3.0})
        assert not r.ok and r.code is DraftResultCode.INVALID_EDIT
        assert svc.get_draft(d.id).qty == 3.0  # unchanged

    def test_edit_version_conflict(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        r = svc.edit_draft(d.id, actor="gongqing", surface="web",
                           edits={"qty": 4.0}, expected_version=99)
        assert r.code is DraftResultCode.VERSION_CONFLICT

    def test_replay_returns_confirmed_event(self, setup):
        journal, account, svc, _, _ = setup
        d = _complete_buy(svc, account.id)
        svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="r")
        replay = svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="r")
        assert replay.code is DraftResultCode.REPLAYED
        assert replay.event is not None and replay.event.symbol == "NVDA"


# --------------------------------------------------------------- propose_close


class TestProposeClose:
    def test_derives_qty_from_holdings(self, setup):
        journal, account, svc, _, _ = setup
        # seed a position via a confirmed buy
        d0 = _complete_buy(svc, account.id, qty=10.0)
        svc.confirm_draft(d0.id, actor="gongqing", surface="web", idempotency_key="seed")
        # "NVDA 全清了"
        d = svc.propose_close(account_id=account.id, symbol="NVDA",
                              original_text="NVDA 全清了")
        assert d.event_type is EventType.SELL and d.qty == 10.0
        # still requires confirmation, and flags order-vs-fill
        assert any("completed trade" in a for a in d.ambiguities)

    def test_no_position_flags_ambiguity(self, setup):
        journal, account, svc, _, _ = setup
        d = svc.propose_close(account_id=account.id, symbol="AMD")
        assert any("no open position" in a for a in d.ambiguities)
        assert d.qty is None


# ------------------------------------------------------------------- expiry


class TestExpiry:
    def test_expire_old_drafts(self, setup):
        journal, account, svc, clock, url = setup
        d = _complete_buy(svc, account.id)
        clock.t = NOW + timedelta(hours=49)
        assert svc.expire_drafts() == 1
        assert svc.get_draft(d.id).status is DraftStatus.EXPIRED

    def test_fresh_drafts_not_expired(self, setup):
        journal, account, svc, clock, _ = setup
        _complete_buy(svc, account.id)
        clock.t = NOW + timedelta(hours=1)
        assert svc.expire_drafts() == 0


# ---------------------------------------------- boundary #1: ledger untouched


class TestNoLedgerContamination:
    def test_confirmed_draft_creates_no_ledger_fill(self, setup):
        journal, account, svc, _, url = setup
        ledger = Ledger(url=url)  # same DB file
        d = _complete_buy(svc, account.id)
        svc.confirm_draft(d.id, actor="gongqing", surface="web", idempotency_key="k")
        assert ledger.get_fills(Mode.PAPER) == [] and ledger.get_trades(Mode.PAPER) == []
        assert ledger.get_candidates() == []
