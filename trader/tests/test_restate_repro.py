"""Regression: a trade between propose_restate and confirm expires the card."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.portfolio import EventType, MarketScope, PortfolioEvent
from swing_trader.portfolio import DraftStatus
from swing_trader.portfolio_draft import DraftResultCode, PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _ev(**kw):
    base = dict(occurred_at=NOW, actor="setup", surface="system",
                currency="CNY", created_at=NOW)
    base.update(kw)
    return PortfolioEvent(**base)


def _cash(j, acct_id):
    return next((c.amount for c in j.holdings(acct_id).cash if c.currency == "CNY"), None)


def _pos(j, acct_id, sym):
    return next((h for h in j.holdings(acct_id).holdings if h.symbol == sym), None)


def test_buy_between_propose_and_confirm(tmp_path):
    j = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    a = j.create_account(name="平安证券", market_scope="CN", base_currency="CNY")
    # opening cash 100000, buy CCC 1000@1.0 -> cash 99000, pos 1000@1.0
    j.append_event(_ev(account_id=a.id, event_type=EventType.OPENING_BALANCE,
                       amount=100000.0, idempotency_key="cash0"))
    j.append_event(_ev(account_id=a.id, event_type=EventType.BUY, symbol="CCC",
                       market=MarketScope.CN, qty=1000, price=1.0,
                       idempotency_key="b1"))
    svc = PortfolioDraftService(j, clock=lambda: NOW)

    assert _pos(j, a.id, "CCC").qty == 1000
    assert _cash(j, a.id) == pytest.approx(99000.0)

    # 1) propose cost restate to 1.20 (draft freezes qty=1000, price=1.20)
    r = svc.propose_restate(account_id=a.id, symbol="CCC", field="cost", value=1.20)
    assert r.ok
    draft_id = r.draft.id
    assert r.draft.qty == 1000 and r.draft.price == 1.20

    # 2) BEFORE confirm, a second buy is recorded (normal trade_recorder flow)
    later = NOW + timedelta(hours=1)
    j.append_event(PortfolioEvent(
        account_id=a.id, event_type=EventType.BUY, symbol="CCC",
        market=MarketScope.CN, currency="CNY", qty=500, price=3.0,
        occurred_at=later, idempotency_key="b2", actor="gongqing",
        surface="telegram", created_at=later))

    # real position now: 1500 shares, avg 1.6667; cash 97500
    assert _pos(j, a.id, "CCC").qty == 1500
    cash_before_confirm = _cash(j, a.id)
    assert cash_before_confirm == pytest.approx(97500.0)

    # 3) confirm the STALE cost card
    c = svc.confirm_draft(draft_id, actor="gongqing", surface="telegram",
                          idempotency_key="conf-cost")
    assert not c.ok
    assert c.code is DraftResultCode.VERSION_CONFLICT
    assert c.draft.status is DraftStatus.EXPIRED

    pos = _pos(j, a.id, "CCC")
    cash_after = _cash(j, a.id)
    assert pos.qty == pytest.approx(1500.0)
    assert pos.avg_cost == pytest.approx(5 / 3)
    assert cash_after == pytest.approx(cash_before_confirm)


def test_sell_between_propose_and_confirm(tmp_path):
    j = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    a = j.create_account(name="平安证券", market_scope="CN", base_currency="CNY")
    j.append_event(_ev(account_id=a.id, event_type=EventType.OPENING_BALANCE,
                       amount=100000.0, idempotency_key="cash0"))
    j.append_event(_ev(account_id=a.id, event_type=EventType.BUY, symbol="CCC",
                       market=MarketScope.CN, qty=1000, price=1.0,
                       idempotency_key="b1"))
    svc = PortfolioDraftService(j, clock=lambda: NOW)

    r = svc.propose_restate(account_id=a.id, symbol="CCC", field="cost", value=1.20)
    draft_id = r.draft.id

    later = NOW + timedelta(hours=1)
    j.append_event(PortfolioEvent(
        account_id=a.id, event_type=EventType.SELL, symbol="CCC",
        market=MarketScope.CN, currency="CNY", qty=600, price=2.0,
        occurred_at=later, idempotency_key="s1", actor="gongqing",
        surface="telegram", created_at=later))

    # real position now: 400 shares; cash 99000 + 1200 = 100200
    assert _pos(j, a.id, "CCC").qty == 400
    assert _cash(j, a.id) == pytest.approx(100200.0)

    c = svc.confirm_draft(draft_id, actor="gongqing", surface="telegram",
                          idempotency_key="conf-cost")
    assert not c.ok and c.code is DraftResultCode.VERSION_CONFLICT

    pos = _pos(j, a.id, "CCC")
    assert pos.qty == pytest.approx(400.0)
    assert pos.avg_cost == pytest.approx(1.0)
    assert _cash(j, a.id) == pytest.approx(100200.0)
