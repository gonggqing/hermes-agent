"""Tests for update-holdings re-statement (propose_restate + confirm).

The hard invariant: correcting a holding's cost/qty/account RE-STATES the lot
(reverse the symbol's events + record the corrected OPENING_BALANCE) while
keeping CASH exactly unchanged (compensating for any reversed buy/sell cash) —
so a cost fix never silently shifts the separately-reconciled cash. Append-only
throughout; still human-confirmed (boundary #4).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swing_trader.portfolio import EventType, MarketScope, PortfolioEvent
from swing_trader.portfolio_draft import DraftResultCode, PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal

NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _ev(**kw):
    base = dict(occurred_at=NOW, actor="setup", surface="system",
                currency="CNY", created_at=NOW)
    base.update(kw)
    return PortfolioEvent(**base)


@pytest.fixture()
def env(tmp_path):
    j = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    a = j.create_account(name="平安证券", market_scope="CN", base_currency="CNY")
    b = j.create_account(name="蚂蚁财富", market_scope="CN", base_currency="CNY")
    j.append_event(_ev(account_id=a.id, event_type=EventType.OPENING_BALANCE,
                       amount=10000.0, idempotency_key="cash0"))
    # a BUY (has a cash effect) — the hard case for cash-neutral correction
    j.append_event(_ev(account_id=a.id, event_type=EventType.BUY, symbol="159518.SZ",
                       market=MarketScope.CN, qty=1200, price=1.1332,
                       idempotency_key="buy0"))
    return j, a, b, PortfolioDraftService(j, clock=lambda: NOW)


def _cash(j, acct_id):
    return next((c.amount for c in j.holdings(acct_id).cash if c.currency == "CNY"), None)


def _pos(j, acct_id, sym):
    return next((h for h in j.holdings(acct_id).holdings if h.symbol == sym), None)


def test_setup_cash_reflects_the_buy(env):
    j, a, _, _ = env
    assert _cash(j, a.id) == pytest.approx(10000 - 1200 * 1.1332)  # 8640.16
    assert _pos(j, a.id, "159518.SZ").avg_cost == pytest.approx(1.1332)


def test_restate_cost_corrects_lot_and_keeps_cash(env):
    j, a, _, svc = env
    cash_before = _cash(j, a.id)
    r = svc.propose_restate(account_id=a.id, symbol="159518.SZ", field="cost", value=1.20)
    assert r.ok, r.message
    c = svc.confirm_draft(r.draft.id, actor="gongqing", surface="telegram",
                          idempotency_key="conf-cost")
    assert c.ok and c.code is DraftResultCode.APPLIED, c.message
    pos = _pos(j, a.id, "159518.SZ")
    assert pos.qty == 1200 and pos.avg_cost == pytest.approx(1.20)  # cost fixed
    assert _cash(j, a.id) == pytest.approx(cash_before)             # cash UNCHANGED


def test_restate_qty_corrects_lot_and_keeps_cash(env):
    j, a, _, svc = env
    cash_before = _cash(j, a.id)
    r = svc.propose_restate(account_id=a.id, symbol="159518.SZ", field="qty", value=1000)
    c = svc.confirm_draft(r.draft.id, actor="gongqing", surface="telegram",
                          idempotency_key="conf-qty")
    assert c.ok, c.message
    pos = _pos(j, a.id, "159518.SZ")
    assert pos.qty == 1000 and pos.avg_cost == pytest.approx(1.1332)  # cost kept
    assert _cash(j, a.id) == pytest.approx(cash_before)


def test_restate_account_move_lot_moves_cash_stays(env):
    j, a, b, svc = env
    cash_a = _cash(j, a.id)
    r = svc.propose_restate(account_id=a.id, symbol="159518.SZ", field="account",
                            target_account_id=b.id)
    assert r.ok, r.message
    svc.confirm_draft(r.draft.id, actor="gongqing", surface="telegram",
                      idempotency_key="conf-acct")
    assert _pos(j, a.id, "159518.SZ") is None            # gone from 平安
    assert _pos(j, b.id, "159518.SZ").qty == 1200        # now in 蚂蚁
    assert _cash(j, a.id) == pytest.approx(cash_a)       # 平安 cash unchanged
    assert _cash(j, b.id) is None                        # no cash moved to 蚂蚁


def test_restate_card_shows_correction_and_cash_note(env):
    from swing_trader.telegram_gateway import render_draft_card

    _, a, _, svc = env
    r = svc.propose_restate(account_id=a.id, symbol="159518.SZ", field="cost", value=1.20)
    card = render_draft_card(r.draft, account_label="平安证券")
    assert "更正持仓" in card and "159518.SZ" in card
    assert "1.1332" in card and "1.2" in card       # current → target cost
    assert "现金不变" in card                          # the key reassurance


def test_restate_refuses_unheld_symbol(env):
    _, a, _, svc = env
    r = svc.propose_restate(account_id=a.id, symbol="000001.SZ", field="cost", value=9)
    assert not r.ok and r.code is DraftResultCode.INCOMPLETE


def test_restate_confirm_is_idempotent(env):
    j, a, _, svc = env
    r = svc.propose_restate(account_id=a.id, symbol="159518.SZ", field="cost", value=1.20)
    k = "conf-once"
    c1 = svc.confirm_draft(r.draft.id, actor="gongqing", surface="telegram", idempotency_key=k)
    c2 = svc.confirm_draft(r.draft.id, actor="gongqing", surface="telegram", idempotency_key=k)
    assert c1.ok and c2.ok and c2.code is DraftResultCode.REPLAYED
    assert _pos(j, a.id, "159518.SZ").avg_cost == pytest.approx(1.20)  # applied once
