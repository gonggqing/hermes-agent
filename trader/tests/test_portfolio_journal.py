"""Tests for the Phase 0.9 Portfolio Journal (Loop.md §7 P0.9).

Covers the boundary contract: append-only events + compensating corrections,
holdings DERIVED from events (rebuildable, never a mutable qty column),
idempotent commits and duplicate-broker-execution dedup, unknown cost basis
never guessed, multi-account isolation, and — critically — that opening
balances / external trades DO NOT create system fills or trades in the trading
ledger (boundary #1: no strategy-stat contamination).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from swing_trader.ledger import Ledger
from swing_trader.portfolio import (
    AccountType,
    EventSource,
    EventType,
    MarketScope,
    PortfolioEvent,
    ProviderKind,
    derive_holdings,
)
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.schemas import Mode

T0 = datetime(2026, 7, 1, 14, 30, tzinfo=timezone.utc)


def _dt(day: int, hh: int = 14) -> datetime:
    return datetime(2026, 7, day, hh, 0, tzinfo=timezone.utc)


@pytest.fixture()
def journal(tmp_path) -> PortfolioJournal:
    return PortfolioJournal(url=f"sqlite:///{tmp_path/'portfolio.db'}")


def test_migration_adds_missing_column(tmp_path):
    """An existing DB whose portfolio_drafts table predates reverses_event_id
    (create_all never ALTERs) must get the column added on init, else draft
    writes fail with 'no such column' (the live-service bug)."""
    import sqlite3

    dbfile = tmp_path / "old.db"
    url = f"sqlite:///{dbfile}"
    PortfolioJournal(url=url)  # create current schema
    # Simulate an OLD DB: drop the column SQLite-style (rebuild without it).
    con = sqlite3.connect(dbfile)
    con.execute("ALTER TABLE portfolio_drafts DROP COLUMN reverses_event_id")
    con.commit()
    con.close()
    def cols():
        return [r[1] for r in sqlite3.connect(dbfile).execute(
            "PRAGMA table_info(portfolio_drafts)")]

    assert "reverses_event_id" not in cols()  # old schema
    PortfolioJournal(url=url)  # re-open → migration runs
    assert "reverses_event_id" in cols()  # column restored


def _us_account(journal: PortfolioJournal):
    return journal.create_account(
        name="IBKR US", market_scope=MarketScope.US, base_currency="USD",
        provider=ProviderKind.MANUAL,
    )


def _event(account_id: str, **over) -> PortfolioEvent:
    base = dict(
        account_id=account_id,
        event_type=EventType.BUY,
        symbol="NVDA",
        market=MarketScope.US,
        currency="USD",
        qty=10.0,
        price=100.0,
        occurred_at=T0,
        source=EventSource.MANUAL,
        idempotency_key=f"k-{over.get('idempotency_key', 'x')}",
        actor="gongqing",
        surface="web",
    )
    base.update(over)
    # keep a stable idempotency key unless the caller set one explicitly
    if "idempotency_key" not in over:
        base["idempotency_key"] = "k-default"
    return PortfolioEvent(**base)


# ----------------------------------------------------------------- accounts


class TestAccounts:
    def test_create_list_get(self, journal):
        a = _us_account(journal)
        assert a.market_scope is MarketScope.US
        assert a.base_currency == "USD"
        assert journal.get_account(a.id).name == "IBKR US"
        assert [x.id for x in journal.list_accounts()] == [a.id]

    def test_get_unknown_returns_none(self, journal):
        assert journal.get_account("nope") is None

    def test_update_config_bumps_updated_at(self, journal):
        a = _us_account(journal)
        later = datetime(2026, 7, 2, tzinfo=timezone.utc)
        b = journal.update_account(a.id, name="IBKR US Main",
                                   include_in_risk=False, now=later)
        assert b.name == "IBKR US Main"
        assert b.include_in_risk is False
        assert b.updated_at == later
        assert b.created_at == a.created_at  # history/creation preserved

    def test_update_unknown_raises(self, journal):
        with pytest.raises(ValueError, match="unknown account"):
            journal.update_account("nope", name="x")

    def test_currency_normalised_upper(self, journal):
        a = journal.create_account(name="A", market_scope="HK", base_currency="hkd")
        assert a.base_currency == "HKD"
        assert a.market_scope is MarketScope.HK


# ------------------------------------------------------------ append-only


class TestAppendOnly:
    def test_append_and_read_back(self, journal):
        a = _us_account(journal)
        ev, created = journal.append_event(_event(a.id))
        assert created is True
        got = journal.get_event(ev.id)
        assert got.symbol == "NVDA" and got.qty == 10.0 and got.price == 100.0
        assert got.event_type is EventType.BUY

    def test_events_sorted_by_occurred_then_created(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, occurred_at=_dt(3), idempotency_key="b"))
        journal.append_event(_event(a.id, occurred_at=_dt(1), idempotency_key="a"))
        journal.append_event(_event(a.id, occurred_at=_dt(2), idempotency_key="c"))
        days = [e.occurred_at.day for e in journal.get_events(a.id)]
        assert days == [1, 2, 3]

    def test_no_update_or_delete_api(self, journal):
        """Journal is append-only: only corrections, never mutation/removal."""
        for forbidden in ("update_event", "delete_event", "remove_event"):
            assert not hasattr(journal, forbidden)

    def test_append_to_unknown_account_raises(self, journal):
        with pytest.raises(ValueError, match="unknown account"):
            journal.append_event(_event("ghost"))

    def test_filter_by_symbol(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, symbol="NVDA", idempotency_key="n"))
        journal.append_event(_event(a.id, symbol="AMD", idempotency_key="d"))
        assert {e.symbol for e in journal.get_events(a.id, symbol="AMD")} == {"AMD"}


# ------------------------------------------------------------ idempotency


class TestIdempotency:
    def test_same_idempotency_key_no_duplicate(self, journal):
        a = _us_account(journal)
        e1, c1 = journal.append_event(_event(a.id, idempotency_key="same"))
        e2, c2 = journal.append_event(_event(a.id, idempotency_key="same", qty=999))
        assert c1 is True and c2 is False
        assert e2.id == e1.id and e2.qty == 10.0  # original wins, no double count
        assert len(journal.get_events(a.id)) == 1

    def test_duplicate_broker_execution_deduped_by_external_id(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, external_id="EXE-1", idempotency_key="i1",
                                    source=EventSource.IBKR_FLEX))
        _, created = journal.append_event(
            _event(a.id, external_id="EXE-1", idempotency_key="i2",
                   source=EventSource.IBKR_FLEX))
        assert created is False
        assert len(journal.get_events(a.id)) == 1

    def test_same_external_id_different_account_not_deduped(self, journal):
        a = _us_account(journal)
        b = journal.create_account(name="Other", market_scope="US", base_currency="USD")
        journal.append_event(_event(a.id, external_id="X", idempotency_key="i1"))
        _, created = journal.append_event(_event(b.id, external_id="X", idempotency_key="i2"))
        assert created is True


# ------------------------------------------------------- holdings projection


class TestHoldingsProjection:
    def test_opening_plus_buys_weighted_avg_cost(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=90.0, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.BUY,
                                    qty=10, price=110.0, idempotency_key="buy"))
        h = journal.holdings(a.id)
        (pos,) = h.holdings
        assert pos.symbol == "NVDA" and pos.qty == 20.0
        assert pos.cost_basis_known is True
        assert pos.avg_cost == pytest.approx(100.0)  # (10*90 + 10*110)/20

    def test_sell_reduces_qty_keeps_avg_cost(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=100.0, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.SELL,
                                    qty=4, price=130.0, idempotency_key="sell"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.qty == 6.0
        assert pos.avg_cost == pytest.approx(100.0)  # avg method: unchanged

    def test_fully_closed_symbol_drops_out(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=100.0, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.SELL,
                                    qty=10, price=120.0, idempotency_key="sell"))
        assert journal.holdings(a.id).holdings == []

    def test_unknown_price_makes_cost_unknown_never_guessed(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=None, idempotency_key="open"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.qty == 10.0
        assert pos.cost_basis_known is False
        assert pos.avg_cost is None  # never synthesized

    def test_unknown_price_is_sticky_across_a_known_buy(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=None, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.BUY,
                                    qty=10, price=100.0, idempotency_key="buy"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.avg_cost is None and pos.cost_basis_known is False

    def test_commission_included_in_cost_basis(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10,
                                    price=100.0, commission=5.0, idempotency_key="b"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.avg_cost == pytest.approx(100.5)  # (1000 + 5)/10

    def test_split_scales_qty_and_avg_cost(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10,
                                    price=100.0, idempotency_key="b"))
        journal.append_event(_event(a.id, event_type=EventType.SPLIT, qty=2.0,
                                    price=None, occurred_at=_dt(5), idempotency_key="s"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.qty == 20.0
        assert pos.avg_cost == pytest.approx(50.0)  # cost/share halves

    def test_correction_reverses_a_prior_event(self, journal):
        a = _us_account(journal)
        bad, _ = journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10,
                                             price=100.0, idempotency_key="bad"))
        # A CORRECTION reversing the erroneous BUY nullifies its effect.
        journal.append_event(_event(a.id, event_type=EventType.CORRECTION,
                                    qty=0.0, price=None, reverses_event_id=bad.id,
                                    occurred_at=_dt(6), idempotency_key="fix"))
        assert journal.holdings(a.id).holdings == []

    def test_correction_must_reference_existing_event(self, journal):
        a = _us_account(journal)
        with pytest.raises(ValueError, match="reverses_event_id"):
            journal.append_event(_event(a.id, event_type=EventType.CORRECTION,
                                        reverses_event_id="ghost", idempotency_key="x"))

    def test_projection_is_pure_and_rebuildable(self, journal):
        """holdings() derives from events alone — re-deriving the fetched events
        yields the identical projection (no hidden mutable state)."""
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=100.0, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.SELL, qty=3,
                                    price=120.0, occurred_at=_dt(4), idempotency_key="s"))
        via_journal = journal.holdings(a.id)
        rebuilt = derive_holdings(a.id, journal.get_events(a.id))
        assert [(h.symbol, h.qty, h.avg_cost) for h in via_journal.holdings] == \
               [(h.symbol, h.qty, h.avg_cost) for h in rebuilt.holdings]
        assert via_journal.as_of == rebuilt.as_of


# --------------------------------------------------------------- cash


class TestCashProjection:
    def test_opening_cash_dividend_fee(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    symbol=None, amount=10_000.0, qty=0.0, price=None,
                                    idempotency_key="cash"))
        journal.append_event(_event(a.id, event_type=EventType.DIVIDEND, symbol=None,
                                    amount=50.0, qty=0.0, price=None, occurred_at=_dt(2),
                                    idempotency_key="div"))
        journal.append_event(_event(a.id, event_type=EventType.FEE, symbol=None,
                                    amount=8.0, qty=0.0, price=None, occurred_at=_dt(3),
                                    idempotency_key="fee"))
        (cash,) = journal.holdings(a.id).cash
        assert cash.currency == "USD"
        assert cash.known is True
        assert cash.amount == pytest.approx(10_000 + 50 - 8)

    def test_buy_reduces_cash_sell_increases(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    symbol=None, amount=5_000.0, qty=0.0, price=None,
                                    idempotency_key="cash"))
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10, price=100.0,
                                    commission=1.0, occurred_at=_dt(2), idempotency_key="b"))
        journal.append_event(_event(a.id, event_type=EventType.SELL, qty=5, price=120.0,
                                    commission=1.0, occurred_at=_dt(3), idempotency_key="s"))
        (cash,) = journal.holdings(a.id).cash
        # 5000 - (1000+1) + (600-1) = 4598
        assert cash.amount == pytest.approx(4598.0)

    def test_cash_transfer_signed_amount(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.CASH_TRANSFER, symbol=None,
                                    amount=2_000.0, qty=0.0, price=None, idempotency_key="in"))
        journal.append_event(_event(a.id, event_type=EventType.CASH_TRANSFER, symbol=None,
                                    amount=-500.0, qty=0.0, price=None, occurred_at=_dt(2),
                                    idempotency_key="out"))
        (cash,) = journal.holdings(a.id).cash
        assert cash.amount == pytest.approx(1_500.0) and cash.known is True

    def test_unknown_price_makes_cash_unknown(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    symbol=None, amount=5_000.0, qty=0.0, price=None,
                                    idempotency_key="cash"))
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10, price=None,
                                    occurred_at=_dt(2), idempotency_key="b"))
        (cash,) = journal.holdings(a.id).cash
        assert cash.known is False and cash.amount is None


# ------------------------------------------------------ multi-account


class TestMultiAccount:
    def test_events_scoped_by_account(self, journal):
        us = _us_account(journal)
        hk = journal.create_account(name="HK", market_scope="HK", base_currency="HKD")
        journal.append_event(_event(us.id, symbol="NVDA", idempotency_key="u"))
        journal.append_event(_event(hk.id, symbol="0700.HK", currency="HKD",
                                    market=MarketScope.HK, idempotency_key="h"))
        assert {h.symbol for h in journal.holdings(us.id).holdings} == {"NVDA"}
        assert {h.symbol for h in journal.holdings(hk.id).holdings} == {"0700.HK"}

    def test_correction_cannot_cross_accounts(self, journal):
        us = _us_account(journal)
        hk = journal.create_account(name="HK", market_scope="HK", base_currency="HKD")
        ev, _ = journal.append_event(_event(us.id, idempotency_key="u"))
        with pytest.raises(ValueError, match="not found"):
            journal.append_event(_event(hk.id, event_type=EventType.CORRECTION,
                                        currency="HKD", reverses_event_id=ev.id,
                                        idempotency_key="x"))


# ------------------------------------ boundary #1: no ledger contamination


class TestNoLedgerContamination:
    def test_portfolio_events_do_not_create_fills_or_trades(self, tmp_path):
        """Opening balances / external trades must NEVER become system fills or
        trades (Loop.md P0.9 boundary #1) — even sharing one DB file."""
        url = f"sqlite:///{tmp_path/'shared.db'}"
        ledger = Ledger(url=url)
        journal = PortfolioJournal(url=url)
        a = journal.create_account(name="US", market_scope="US", base_currency="USD")
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=100, price=90.0, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=10, price=100.0,
                                    occurred_at=_dt(2), idempotency_key="buy"))
        # The trading ledger stays pristine — no fills, trades, or candidates.
        assert ledger.get_fills(Mode.PAPER) == []
        assert ledger.get_fills(Mode.LIVE) == []
        assert ledger.get_trades(Mode.PAPER) == []
        assert ledger.get_trades(Mode.LIVE) == []
        assert ledger.get_candidates() == []
        assert ledger.stats(Mode.PAPER).n_closed == 0
        # And the portfolio holdings are intact.
        assert journal.holdings(a.id).holdings[0].qty == 110.0


