"""Tests for the paper-trading readiness report (Loop.md Phase 0.95 exit)."""

from __future__ import annotations

from datetime import datetime, timezone

from swing_trader.ledger import Ledger
from swing_trader.readiness import PaperReadiness, assess_paper_readiness
from swing_trader.schemas import CandidateOrder, Fill, Mode, OrderType, Role, Side


def _cand(day, symbol="NVDA"):
    return CandidateOrder(
        symbol=symbol, side=Side.BUY, qty=1, order_type=OrderType.LMT, limit=100.0,
        sl=95.0, rationale="t", confidence=0.8, pool=Role.ROTATION,
        ts=datetime(2026, 3, day, 15, 0, tzinfo=timezone.utc))


def _fill(day, oid, side=Side.BUY):
    return Fill(id=f"f{day}-{oid}", order_id=oid, symbol="NVDA", side=side, qty=1,
                px=100.0, commission=1.0, mode=Mode.PAPER,
                ts=datetime(2026, 3, day, 15, 30, tzinfo=timezone.utc))


class TestPaperReadinessLogic:
    def test_ready_needs_days_and_a_closed_trade(self):
        assert PaperReadiness(20, 20, 5, 3, "a", "b").ready is True
        assert PaperReadiness(20, 20, 5, 0, "a", "b").ready is False  # no round-trip
        assert PaperReadiness(20, 19, 5, 3, "a", "b").ready is False  # 1 day short

    def test_days_remaining(self):
        assert PaperReadiness(20, 12, 0, 0, None, None).days_remaining == 8
        assert PaperReadiness(20, 25, 0, 0, None, None).days_remaining == 0

    def test_summary_mentions_state(self):
        assert "not yet" in PaperReadiness(20, 3, 1, 0, "x", "y").summary()
        assert "READY" in PaperReadiness(20, 20, 5, 2, "x", "y").summary()


class TestAssessOverLedger:
    def test_counts_distinct_session_and_fill_days(self, tmp_path):
        led = Ledger(url=f"sqlite:///{tmp_path/'r.db'}")
        for day in (2, 2, 3, 4):  # 3 distinct session dates (day 2 twice)
            led.record_candidate(_cand(day), Mode.PAPER)
        for day, oid in ((2, "o1"), (3, "o2")):
            led.record_fill(_fill(day, oid))
        r = assess_paper_readiness(led, min_days=20)
        assert r.session_days == 3
        assert r.fill_days == 2
        assert r.first_day == "2026-03-02"
        assert r.last_day == "2026-03-04"
        assert r.days_remaining == 17

    def test_empty_ledger(self, tmp_path):
        led = Ledger(url=f"sqlite:///{tmp_path/'e.db'}")
        r = assess_paper_readiness(led)
        assert r.session_days == 0 and r.fill_days == 0
        assert r.first_day is None and r.ready is False

    def test_mode_scoped(self, tmp_path):
        led = Ledger(url=f"sqlite:///{tmp_path/'m.db'}")
        led.record_candidate(_cand(2), Mode.PAPER)
        led.record_candidate(_cand(3), Mode.LIVE)
        assert assess_paper_readiness(led, mode=Mode.PAPER).session_days == 1
        assert assess_paper_readiness(led, mode=Mode.LIVE).session_days == 1

    def test_tz_bucketing(self, tmp_path):
        led = Ledger(url=f"sqlite:///{tmp_path/'tz.db'}")
        # 02:00 UTC on Mar 3 is 21:00 ET on Mar 2 → counts as the Mar-2 session.
        led.record_candidate(CandidateOrder(
            symbol="NVDA", side=Side.BUY, qty=1, order_type=OrderType.LMT, limit=100.0,
            sl=95.0, rationale="t", confidence=0.8, pool=Role.ROTATION,
            ts=datetime(2026, 3, 3, 2, 0, tzinfo=timezone.utc)), Mode.PAPER)
        r = assess_paper_readiness(led, market_tz="America/New_York")
        assert r.first_day == "2026-03-02"


class TestReadinessEndpoint:
    def test_endpoint_reports_progress(self, tmp_path):
        from fastapi.testclient import TestClient

        from swing_trader.api import FinanceRuntime, create_app

        led = Ledger(url=f"sqlite:///{tmp_path/'api.db'}")
        for day in (2, 3, 4):
            led.record_candidate(_cand(day), Mode.PAPER)
        client = TestClient(create_app(FinanceRuntime(ledger=led, mode=Mode.PAPER)))
        body = client.get("/v1/readiness").json()
        assert body["session_days"] == 3 and body["min_days"] == 20
        assert body["days_remaining"] == 17 and body["ready"] is False
        assert "3/20" in body["summary"]
