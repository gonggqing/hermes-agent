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
        symbol="NVDA",
        side=Side.BUY,
        qty=2,
        order_type=OrderType.BRACKET,
        limit=99.5,
        stop=91.5,
        tp=111.5,
        rationale="test entry",
        confidence=0.7,
        ref_px=100.0,
        status=CandidateStatus.APPROVED,
    )
    base.update(kw)
    return CandidateOrder(**base)


@pytest.fixture()
def env(tmp_path):
    broker = PaperBroker(starting_cash=10_000.0)
    ledger = Ledger(url=f"sqlite:///{tmp_path / 't.db'}")
    engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
    return broker, ledger, engine


def record(ledger: Ledger, c: CandidateOrder) -> CandidateOrder:
    ledger.record_candidate(c, Mode.PAPER)
    return c


class TestGuardrails:
    def test_live_mode_without_permission_raises(self, env):
        broker, ledger, _ = env
        engine = ExecutionEngine(broker, ledger, mode=Mode.LIVE, live_orders_allowed=False)
        with pytest.raises(GuardrailError, match="Loop.md"):
            engine.execute([], {}, NOW)

    def test_paper_mode_never_blocked(self, env):
        _, _, engine = env
        assert isinstance(engine.execute([], {}, NOW), ExecutionReport)


class TestReValidation:
    def test_non_actionable_statuses_skipped(self, env):
        _, ledger, engine = env
        for status in (
            CandidateStatus.PROPOSED,
            CandidateStatus.RISK_APPROVED,
            CandidateStatus.REJECTED,
            CandidateStatus.EXPIRED,
        ):
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
        stored = ledger.get_candidates(mode=Mode.PAPER, status=CandidateStatus.EXPIRED)
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


class _StubBroker:
    """Minimal broker exposing just the cancel-all surface with independent,
    non-cascading orders (unlike PaperBroker's bracket cascade)."""

    def __init__(self, orders):
        self._orders = {o.id: o for o in orders}

    def get_orders(self, active_only=False):
        return list(self._orders.values())

    def cancel_order(self, order_id):
        o = self._orders.get(order_id)
        if o is None or o.status is OrderStatus.FILLED:
            return False
        self._orders[order_id] = o.model_copy(update={"status": OrderStatus.CANCELLED})
        return True