# --------------------------------------------------------- validation


class TestValidation:
    def test_buy_requires_symbol(self):
        with pytest.raises(ValueError, match="requires a symbol"):
            PortfolioEvent(account_id="a", event_type=EventType.BUY, currency="USD",
                           qty=10, price=100.0, occurred_at=T0, idempotency_key="k",
                           actor="x", surface="web")

    def test_correction_requires_reverses_id(self):
        with pytest.raises(ValueError, match="reverses_event_id"):
            PortfolioEvent(account_id="a", event_type=EventType.CORRECTION,
                           currency="USD", occurred_at=T0, idempotency_key="k",
                           actor="x", surface="web")

    def test_cash_event_requires_amount(self):
        with pytest.raises(ValueError, match="cash amount"):
            PortfolioEvent(account_id="a", event_type=EventType.CASH_TRANSFER,
                           currency="USD", occurred_at=T0, idempotency_key="k",
                           actor="x", surface="web")

    def test_negative_qty_rejected(self):
        with pytest.raises(ValueError, match="magnitude"):
            PortfolioEvent(account_id="a", event_type=EventType.BUY, symbol="NVDA",
                           currency="USD", qty=-5, price=10.0, occurred_at=T0,
                           idempotency_key="k", actor="x", surface="web")

    def test_naive_timestamp_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            PortfolioEvent(account_id="a", event_type=EventType.BUY, symbol="NVDA",
                           currency="USD", qty=5, price=10.0,
                           occurred_at=datetime(2026, 7, 1, 12, 0),
                           idempotency_key="k", actor="x", surface="web")

    def test_symbol_and_currency_normalised(self):
        e = PortfolioEvent(account_id="a", event_type=EventType.BUY, symbol=" nvda ",
                           currency="usd", qty=5, price=10.0, occurred_at=T0,
                           idempotency_key="k", actor="x", surface="web")
        assert e.symbol == "NVDA" and e.currency == "USD"

    def test_settlement_date_roundtrips(self, journal):
        a = _us_account(journal)
        ev, _ = journal.append_event(_event(a.id, settlement_date=date(2026, 7, 3),
                                            idempotency_key="s"))
        assert journal.get_event(ev.id).settlement_date == date(2026, 7, 3)

    def test_zero_qty_buy_rejected(self):
        with pytest.raises(ValueError, match="qty > 0"):
            PortfolioEvent(account_id="a", event_type=EventType.BUY, symbol="NVDA",
                           currency="USD", qty=0, price=10.0, occurred_at=T0,
                           idempotency_key="k", actor="x", surface="web")

    def test_opening_share_lot_zero_qty_rejected(self):
        with pytest.raises(ValueError, match="qty > 0"):
            PortfolioEvent(account_id="a", event_type=EventType.OPENING_BALANCE,
                           symbol="NVDA", currency="USD", qty=0, price=10.0,
                           occurred_at=T0, idempotency_key="k", actor="x", surface="web")

    def test_opening_balance_without_symbol_or_amount_rejected(self):
        with pytest.raises(ValueError, match="symbol\\+qty .* or an amount"):
            PortfolioEvent(account_id="a", event_type=EventType.OPENING_BALANCE,
                           currency="USD", qty=0, occurred_at=T0,
                           idempotency_key="k", actor="x", surface="web")


