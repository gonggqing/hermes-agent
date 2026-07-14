"""Tests for the finance-bot slash-command handlers (finance_commands.py)."""

from __future__ import annotations

from swing_trader.finance_commands import COMMAND_MENU, make_command_handler
from swing_trader.name_override import NameOverrideStore
from swing_trader.portfolio_draft import PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal


class _RT:
    def __init__(self, journal=None, name_overrides=None, briefs=None):
        self.portfolio = journal
        self.name_overrides = name_overrides
        self.latest_briefs = briefs or {}


def _seed_journal(tmp_path):
    j = PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")
    acct = j.create_account(name="平安证券", market_scope="CN", base_currency="CNY")
    from datetime import datetime, timezone
    d = PortfolioDraftService(j, clock=lambda: datetime(2026, 7, 14, tzinfo=timezone.utc))
    draft = d.create_draft(account_id=acct.id, event_type="buy", symbol="159518.SZ",
                           market="CN", currency="CNY", qty=1200, price=1.1332,
                           note="平安证券 场内ETF；",
                           occurred_at=datetime(2026, 7, 14, tzinfo=timezone.utc))
    d.confirm_draft(draft.id, actor="gongqing", surface="web", idempotency_key="s1")
    return j, acct


def test_help_and_record_are_static(tmp_path):
    h = make_command_handler(_RT())
    assert "记账" in h("/帮助") and "分析" in h("/help")
    assert "买入" in h("/记账") and "卖出" in h("/记账")
    assert h("/start") is not None


def test_unknown_command_falls_through(tmp_path):
    h = make_command_handler(_RT())
    assert h("/new-session") is None      # not a finance command → fall through
    assert h("卖了 159813 200股") is None  # not a slash command at all


def test_holdings_command_renders_names_with_override(tmp_path):
    j, _ = _seed_journal(tmp_path)
    ov = NameOverrideStore(url=f"sqlite:///{tmp_path/'n.db'}")
    ov.set("159518.SZ", "标普油气ETF嘉实")
    out = make_command_handler(_RT(journal=j, name_overrides=ov))("/持仓")
    assert "平安证券" in out and "159518.SZ" in out
    assert "标普油气ETF嘉实" in out  # override name shows
    assert "1.1332" in out          # avg cost


def test_research_command_asks_region_then_renders(tmp_path):
    briefs = {"kr": {"as_of": "2026-07-14T09:11:00Z", "movers": {"top": [
        {"symbol": "005930.KS", "display_name": "三星电子", "dist_sma20_pct": -16.6}]}}}
    h = make_command_handler(_RT(briefs=briefs))
    # no region → asks which one
    ask = h("/研究")
    assert "cn" in ask and "kr" in ask and "哪个地区" in ask
    # with a region → renders that brief
    out = h("/brief kr")
    assert "韩国半导体" in out and "005930.KS" in out and "三星电子" in out


def test_command_menu_shape():
    # Telegram requires lowercase-latin command names; descriptions are English.
    cmds = [c for c, _ in COMMAND_MENU]
    assert cmds == ["holdings", "brief", "record", "help"]
    assert all(c.isascii() and c.islower() for c in cmds)
    assert all(d.isascii() for _, d in COMMAND_MENU)  # English descriptions
