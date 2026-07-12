"""Tests for swing_trader.execution (Loop.md §5.7, backlog 12).

Uses the real PaperBroker + Ledger (tmp_path SQLite) — offline, deterministic,
and doubles as a cross-module contract check.
"""

from datetime import datetime, timedelta, timezone

import pytest

from swing_trader.execution import ExecutionEngine, ExecutionReport, GuardrailError
from swing_trader.interfaces import Bar
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

NOW = datetime(2026, 7, 13, 15, 45, tzinfo=timezone.utc)  # 11:45 ET (EDT)


def bar(symbol="NVDA", o=99.0, h=101.0, lo=98.0, c=100.0, v=1_000_000):
    return Bar(symbol=symbol, ts=NOW, open=o, high=h, low=lo, close=c, volume=v)


def candidate(**kw) -> CandidateOrder:
    base = dict(
        symbol="NVDA", side=Side.BUY, qty=2, order_type=OrderType.BRACKET,
        limit=99.5, stop=91.5, tp=111.5, rationale="test entry",
        confidence=0.7, ref_px=100.0, status=CandidateStatus.APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def env(tmp_path):
    broker = PaperBroker(starting_cash=10_000.0)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'t.db'}")
    engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
    return broker, ledger, engine


def record(ledger: Ledger, c: CandidateOrder) -> CandidateOrder:
    ledger.record_candidate(c, Mode.PAPER)
    return c


class TestGuardrails:
    def test_live_mode_without_permission_raises(self, env):
        broker, ledger, _ = env
        engine = ExecutionEngine(broker, ledger, mode=Mode.LIVE,
                                 live_orders_allowed=False)
        with pytest.raises(GuardrailError, match="Loop.md"):
            engine.execute([], {}, NOW)

    def test_paper_mode_never_blocked(self, env):
        _, _, engine = env
        assert isinstance(engine.execute([], {}, NOW), ExecutionReport)


class TestReValidation:
    def test_non_actionable_statuses_skipped(self, env):
        _, ledger, engine = env
        for status in (CandidateStatus.PROPOSED, CandidateStatus.RISK_APPROVED,
                       CandidateStatus.REJECTED, CandidateStatus.EXPIRED):
            c = record(ledger, candidate(status=status))
            report = engine.execute([c], {"NVDA": 100.0}, NOW)
            assert report.placed == []
            assert "not actionable" in report.skipped[0][1]

    def test_expired_validity_window(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate(valid_until=NOW - timedelta(minutes=1)))
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        assert report.placed == []
        assert "validity window" in report.skipped[0][1]
        stored = ledger.get_candidates(mode=Mode.PAPER,
                                       status=CandidateStatus.EXPIRED)
        assert len(stored) == 1

    def test_price_drift_beyond_tolerance_skipped(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate())  # ref 100, tolerance 1.5%
        report = engine.execute([c], {"NVDA": 102.0}, NOW)
        assert "price ran away" in report.skipped[0][1]

    def test_price_within_tolerance_placed(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate())
        report = engine.execute([c], {"NVDA": 101.0}, NOW)
        assert len(report.placed) == 1

    def test_thesis_broken_when_last_below_stop(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate())
        report = engine.execute([c], {"NVDA": 91.0}, NOW)
        assert "thesis broken" in report.skipped[0][1]

    def test_buy_without_quote_skipped(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate())
        report = engine.execute([c], {}, NOW)
        assert "no fresh quote" in report.skipped[0][1]