class TestCancelAll:
    def _order(self, oid, side, ot, status=OrderStatus.SUBMITTED):
        from swing_trader.schemas import Order

        return Order(
            id=oid,
            mode=Mode.PAPER,
            symbol="NVDA",
            side=side,
            qty=2,
            order_type=ot,
            limit=100.0,
            stop=90.0,
            status=status,
        )

    def test_cancels_active_bracket(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        engine.execute([c], {"NVDA": 101.0}, NOW)
        assert broker.get_orders(active_only=True)
        cancelled = engine.cancel_all_orders()
        assert len(cancelled) >= 1
        assert broker.get_orders(active_only=True) == []

    def test_include_protection_false_keeps_stops(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'c.db'}")
        entry = self._order("e1", Side.BUY, OrderType.LMT)
        stop = self._order("s1", Side.SELL, OrderType.STP)
        broker = _StubBroker([entry, stop])
        engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
        cancelled = engine.cancel_all_orders(include_protection=False)
        assert [o.id for o in cancelled] == ["e1"]  # entry only
        remaining = {o.id: o.status for o in broker.get_orders()}
        assert remaining["s1"] is OrderStatus.SUBMITTED  # protective stop kept
        assert remaining["e1"] is OrderStatus.CANCELLED

    def test_include_protection_true_cancels_everything(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'c.db'}")
        entry = self._order("e1", Side.BUY, OrderType.LMT)
        stop = self._order("s1", Side.SELL, OrderType.STP)
        broker = _StubBroker([entry, stop])
        engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
        cancelled = engine.cancel_all_orders(include_protection=True)
        assert {o.id for o in cancelled} == {"e1", "s1"}


class TestTranslation:
    def test_bracket_candidate_places_bracket_with_children(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        report = engine.execute([c], {"NVDA": 100.0}, NOW)
        assert len(report.placed) == 1
        order = report.placed[0]
        assert order.order_type is OrderType.BRACKET
        assert order.tif is TimeInForce.DAY  # entry parent is DAY (Loop.md §5.7)
        all_orders = broker.get_orders()
        children = [o for o in all_orders if o.parent_order_id == order.id]
        assert len(children) == 2  # protective stop + take-profit
        assert all(o.tif is TimeInForce.GTC for o in children)  # protection is GTC
        assert ledger.get_candidates(status=CandidateStatus.PLACED)
        assert order.id == f"candidate-{c.id}"
        assert order.broker_ref == f"candidate:{c.id}"
        audit = ledger.get_audit(candidate_id=c.id)
        assert audit[-1].action == "execute"
        assert audit[-1].new_status == CandidateStatus.PLACED.value

    def test_repeat_execute_recovers_existing_order_without_duplicate(self, env):
        broker, ledger, engine = env
        c = record(ledger, candidate())
        first = engine.execute([c], {"NVDA": 100.0}, NOW)
        n_orders = len(broker.get_orders())

        second = engine.execute([c], {"NVDA": 100.0}, NOW + timedelta(minutes=5))

        assert len(first.placed) == 1
        assert second.placed == []
        assert len(second.recovered) == 1
        assert len(broker.get_orders()) == n_orders
        execute_audit = [
            row
            for row in ledger.get_audit(candidate_id=c.id)
            if row.idempotency_key == f"execute:{c.id}"
        ]
        assert len(execute_audit) == 1

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
        c = record(
            ledger, candidate(order_type=OrderType.MOC, limit=None, stop=None, sl=94.0, tp=None)
        )
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
        exit_c = record(
            ledger,
            candidate(
                side=Side.SELL,
                order_type=OrderType.MOC,
                qty=2,
                limit=None,
                stop=None,
                tp=None,
                sl=None,
                tif=TimeInForce.DAY,
                status=CandidateStatus.EDITED,  # edited counts as human-approved
            ),
        )
        report = engine.execute([exit_c], {}, NOW)
        assert len(report.placed) == 1
        assert report.placed[0].order_type is OrderType.MOC
        # protection was cleared so the exit could claim the shares
        active_sells = [
            o
            for o in broker.get_orders(active_only=True)
            if o.side is Side.SELL and o.order_type is OrderType.STP
        ]
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
        bad_exit = record(
            ledger,
            candidate(
                side=Side.SELL,
                order_type=OrderType.MOC,
                qty=50,
                limit=None,
                stop=None,
                tp=None,
                sl=None,
                tif=TimeInForce.DAY,
            ),
        )
        report = engine.execute([bad_exit], {}, NOW)
        assert len(report.rejected) == 1
        stops = [
            o
            for o in broker.get_orders(active_only=True)
            if o.side is Side.SELL and o.order_type is OrderType.STP
        ]
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


class TestDayEntryLifecycle:
    """Loop.md §5.7 ordinary-order lifecycle: a DAY limit-entry parent with GTC
    protective children — an unfilled entry is cancelled at the close and a
    partial fill keeps GTC protection sized to the filled quantity."""

    def test_unfilled_day_entry_expires_at_close_and_voids_protection(self, env):
        broker, ledger, engine = env
        parent = engine.execute([record(ledger, candidate(qty=2))], {"NVDA": 100.0}, NOW).placed[0]
        assert parent.tif is TimeInForce.DAY

        # a session where price never trades down to the 99.5 entry limit
        broker.step({"NVDA": bar(o=100.0, h=101.0, lo=99.8, c=100.5)})
        broker.end_of_day()

        stored = {o.id: o for o in broker.get_orders()}
        assert stored[parent.id].status is OrderStatus.EXPIRED
        children = [o for o in stored.values() if o.parent_order_id == parent.id]
        assert children and all(o.status is OrderStatus.CANCELLED for o in children)
        assert broker.get_positions() == []  # unfilled -> no naked position

    def test_partial_fill_keeps_gtc_protection_sized_to_fill(self, env):
        broker, ledger, engine = env
        parent = engine.execute([record(ledger, candidate(qty=2))], {"NVDA": 100.0}, NOW).placed[0]

        # price reaches the 99.5 limit but only one share of liquidity this bar
        broker.step({"NVDA": bar(o=100.0, h=101.0, lo=99.0, c=99.5, v=1)})
        stored = {o.id: o for o in broker.get_orders()}
        assert stored[parent.id].status is OrderStatus.PARTIALLY_FILLED
        assert stored[parent.id].filled_qty == 1

        broker.end_of_day()  # cancel the unfilled entry remainder
        assert (
            next(o for o in broker.get_orders() if o.id == parent.id).status is OrderStatus.EXPIRED
        )

        # the 1-share position keeps a resting GTC protective stop sized to 1
        positions = broker.get_positions()
        assert len(positions) == 1 and positions[0].qty == 1
        stops = [
            o
            for o in broker.get_orders(active_only=True)
            if o.side is Side.SELL and o.order_type is OrderType.STP
        ]
        assert len(stops) == 1
        assert stops[0].tif is TimeInForce.GTC and stops[0].qty == 1


class TestHKTickGuard:
    """Loop.md §5.7 / rollout doc: a human-approved SEHK order is refused
    fail-closed if any price is off the exchange tick grid — never silently
    rounded after approval."""

    def test_offgrid_hk_entry_is_refused_not_rounded(self, env):
        _, ledger, engine = env
        c = record(
            ledger, candidate(symbol="0700.HK", limit=20.03, stop=18.00, tp=25.50, ref_px=20.0)
        )
        report = engine.execute([c], {"0700.HK": 20.0}, NOW)
        assert report.placed == []
        assert "SEHK tick grid" in report.skipped[0][1]

    def test_ongrid_hk_entry_passes_tick_guard(self, tmp_path):
        broker = PaperBroker(starting_cash_by_currency={"USD": 10_000.0, "HKD": 16_000.0})
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'hk.db'}")
        engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
        c = record(
            ledger, candidate(symbol="0700.HK", limit=20.05, stop=18.00, tp=25.50, ref_px=20.05)
        )
        # 2026-07-15 03:30 UTC = 11:30 HKT (morning continuous) — in-hours
        now_hk = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)
        report = engine.execute([c], {"0700.HK": 20.05}, now_hk)
        assert len(report.placed) == 1
        parent = report.placed[0]
        assert parent.currency == "HKD"
        assert parent.tif is TimeInForce.DAY

        fills = broker.step(
            {
                "0700.HK": Bar(
                    symbol="0700.HK",
                    ts=now_hk,
                    open=20.00,
                    high=20.50,
                    low=19.90,
                    close=20.25,
                    volume=1_000_000,
                )
            }
        )
        assert len(fills) == 1
        assert fills[0].currency == "HKD"
        assert broker.get_account().cash_by_currency["HKD"] < 16_000.0
        assert broker.get_positions()[0].currency == "HKD"

        protection = [
            order
            for order in broker.get_orders(active_only=True)
            if order.parent_order_id == parent.id and order.side is Side.SELL
        ]
        assert {order.order_type for order in protection} == {
            OrderType.STP,
            OrderType.LMT,
        }
        assert all(order.currency == "HKD" for order in protection)
        assert all(order.tif is TimeInForce.GTC for order in protection)
        assert all(order.qty == c.qty for order in protection)

        assert engine.sync_fills() == 1
        assert len(ledger.get_fills(mode=Mode.PAPER)) == 1

    def test_hk_entry_refused_during_lunch_break(self, env):
        _, ledger, engine = env
        c = record(
            ledger, candidate(symbol="0700.HK", limit=20.05, stop=18.00, tp=25.50, ref_px=20.05)
        )
        # 04:30 UTC = 12:30 HKT — SEHK lunch break
        lunch = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)
        report = engine.execute([c], {"0700.HK": 20.05}, lunch)
        assert report.placed == []
        assert "market closed" in report.skipped[0][1]