class TestProjectionEdges:
    def test_split_with_no_prior_lot_is_noop(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.SPLIT, symbol="AMD",
                                    qty=2.0, price=None, idempotency_key="s"))
        assert journal.holdings(a.id).holdings == []

    def test_sell_on_unknown_cost_lot_stays_unknown(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=None, idempotency_key="open"))
        journal.append_event(_event(a.id, event_type=EventType.SELL, qty=4, price=None,
                                    occurred_at=_dt(2), idempotency_key="s"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.qty == 6.0 and pos.avg_cost is None and pos.cost_basis_known is False

    def test_market_backfilled_from_later_event(self, journal):
        a = _us_account(journal)
        journal.append_event(_event(a.id, event_type=EventType.OPENING_BALANCE,
                                    qty=10, price=90.0, market=None, idempotency_key="o"))
        journal.append_event(_event(a.id, event_type=EventType.BUY, qty=5, price=100.0,
                                    market=MarketScope.US, occurred_at=_dt(2),
                                    idempotency_key="b"))
        (pos,) = journal.holdings(a.id).holdings
        assert pos.market is MarketScope.US

    def test_empty_account_projection(self, journal):
        a = _us_account(journal)
        h = journal.holdings(a.id)
        assert h.holdings == [] and h.cash == [] and h.as_of is None and h.n_events == 0

    def test_update_account_note_and_type(self, journal):
        a = _us_account(journal)
        b = journal.update_account(a.id, note="taxable", account_type=AccountType.MARGIN)
        assert b.note == "taxable" and b.account_type is AccountType.MARGIN
