from datetime import datetime, timezone

from swing_trader.interfaces import Quote
from swing_trader.portfolio import (
    AccountEnvironment,
    EventSource,
    EventType,
    MarketScope,
    PortfolioEvent,
)
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.portfolio_marks import refresh_held_marks

NOW = datetime(2026, 9, 9, 7, 0, tzinfo=timezone.utc)


class _Feed:
    def __init__(self):
        self.calls = []

    def get_quote(self, symbol):
        self.calls.append(symbol)
        return Quote(symbol=symbol, ts=NOW, last=5.25)


def _opening(account_id, symbol, currency, key):
    return PortfolioEvent(
        account_id=account_id,
        event_type=EventType.OPENING_BALANCE,
        symbol=symbol,
        market=MarketScope.CN,
        currency=currency,
        qty=100,
        price=4.5,
        occurred_at=NOW,
        source=EventSource.MANUAL,
        idempotency_key=key,
        actor="human",
        surface="test",
    )


def test_refresh_held_marks_is_environment_scoped(tmp_path):
    journal = PortfolioJournal(url=f"sqlite:///{tmp_path / 'portfolio.db'}")
    live = journal.create_account(
        name="live",
        market_scope="CN",
        base_currency="CNY",
        environment=AccountEnvironment.LIVE,
    )
    paper = journal.create_account(
        name="paper",
        market_scope="CN",
        base_currency="CNY",
        environment=AccountEnvironment.PAPER,
    )
    journal.append_event(_opening(live.id, "510300.SS", "CNY", "live"))
    journal.append_event(_opening(paper.id, "588000.SS", "CNY", "paper"))
    feed = _Feed()

    report = refresh_held_marks(journal, feed, environment="live", clock=lambda: NOW)

    assert report.refreshed == ("510300.SS",)
    assert feed.calls == ["510300.SS"]
    assert journal.get_marks()["510300.SS"].price == 5.25
    assert "588000.SS" not in journal.get_marks()


def test_refresh_skips_same_symbol_with_conflicting_currencies(tmp_path):
    journal = PortfolioJournal(url=f"sqlite:///{tmp_path / 'portfolio.db'}")
    first = journal.create_account(name="a", market_scope="CN", base_currency="CNY")
    second = journal.create_account(name="b", market_scope="HK", base_currency="HKD")
    journal.append_event(_opening(first.id, "TEST", "CNY", "first"))
    journal.append_event(_opening(second.id, "TEST", "HKD", "second"))
    feed = _Feed()

    report = refresh_held_marks(journal, feed, environment="live", clock=lambda: NOW)

    assert report.skipped == ("TEST",)
    assert feed.calls == []