class TestHKExitDuringLunch:
    """A human-approved HK exit must NOT be dropped during the lunch break —
    only NEW ENTRIES are gated on trading hours (the exit rests until the
    session resumes; blocking it would discard the exit intent)."""

    def test_sell_exit_not_dropped_during_lunch(self, tmp_path):
        broker = PaperBroker(starting_cash_by_currency={"USD": 10_000.0, "HKD": 16_000.0})
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'hk.db'}")
        engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
        # open a position in-hours (11:30 HKT), then fill it
        in_hours = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)
        buy = record(
            ledger, candidate(symbol="0700.HK", limit=20.05, stop=18.00, tp=25.50, ref_px=20.05)
        )
        engine.execute([buy], {"0700.HK": 20.05}, in_hours)
        broker.step({"0700.HK": bar(symbol="0700.HK", o=20.05, h=21.0, lo=20.0, c=20.5)})
        # a discretionary SELL exit revalidated during the 12:30 HKT lunch break
        lunch = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)
        sell = record(
            ledger,
            candidate(
                symbol="0700.HK",
                side=Side.SELL,
                order_type=OrderType.LMT,
                limit=21.00,
                stop=None,
                tp=None,
                ref_px=21.00,
            ),
        )
        report = engine.execute([sell], {"0700.HK": 21.00}, lunch)
        assert not any("market closed" in reason for _, reason in report.skipped)


