"""Tests for Phase 0.9 CSV import (Loop.md P0.9 backlog #4).

Preview validates + dedups without writing; commit appends only valid,
non-duplicate rows as append-only events; re-importing the same file is
idempotent; imports never touch the trading ledger (boundary #1).
"""

from __future__ import annotations

import pytest

from swing_trader.ledger import Ledger
from swing_trader.portfolio_csv import commit_csv, parse_csv
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.schemas import Mode

GOOD_CSV = """date,event_type,symbol,market,currency,qty,price,commission,note
2026-07-01,opening_balance,NVDA,US,USD,10,90,,seed
2026-07-02,buy,NVDA,US,USD,5,110,1,add
2026-07-03,dividend,,,USD,,,,
"""  # note: dividend row is INVALID here (no amount) — used to test errors


@pytest.fixture()
def journal(tmp_path):
    return PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}")


@pytest.fixture()
def account(journal):
    return journal.create_account(name="US", market_scope="US", base_currency="USD")


class TestParse:
    def test_missing_required_column(self, journal, account):
        pv = parse_csv("symbol,qty\nNVDA,10\n", account.id, journal)
        assert pv.header_error and "date" in pv.header_error
        assert not pv.committable

    def test_valid_and_invalid_rows(self, journal, account):
        pv = parse_csv(GOOD_CSV, account.id, journal)
        assert pv.n_valid == 2  # opening + buy
        assert pv.n_invalid == 1  # dividend without amount
        bad = [r for r in pv.rows if r.errors][0]
        assert bad.line == 3

    def test_dedup_against_journal(self, journal, account):
        csv1 = "date,event_type,symbol,market,currency,qty,price\n2026-07-01,buy,NVDA,US,USD,10,100\n"
        commit_csv(journal, account.id, csv1, actor="g", surface="web")
        pv = parse_csv(csv1, account.id, journal)  # same content again
        assert pv.rows[0].duplicate is True
        assert pv.n_valid == 0 and pv.n_duplicate == 1

    def test_dedup_within_file(self, journal, account):
        csv = ("date,event_type,symbol,market,currency,qty,price\n"
               "2026-07-01,buy,NVDA,US,USD,10,100\n"
               "2026-07-01,buy,NVDA,US,USD,10,100\n")
        pv = parse_csv(csv, account.id, journal)
        assert [r.duplicate for r in pv.rows] == [False, True]

    def test_external_id_dedup(self, journal, account):
        csv = ("date,event_type,symbol,market,currency,qty,price,external_id\n"
               "2026-07-01,buy,NVDA,US,USD,10,100,EXE-9\n")
        commit_csv(journal, account.id, csv, actor="g", surface="web")
        # different content but same external id → duplicate
        csv2 = ("date,event_type,symbol,market,currency,qty,price,external_id\n"
                "2026-07-05,buy,NVDA,US,USD,99,999,EXE-9\n")
        pv = parse_csv(csv2, account.id, journal)
        assert pv.rows[0].duplicate is True


class TestCommit:
    def test_commit_appends_events_and_derives_holdings(self, journal, account):
        res = commit_csv(journal, account.id, GOOD_CSV, actor="gongqing", surface="web")
        assert res.n_committed == 2 and res.n_skipped == 1
        (pos,) = journal.holdings(account.id).holdings
        assert pos.symbol == "NVDA" and pos.qty == 15.0

    def test_reimport_is_idempotent(self, journal, account):
        commit_csv(journal, account.id, GOOD_CSV, actor="g", surface="web")
        res2 = commit_csv(journal, account.id, GOOD_CSV, actor="g", surface="web")
        assert res2.n_committed == 0 and res2.n_duplicate == 2
        (pos,) = journal.holdings(account.id).holdings
        assert pos.qty == 15.0  # not doubled

    def test_commit_unknown_account_raises(self, journal):
        with pytest.raises(ValueError, match="unknown account"):
            commit_csv(journal, "ghost", GOOD_CSV, actor="g", surface="web")

    def test_import_creates_no_ledger_fills(self, tmp_path):
        url = f"sqlite:///{tmp_path/'shared.db'}"
        ledger = Ledger(url=url)
        journal = PortfolioJournal(url=url)
        acct = journal.create_account(name="US", market_scope="US", base_currency="USD")
        commit_csv(journal, acct.id, GOOD_CSV, actor="g", surface="web")
        assert ledger.get_fills(Mode.PAPER) == [] and ledger.get_trades(Mode.PAPER) == []
        assert ledger.get_candidates() == []

    def test_events_tagged_source_csv(self, journal, account):
        commit_csv(journal, account.id, GOOD_CSV, actor="g", surface="web")
        assert all(e.source.value == "csv" for e in journal.get_events(account.id))
