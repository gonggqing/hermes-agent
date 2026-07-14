"""Tests for the finance-bot DM trade recorder (trade_record.py).

Ties the parser to account resolution + draft creation. The output is a PROPOSED
draft that lands on a confirm card — so an unresolved account rides as an
ambiguity (card guides the user), never a silent guess.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swing_trader.portfolio import EventType, MarketScope
from swing_trader.portfolio_draft import PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.trade_record import make_trade_recorder

NOW = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)


class _RT:
    """Minimal FinanceRuntime stand-in for the recorder."""

    def __init__(self, journal, drafts):
        self.portfolio = journal
        self.portfolio_drafts = drafts
        self.clock = lambda: NOW


@pytest.fixture()
def env(tmp_path):
    journal = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    pingan = journal.create_account(name="平安证券", market_scope="CN", base_currency="CNY")
    mayi = journal.create_account(name="蚂蚁财富", market_scope="CN", base_currency="CNY")
    drafts = PortfolioDraftService(journal, clock=lambda: NOW)
    # Seed a 159813 holding in 平安证券 (confirm a buy) so sells resolve to it.
    seed = drafts.create_draft(
        account_id=pingan.id, event_type="buy", symbol="159813.SZ",
        market="CN", currency="CNY", qty=400, price=1.2115, occurred_at=NOW)
    drafts.confirm_draft(seed.id, actor="gongqing", surface="web",
                         idempotency_key="seed-1")
    fund = drafts.create_draft(
        account_id=mayi.id, event_type="opening_balance", symbol="017470",
        market="CN", currency="CNY", qty=2433.93, price=2.5274,
        occurred_at=NOW)
    drafts.confirm_draft(fund.id, actor="gongqing", surface="web",
                         idempotency_key="seed-fund")
    return make_trade_recorder(_RT(journal, drafts)), journal, pingan, mayi


def test_sell_resolves_to_holding_account(env):
    record, journal, pingan, _ = env
    out = record("卖了 159813 200股 @1.893")
    assert out is not None
    draft, label, ack = out
    assert draft.account_id == pingan.id  # resolved: only 平安 holds 159813
    assert draft.event_type is EventType.SELL and draft.symbol == "159813.SZ"
    assert draft.qty == 200 and draft.price == 1.893
    assert draft.market is MarketScope.CN and draft.currency == "CNY"
    assert not draft.needs_clarification  # complete → card can confirm directly
    assert label == "平安证券" and "159813" in ack and "卖出" in ack


def test_account_name_hint_wins(env):
    record, _, _, mayi = env
    draft, label, _ = record("蚂蚁财富 买入 000001 100股 成交价12")
    assert draft.account_id == mayi.id and label == "蚂蚁财富"
    assert draft.symbol == "000001.SZ" and draft.event_type is EventType.BUY


def test_bare_fund_sell_uses_exact_existing_holding(env):
    record, _, _, mayi = env
    draft, label, ack = record("蚂蚁财富，017470，以成交价3.500卖了400份")

    assert draft.account_id == mayi.id and label == "蚂蚁财富"
    assert draft.symbol == "017470"
    assert draft.event_type is EventType.SELL
    assert draft.market is MarketScope.CN and draft.currency == "CNY"
    assert not draft.needs_clarification
    assert "017470 400股 @ 3.5" in ack


def test_explicit_account_cannot_bypass_unheld_sell_check(env):
    record, _, _, mayi = env
    draft, label, _ = record("蚂蚁财富，017471，以成交价3.500卖了400份")

    assert draft.account_id == mayi.id and label == "蚂蚁财富"
    assert draft.needs_clarification
    assert any("未持有 017471" in a for a in draft.ambiguities)


def test_oversell_is_flagged_before_card_confirmation(env):
    record, _, _, _ = env
    draft, _, _ = record("蚂蚁财富，017470，以成交价3.500卖了3000份")
    assert draft.symbol == "017470"
    assert any("超过当前持仓" in a for a in draft.ambiguities)


def test_new_symbol_two_accounts_flags_account(env):
    record, _, _, _ = env
    # 588200 not held, 2 accounts, no name hint → account unresolved (INCOMPLETE)
    draft, label, _ = record("买入 588200 300股 成交价3.179")
    assert draft.account_id is None and label == ""
    assert draft.needs_clarification
    assert any("账户" in a for a in draft.ambiguities)  # card guides to specify


def test_non_trade_message_returns_none(env):
    record, _, _, _ = env
    assert record("分析一下 159813 走势") is None
    assert record("159813 现在多少钱") is None