class TestTranslation:
    def test_bracket_candidate_places_bracket_with_children(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        assert len(report.placed) == 1
        order = report.placed[0]
        assert order.order_type is OrderType.BRACKET
        assert order.tif is TimeInForce.GTC
        all_orders = broker.get_orders()
        children = [o for o in all_orders if o.parent_order_id == order.id]
        assert len(children) == 2  # protective stop + take-profit
        assert ledger.get_candidates(status=CandidateStatus.PLACED)

    def test_lmt_with_sl_upgraded_to_bracket(self, env):
        broker, _, engine = env
        c = candidate(order_type=OrderType.LMT, stop=None, sl=94.0, tp=None)
        self_ledger = engine.ledger
        self_ledger.record_candidate(c, Mode.PAPER)
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        order = report.placed[0]
        assert order.order_type is OrderType.BRACKET
        assert order.stop == pytest.approx(94.0)

    def test_unsupported_entry_type_skipped(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate(order_type=OrderType.MOC, limit=None,
                                     stop=None, sl=94.0, tp=None))
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        assert "unsupported" in report.skipped[0][1]

    def test_sell_exit_passthrough_without_quote(self, env):
        broker, ledger, engine = env
        # seed a long position: buy 2 @ open 99
        entry = record(ledger, candidate())
        engine.execute([entry], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0)})
        engine.sync_fills()
        # discretionary MOC exit, no quote provided — exits are never blocked
        exit_c = record(ledger, candidate(
            side=Side.SELL, order_type=OrderType.MOC, qty=2,
            limit=None, stop=None, tp=None, sl=None, tif=TimeInForce.DAY,
            status=CandidateStatus.EDITED,  # edited counts as human-approved
        ))
        report = engine.execute([exit_c], {}, NOW)
        assert len(report.placed) == 1
        assert report.placed[0].order_type is OrderType.MOC
        # protection was cleared so the exit could claim the shares
        active_sells = [o for o in broker.get_orders(active_only=True)
                        if o.side is Side.SELL and o.order_type is OrderType.STP]
        assert active_sells == []

    def test_failed_exit_restores_protective_stop(self, env):
        """If the exit is rejected after protection was cancelled, the stop
        must be re-placed — a position may never sit naked (Loop.md §4)."""
        broker, ledger, engine = env
        entry = record(ledger, candidate())
        engine.execute([entry], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0)})
        engine.sync_fills()
        # an exit that the broker will reject: sells more than held
        bad_exit = record(ledger, candidate(
            side=Side.SELL, order_type=OrderType.MOC, qty=50,
            limit=None, stop=None, tp=None, sl=None, tif=TimeInForce.DAY,
        ))
        report = engine.execute([bad_exit], {}, NOW)
        assert len(report.rejected) == 1
        stops = [o for o in broker.get_orders(active_only=True)
                 if o.side is Side.SELL and o.order_type is OrderType.STP]
        assert len(stops) == 1
        assert stops[0].stop == pytest.approx(91.5)
        assert stops[0].qty == pytest.approx(2)


class TestBrokerRejection:
    def test_rejection_recorded(self, env):
        _, ledger, engine = env
        c = record(ledger, candidate(qty=500))  # 500 * 99.5 >> 10k cash
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        assert report.placed == []
        assert len(report.rejected) == 1
        order, reason = report.rejected[0]
        assert order.status is OrderStatus.REJECTED
        assert reason
        cands = ledger.get_candidates(mode=Mode.PAPER)
        assert any("broker rejected" in cc.risk_note for cc in cands)


class TestSyncFills:
    def test_fills_land_in_ledger_with_risk(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        engine.execute([c], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0)})  # entry fills at open
        n = engine.sync_fills()
        assert n == 1
        trades = ledger.get_trades(Mode.PAPER, open_only=True)
        assert len(trades) == 1
        assert trades[0].risk_per_share == pytest.approx(99.0 - 91.5)

    def test_sync_is_idempotent(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        engine.execute([c], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0)})
        assert engine.sync_fills() == 1
        assert engine.sync_fills() == 0

    def test_full_roundtrip_entry_stop_out(self, env):
        """Entry fills, price collapses, protective stop fills -> closed trade."""
        broker, ledger, engine = env
        c = record(ledger, candidate())
        engine.execute([c], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0)})  # entry 2 @ 99
        engine.sync_fills()
        broker.step({"NVDA": bar(o=90.0, h=91.0, lo=89.0, c=90.5)})  # gap under stop
        engine.sync_fills()
        closed = ledger.get_trades(Mode.PAPER, closed_only=True)
        assert len(closed) == 1
        assert closed[0].pnl < 0  # stopped out at a loss
        assert closed[0].r_multiple is not None
