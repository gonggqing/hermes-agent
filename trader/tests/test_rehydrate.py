"""Tests for swing_trader.rehydrate (Loop.md Phase 0.5, backlog item 1).

Strategy: run a real session (PaperBroker + Ledger + ExecutionEngine),
restart into a FRESH broker rehydrated from the ledger, and assert the two
worlds match — including the §4 invariant that protective stops come back
up and still fire.
"""

from datetime import datetime, timezone

import pytest

from swing_trader.execution import ExecutionEngine
from swing_trader.interfaces import Bar
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.rehydrate import rehydrate_from_ledger
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderStatus,
    OrderType,
    Side,
)

NOW = datetime(2026, 7, 13, 15, 45, tzinfo=timezone.utc)


def bar(symbol="NVDA", o=99.0, h=101.0, lo=98.0, c=100.0, v=1_000_000):
    return Bar(symbol=symbol, ts=NOW, open=o, high=h, low=lo, close=c, volume=v)


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA", side=Side.BUY, qty=2, order_type=OrderType.BRACKET,
        limit=99.5, stop=91.5, tp=111.5, rationale="test", confidence=0.7,
        ref_px=100.0, status=CandidateStatus.APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def session(tmp_path):
    """A live session with one filled bracket entry + one resting LMT."""
    ledger = Ledger(url=f"sqlite:///{tmp_path/'r.db'}")
    broker = PaperBroker(starting_cash=10_000.0)
    engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)

    filled = candidate()
    ledger.record_candidate(filled, Mode.PAPER)
    engine.execute([filled], {"NVDA": 100.0}, NOW)
    broker.step({"NVDA": bar(o=99.0)})  # entry fills; children activate
    engine.sync_fills()

    resting = candidate(symbol="MU", limit=80.0, stop=74.0, tp=90.0, qty=3,
                        ref_px=80.2)
    ledger.record_candidate(resting, Mode.PAPER)
    engine.execute([resting], {"MU": 80.2}, NOW)  # never fills
    engine.sync_fills()
    for order in broker.get_orders():
        ledger.update_order(order)
    return ledger, broker


def restart(ledger) -> tuple[PaperBroker, "rehydrate_from_ledger"]:
    fresh = PaperBroker(starting_cash=10_000.0)
    report = rehydrate_from_ledger(fresh, ledger, Mode.PAPER)
    return fresh, report