class TestExistingOrderBrokerAuthoritative:
    """Broker state wins over a stale ledger row in _existing_order (the ledger
    lags between end_of_day and the next sync)."""

    def test_broker_expired_beats_stale_ledger_submitted(self, tmp_path):
        from swing_trader.schemas import Order

        ledger = Ledger(url=f"sqlite:///{tmp_path / 'x.db'}")
        cid = "c-1"
        oid = f"candidate-{cid}"
        # ledger still shows SUBMITTED (stale); broker shows EXPIRED (truth)
        ledger.record_order(
            Order(
                id=oid,
                mode=Mode.PAPER,
                symbol="NVDA",
                side=Side.BUY,
                qty=2,
                order_type=OrderType.LMT,
                limit=100.0,
                status=OrderStatus.SUBMITTED,
                broker_ref=f"candidate:{cid}",
            )
        )
        expired = Order(
            id=oid,
            mode=Mode.PAPER,
            symbol="NVDA",
            side=Side.BUY,
            qty=2,
            order_type=OrderType.LMT,
            limit=100.0,
            status=OrderStatus.EXPIRED,
            broker_ref=f"candidate:{cid}",
        )
        engine = ExecutionEngine(_StubBroker([expired]), ledger, mode=Mode.PAPER)
        assert engine._existing_order(candidate(id=cid)) is None


class TestReprotectAfterUnfilledExit:
    """A discretionary exit that strips protection then rests unfilled must not
    leave the position naked — the close-time safety net re-arms the stop."""

    def test_stop_rearmed_when_limit_exit_expires_unfilled(self, tmp_path):
        broker = PaperBroker(starting_cash=10_000.0)
        ledger = Ledger(url=f"sqlite:///{tmp_path / 'rp.db'}")
        engine = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
        # 1) enter + fill a bracket so a protective STP is resting
        buy = record(ledger, candidate())
        engine.execute([buy], {"NVDA": 100.0}, NOW)
        broker.step({"NVDA": bar(o=99.0, h=101.0, lo=98.0, c=100.0)})
        assert any(
            o.order_type is OrderType.STP and o.side is Side.SELL
            for o in broker.get_orders(active_only=True)
        )
        # 2) a discretionary LMT exit far above market strips protection, rests
        sell = record(
            ledger,
            candidate(
                id="exit-1",
                side=Side.SELL,
                order_type=OrderType.LMT,
                limit=120.0,
                stop=None,
                tp=None,
                tif=TimeInForce.DAY,
                qty=2,
            ),
        )
        engine.execute([sell], {"NVDA": 100.0}, NOW)
        assert not any(
            o.order_type is OrderType.STP for o in broker.get_orders(active_only=True)
        )  # naked
        # 3) close: exit doesn't reach 120, DAY-expires; safety net re-arms stop
        broker.step({"NVDA": bar(o=100.0, h=105.0, lo=99.0, c=104.0)})
        broker.end_of_day()
        restored = engine.reprotect_positions(NOW)
        assert len(restored) == 1
        stops = [
            o
            for o in broker.get_orders(active_only=True)
            if o.order_type is OrderType.STP and o.side is Side.SELL
        ]
        assert len(stops) == 1 and stops[0].stop == 91.5 and stops[0].qty == 2