class TestRoundTrip:
    def test_cash_and_positions_match(self, session):
        ledger, original = session
        fresh, report = restart(ledger)
        assert report.performed
        assert fresh.get_account().cash == pytest.approx(original.get_account().cash)
        orig_pos = {p.symbol: p for p in original.get_positions()}
        new_pos = {p.symbol: p for p in fresh.get_positions()}
        assert set(new_pos) == set(orig_pos) == {"NVDA"}
        assert new_pos["NVDA"].qty == orig_pos["NVDA"].qty
        assert new_pos["NVDA"].avg_px == pytest.approx(orig_pos["NVDA"].avg_px)

    def test_resting_orders_restored(self, session):
        ledger, original = session
        fresh, _ = restart(ledger)
        orig_active = {o.id: o for o in original.get_orders(active_only=True)}
        new_active = {o.id: o for o in fresh.get_orders(active_only=True)}
        assert set(new_active) == set(orig_active)
        for oid, order in orig_active.items():
            assert new_active[oid].status is order.status
            assert new_active[oid].qty == order.qty
            assert new_active[oid].oca_group == order.oca_group

    def test_protective_stop_still_fires(self, session):
        """§4: after a restart the position must still be protected."""
        ledger, _ = session
        fresh, _ = restart(ledger)
        stops = [o for o in fresh.get_orders(active_only=True)
                 if o.symbol == "NVDA" and o.order_type is OrderType.STP
                 and o.side is Side.SELL]
        assert len(stops) == 1
        fills = fresh.step({"NVDA": bar(o=90.0, h=91.0, lo=89.0, c=90.5)})
        assert any(f.side is Side.SELL for f in fills)  # stop executed
        assert fresh.get_positions() == []  # flat again

    def test_oca_sibling_cancelled_after_restart(self, session):
        ledger, _ = session
        fresh, _ = restart(ledger)
        fresh.step({"NVDA": bar(o=90.0, h=91.0, lo=89.0, c=90.5)})  # stop fills
        tps = [o for o in fresh.get_orders()
               if o.symbol == "NVDA" and o.tp is None
               and o.order_type is OrderType.LMT and o.side is Side.SELL]
        # the tp leg must be cancelled, not resting
        assert all(o.status is OrderStatus.CANCELLED for o in tps)

    def test_cash_reservation_still_enforced(self, session):
        """The restored resting MU BUY keeps its cash reserved."""
        ledger, original = session
        fresh, _ = restart(ledger)
        # try to spend almost all remaining cash: must be rejected because
        # the MU order still reserves 3*80 + commission
        cash = fresh.get_account().cash
        from swing_trader.schemas import Order
        result = fresh.place_order(Order(
            mode=Mode.PAPER, symbol="AMD", side=Side.BUY,
            qty=int(cash // 100), order_type=OrderType.LMT, limit=100.0,
        ))
        assert not result.accepted
        assert "reserv" in result.reason.lower() or "cash" in result.reason.lower()

    def test_sync_fills_records_nothing_new(self, session):
        ledger, _ = session
        n_before = len(ledger.get_fills(Mode.PAPER))
        fresh, report = restart(ledger)
        engine = ExecutionEngine(fresh, ledger, mode=Mode.PAPER)
        engine.seed_synced_fills(report.fill_ids)
        assert engine.sync_fills() == 0
        assert len(ledger.get_fills(Mode.PAPER)) == n_before

    def test_r_multiple_survives_restart(self, session):
        """Pre-restart resting entry fills post-restart with risk recorded."""
        ledger, _ = session
        fresh, report = restart(ledger)
        engine = ExecutionEngine(fresh, ledger, mode=Mode.PAPER)
        engine.seed_synced_fills(report.fill_ids)
        engine.seed_protective_stops(fresh.get_orders(active_only=True))
        fresh.step({"MU": bar(symbol="MU", o=79.5, h=80.5, lo=79.0, c=80.0)})
        engine.sync_fills()
        open_trades = [t for t in ledger.get_trades(Mode.PAPER, open_only=True)
                       if t.symbol == "MU"]
        assert len(open_trades) == 1
        assert open_trades[0].risk_per_share == pytest.approx(79.5 - 74.0)


class TestEdgeCases:
    def test_empty_ledger_noop(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'e.db'}")
        broker = PaperBroker(starting_cash=5_000.0)
        report = rehydrate_from_ledger(broker, ledger, Mode.PAPER)
        assert not report.performed
        assert broker.get_account().cash == 5_000.0

    def test_starting_cash_mismatch_warns(self, session, tmp_path):
        from datetime import timedelta

        ledger, original = session
        # anchor snapshot AFTER the (future-dated) test fills so the
        # rehydrator's "no fills after snapshot" precondition holds
        snap = original.get_account().model_copy(
            update={"ts": NOW + timedelta(hours=1)}
        )
        ledger.record_snapshot(snap)
        fresh = PaperBroker(starting_cash=999.0)  # wrong base
        report = rehydrate_from_ledger(fresh, ledger, Mode.PAPER)
        assert any("starting-cash" in w for w in report.warnings)

    def test_restore_refuses_dirty_broker(self, session):
        ledger, original = session
        with pytest.raises(RuntimeError, match="fresh"):
            original.restore_state(1000.0, [], [])

    def test_report_summary_readable(self, session):
        ledger, _ = session
        _, report = restart(ledger)
        text = report.summary()
        assert "rehydrated" in text and "position" in text
