"""Tests for swing_trader.paper_broker (Loop.md §5.1, §9).

Order lifecycle place -> partial -> fill -> cancel -> reject, deterministic
fill/slippage math, bracket/OCA behavior, DAY expiry, cash-account
constraints, and account math. Fully deterministic; no network.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from swing_trader.interfaces import Bar
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import (
    BreakerState,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

T0 = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)

SLIP = 5.0 / 10_000.0  # matches default slippage_bps=5.0


def bar(
    symbol: str = "NVDA",
    open: float = 100.0,
    high: float = 105.0,
    low: float = 95.0,
    close: float = 102.0,
    volume: float = 1_000_000.0,
    ts: datetime = T0,
) -> Bar:
    return Bar(symbol=symbol, ts=ts, open=open, high=high, low=low, close=close, volume=volume)


def make_order(**kw) -> Order:
    base = dict(
        mode=Mode.PAPER,
        symbol="NVDA",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.LMT,
        limit=100.0,
    )
    base.update(kw)
    return Order(**base)


def broker(**kw) -> PaperBroker:
    return PaperBroker(**kw)


def funded_broker(qty: float = 10, px: float = 100.0, **kw) -> PaperBroker:
    """Broker holding a long position of ``qty`` shares filled at ``px``."""
    b = broker(**kw)
    result = b.place_order(make_order(qty=qty, limit=px))
    assert result.accepted
    fills = b.step({"NVDA": bar(open=px, low=px - 1, high=px + 1, close=px)})
    assert len(fills) == 1 and fills[0].qty == qty
    return b


# --------------------------------------------------------------------- placement


class TestPlaceOrder:
    def test_buy_lmt_accepted_submitted(self):
        b = broker()
        result = b.place_order(make_order())
        assert result.accepted
        assert result.order.status is OrderStatus.SUBMITTED
        assert result.order.mode is Mode.PAPER
        active = b.get_orders(active_only=True)
        assert [o.id for o in active] == [result.order.id]

    def test_buy_rejected_when_reservation_exceeds_cash(self):
        b = broker(starting_cash=2000.0)
        result = b.place_order(make_order(qty=20, limit=100.0))  # 2001 > 2000
        assert not result.accepted
        assert result.order.status is OrderStatus.REJECTED
        assert "insufficient cash" in result.reason

    def test_reservations_accumulate_across_resting_buys(self):
        b = broker(starting_cash=2000.0)
        assert b.place_order(make_order(qty=15, limit=100.0)).accepted  # reserves 1501
        second = b.place_order(make_order(qty=5, limit=100.0))  # +501 -> 2002 > 2000
        assert not second.accepted
        assert "insufficient cash" in second.reason

    def test_cancel_releases_reservation(self):
        b = broker(starting_cash=2000.0)
        first = b.place_order(make_order(qty=15, limit=100.0))
        assert b.cancel_order(first.order.id) is True
        assert b.place_order(make_order(qty=15, limit=100.0)).accepted

    def test_sell_without_position_rejected_no_short(self):
        b = broker()
        result = b.place_order(make_order(side=Side.SELL))
        assert not result.accepted
        assert "shorting not allowed" in result.reason

    def test_sell_more_than_held_rejected(self):
        b = funded_broker(qty=10)
        result = b.place_order(make_order(side=Side.SELL, qty=11, limit=110.0))
        assert not result.accepted
        assert "insufficient unreserved position" in result.reason

    def test_sell_qty_reserved_by_resting_sell(self):
        b = funded_broker(qty=10)
        assert b.place_order(make_order(side=Side.SELL, qty=10, limit=150.0)).accepted
        second = b.place_order(make_order(side=Side.SELL, qty=1, limit=150.0))
        assert not second.accepted
        assert "insufficient unreserved position" in second.reason

    def test_sell_bracket_rejected_as_short(self):
        b = funded_broker(qty=10)
        order = make_order(
            side=Side.SELL, order_type=OrderType.BRACKET, limit=100.0, stop=105.0, tp=90.0
        )
        result = b.place_order(order)
        assert not result.accepted
        assert "short" in result.reason

    def test_moc_buy_without_mark_rejected(self):
        b = broker()
        result = b.place_order(make_order(order_type=OrderType.MOC, limit=None))
        assert not result.accepted
        assert "no reference price" in result.reason

    def test_placed_order_is_copied_not_aliased(self):
        b = broker()
        order = make_order()
        result = b.place_order(order)
        order.qty = 99999  # caller mutates its own object afterwards
        stored = b.get_orders()[0]
        assert stored.qty == 10
        assert result.order.qty == 10


# --------------------------------------------------------------------- LMT fills


class TestLimitFills:
    def test_buy_lmt_fills_at_limit_when_low_touches(self):
        b = broker()
        oid = b.place_order(make_order(limit=98.0)).order.id
        fills = b.step({"NVDA": bar(open=100, high=105, low=97, close=102)})
        assert len(fills) == 1
        f = fills[0]
        assert f.px == 98.0
        assert f.qty == 10
        assert f.order_id == oid
        assert f.mode is Mode.PAPER
        assert f.commission == 1.0
        assert f.ts == T0
        order = b.get_orders()[0]
        assert order.status is OrderStatus.FILLED
        assert order.avg_fill_px == 98.0
        assert b.get_account().cash == pytest.approx(2000.0 - (10 * 98.0 + 1.0))
        pos = b.get_positions()[0]
        assert pos.qty == 10 and pos.avg_px == 98.0

    def test_buy_lmt_gap_open_price_improvement(self):
        b = broker()
        b.place_order(make_order(limit=100.0))
        fills = b.step({"NVDA": bar(open=94, high=99, low=93, close=97)})
        assert fills[0].px == 94.0  # filled at open, better than limit

    def test_buy_lmt_no_fill_when_low_above_limit(self):
        b = broker()
        b.place_order(make_order(limit=90.0))
        fills = b.step({"NVDA": bar(open=100, high=105, low=95, close=102)})
        assert fills == []
        assert b.get_orders()[0].status is OrderStatus.SUBMITTED

    def test_sell_lmt_mirrored_fill_at_limit_via_high(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, qty=10, limit=110.0))
        cash_before = b.get_account().cash
        fills = b.step({"NVDA": bar(open=105, high=111, low=104, close=108)})
        assert fills[0].px == 110.0
        assert b.get_account().cash == pytest.approx(cash_before + 10 * 110.0 - 1.0)
        assert b.get_positions() == []  # qty -> 0 removes the position

    def test_sell_lmt_gap_up_price_improvement(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, qty=10, limit=110.0))
        fills = b.step({"NVDA": bar(open=115, high=118, low=112, close=116)})
        assert fills[0].px == 115.0

    def test_no_fill_without_bar_for_symbol(self):
        b = broker()
        b.place_order(make_order())
        fills = b.step({"AMD": bar(symbol="AMD")})
        assert fills == []
        assert b.get_orders()[0].status is OrderStatus.SUBMITTED


# --------------------------------------------------------------------- partial fills


class TestPartialFills:
    def test_volume_cap_forces_partial_then_completes(self):
        b = broker(liquidity_fraction=0.5)
        oid = b.place_order(make_order(qty=10, limit=100.0)).order.id
        fills1 = b.step({"NVDA": bar(open=99, close=99, low=98, volume=12)})  # cap 6
        assert fills1[0].qty == 6
        order = b.get_orders()[0]
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.filled_qty == 6
        fills2 = b.step({"NVDA": bar(open=97, close=97, low=96, volume=12)})  # remaining 4
        assert fills2[0].qty == 4
        order = b.get_orders()[0]
        assert order.status is OrderStatus.FILLED
        assert order.filled_qty == 10
        # weighted order avg: (6*99 + 4*97) / 10
        assert order.avg_fill_px == pytest.approx((6 * 99 + 4 * 97) / 10)
        assert [f.order_id for f in b.get_fills()] == [oid, oid]
        # commission charged per fill
        assert b.get_account().cash == pytest.approx(2000.0 - (6 * 99 + 1) - (4 * 97 + 1))

    def test_position_avg_px_weighted_across_orders(self):
        b = broker()
        b.place_order(make_order(qty=5, limit=100.0))
        b.step({"NVDA": bar(open=100, low=99, close=100)})
        b.place_order(make_order(qty=5, limit=90.0))
        b.step({"NVDA": bar(open=90, low=88, close=90)})
        pos = b.get_positions()[0]
        assert pos.qty == 10
        assert pos.avg_px == pytest.approx((5 * 100.0 + 5 * 90.0) / 10)


# --------------------------------------------------------------------- STP fills


class TestStopFills:
    def test_sell_stp_triggers_at_stop_with_slippage(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, order_type=OrderType.STP, limit=None, stop=95.0))
        fills = b.step({"NVDA": bar(open=98, high=99, low=94, close=96)})
        assert fills[0].px == pytest.approx(95.0 * (1 - SLIP))

    def test_sell_stp_gap_down_fills_at_open_with_slippage(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, order_type=OrderType.STP, limit=None, stop=95.0))
        fills = b.step({"NVDA": bar(open=90, high=92, low=88, close=91)})
        assert fills[0].px == pytest.approx(90.0 * (1 - SLIP))

    def test_sell_stp_not_triggered_above_stop(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, order_type=OrderType.STP, limit=None, stop=95.0))
        assert b.step({"NVDA": bar(open=99, high=101, low=96, close=100)}) == []

    def test_buy_stp_mirrored_with_positive_slippage(self):
        b = broker()
        b.place_order(make_order(order_type=OrderType.STP, limit=None, stop=105.0))
        fills = b.step({"NVDA": bar(open=101, high=106, low=100, close=104)})
        assert fills[0].px == pytest.approx(105.0 * (1 + SLIP))

    def test_buy_stp_gap_up_fills_at_open_with_slippage(self):
        b = broker()
        b.place_order(make_order(order_type=OrderType.STP, limit=None, stop=105.0))
        fills = b.step({"NVDA": bar(open=108, high=110, low=107, close=109)})
        assert fills[0].px == pytest.approx(108.0 * (1 + SLIP))


# --------------------------------------------------------------------- MOC / LOC


class TestOnCloseFills:
    def test_moc_buy_fills_at_close_plus_slippage(self):
        b = broker()
        b.step({"NVDA": bar(close=100)})  # establish a mark for the reservation
        result = b.place_order(make_order(order_type=OrderType.MOC, limit=None))
        assert result.accepted
        fills = b.step({"NVDA": bar(open=101, high=103, low=99, close=102)})
        assert fills[0].px == pytest.approx(102.0 * (1 + SLIP))

    def test_moc_sell_fills_at_close_minus_slippage(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(
            make_order(side=Side.SELL, order_type=OrderType.MOC, qty=10, limit=None)
        )
        fills = b.step({"NVDA": bar(close=104)})
        assert fills[0].px == pytest.approx(104.0 * (1 - SLIP))

    def test_loc_buy_fills_at_close_only_if_close_satisfies_limit(self):
        b = broker()
        b.place_order(make_order(order_type=OrderType.LOC, limit=100.0))
        assert b.step({"NVDA": bar(close=101)}) == []  # close above limit: no fill
        fills = b.step({"NVDA": bar(close=99.5)})
        assert fills[0].px == 99.5  # exactly close, no slippage

    def test_loc_sell_fills_at_close_only_if_close_satisfies_limit(self):
        b = funded_broker(qty=10, px=100.0)
        b.place_order(make_order(side=Side.SELL, order_type=OrderType.LOC, limit=105.0))
        assert b.step({"NVDA": bar(close=104)}) == []
        fills = b.step({"NVDA": bar(close=106)})
        assert fills[0].px == 106.0


# --------------------------------------------------------------------- bracket / OCA


def place_bracket(b: PaperBroker, qty: float = 5, limit: float = 100.0,
                  sl: float = 95.0, tp: float | None = 110.0):
    order = make_order(order_type=OrderType.BRACKET, qty=qty, limit=limit, stop=sl, tp=tp)
    return b.place_order(order)


class TestBracket:
    def test_children_created_inactive_with_oca_group(self):
        b = broker()
        result = place_bracket(b)
        assert result.accepted
        assert len(result.child_orders) == 2
        stop_child, tp_child = result.child_orders
        assert stop_child.order_type is OrderType.STP
        assert stop_child.stop == 95.0
        assert tp_child.order_type is OrderType.LMT
        assert tp_child.limit == 110.0
        for child in (stop_child, tp_child):
            assert child.status is OrderStatus.NEW
            assert child.side is Side.SELL
            assert child.tif is TimeInForce.GTC
            assert child.parent_order_id == result.order.id
        assert stop_child.oca_group == tp_child.oca_group is not None

    def test_bracket_without_tp_creates_only_stop_child(self):
        b = broker()
        result = place_bracket(b, tp=None)
        assert result.accepted
        assert len(result.child_orders) == 1
        assert result.child_orders[0].order_type is OrderType.STP

    def test_children_activate_on_parent_fill_not_same_bar(self):
        b = broker()
        result = place_bracket(b)  # entry 100, sl 95
        # bar dips through the stop AFTER filling the entry at open; children
        # activate but must not fill on this same bar
        fills = b.step({"NVDA": bar(open=100, high=101, low=94, close=96)})
        assert len(fills) == 1
        assert fills[0].order_id == result.order.id
        children = [o for o in b.get_orders() if o.parent_order_id == result.order.id]
        assert all(c.status is OrderStatus.SUBMITTED for c in children)
        assert all(c.qty == 5 for c in children)

    def test_partial_parent_fill_sets_child_qty_to_cumulative(self):
        b = broker(liquidity_fraction=0.5)
        result = place_bracket(b, qty=10)
        b.step({"NVDA": bar(open=99, low=98, close=99, volume=12)})  # cap 6
        parent = next(o for o in b.get_orders() if o.id == result.order.id)
        assert parent.status is OrderStatus.PARTIALLY_FILLED and parent.filled_qty == 6
        children = [o for o in b.get_orders() if o.parent_order_id == result.order.id]
        assert all(c.status is OrderStatus.SUBMITTED and c.qty == 6 for c in children)

    def test_stop_fill_cancels_tp_sibling(self):
        b = broker()
        result = place_bracket(b)  # qty 5, entry 100, sl 95, tp 110
        b.step({"NVDA": bar(open=100, high=101, low=99, close=100)})  # entry fills
        fills = b.step({"NVDA": bar(open=94, high=95, low=92, close=93)})  # gap through stop
        assert len(fills) == 1
        assert fills[0].px == pytest.approx(94.0 * (1 - SLIP))
        orders = {o.id: o for o in b.get_orders()}
        children = [o for o in orders.values() if o.parent_order_id == result.order.id]
        stop_child = next(c for c in children if c.order_type is OrderType.STP)
        tp_child = next(c for c in children if c.order_type is OrderType.LMT)
        assert stop_child.status is OrderStatus.FILLED
        assert tp_child.status is OrderStatus.CANCELLED
        assert b.get_positions() == []

    def test_tp_fill_cancels_stop_sibling(self):
        b = broker()
        result = place_bracket(b)
        b.step({"NVDA": bar(open=100, high=101, low=99, close=100)})  # entry fills
        fills = b.step({"NVDA": bar(open=108, high=112, low=107, close=111)})  # tp reached
        assert len(fills) == 1
        assert fills[0].px == 110.0
        children = [o for o in b.get_orders() if o.parent_order_id == result.order.id]
        stop_child = next(c for c in children if c.order_type is OrderType.STP)
        tp_child = next(c for c in children if c.order_type is OrderType.LMT)
        assert tp_child.status is OrderStatus.FILLED
        assert stop_child.status is OrderStatus.CANCELLED
        assert b.get_positions() == []

    def test_cancel_parent_cancels_inactive_children(self):
        b = broker()
        result = place_bracket(b)
        assert b.cancel_order(result.order.id) is True
        children = [o for o in b.get_orders() if o.parent_order_id == result.order.id]
        assert all(c.status is OrderStatus.CANCELLED for c in children)


# --------------------------------------------------------------------- lifecycle / expiry


class TestLifecycle:
    def test_day_order_expires_at_end_of_day_gtc_survives(self):
        b = broker()
        day_id = b.place_order(make_order(qty=3, tif=TimeInForce.DAY, limit=90.0)).order.id
        gtc_id = b.place_order(make_order(qty=3, tif=TimeInForce.GTC, limit=90.0)).order.id
        b.step({"NVDA": bar(open=100, low=95, close=100)})  # no fill for either
        b.end_of_day()
        orders = {o.id: o for o in b.get_orders()}
        assert orders[day_id].status is OrderStatus.EXPIRED
        assert orders[gtc_id].status is OrderStatus.SUBMITTED

    def test_expiry_releases_reservation(self):
        b = broker(starting_cash=2000.0)
        b.place_order(make_order(qty=15, tif=TimeInForce.DAY, limit=100.0))  # reserves 1501
        b.end_of_day()
        assert b.place_order(make_order(qty=15, limit=100.0)).accepted

    def test_cancel_terminal_or_unknown_returns_false(self):
        b = broker()
        result = b.place_order(make_order(limit=100.0))
        b.step({"NVDA": bar(open=99, low=98, close=100)})  # fills
        assert b.cancel_order(result.order.id) is False  # FILLED is terminal
        assert b.cancel_order("no-such-id") is False

    def test_cancel_partially_filled_keeps_fills(self):
        b = broker(liquidity_fraction=0.5)
        result = b.place_order(make_order(qty=10, limit=100.0))
        b.step({"NVDA": bar(open=99, low=98, close=99, volume=8)})  # fills 4
        assert b.cancel_order(result.order.id) is True
        order = b.get_orders()[0]
        assert order.status is OrderStatus.CANCELLED
        assert order.filled_qty == 4
        assert b.get_positions()[0].qty == 4

    def test_full_lifecycle_place_partial_fill_cancel_reject(self):
        """Loop.md §9: place -> partial -> fill -> cancel -> reject."""
        b = broker(starting_cash=2000.0, liquidity_fraction=0.5)
        result = b.place_order(make_order(qty=10, limit=100.0))  # place
        assert result.order.status is OrderStatus.SUBMITTED
        b.step({"NVDA": bar(open=99, low=98, close=99, volume=8)})  # partial (4)
        assert b.get_orders()[0].status is OrderStatus.PARTIALLY_FILLED
        b.step({"NVDA": bar(open=98, low=97, close=98, volume=100)})  # fill rest
        assert b.get_orders()[0].status is OrderStatus.FILLED
        second = b.place_order(make_order(qty=5, limit=100.0))  # place another
        assert b.cancel_order(second.order.id) is True  # cancel
        rejected = b.place_order(make_order(qty=1000, limit=100.0))  # reject
        assert not rejected.accepted and rejected.order.status is OrderStatus.REJECTED


# --------------------------------------------------------------------- account math


class TestAccount:
    def test_initial_snapshot(self):
        snap = broker().get_account()
        assert snap.mode is Mode.PAPER
        assert snap.equity == 2000.0
        assert snap.cash == 2000.0
        assert snap.upnl == 0.0
        assert snap.day_pnl == 0.0
        assert snap.drawdown_pct == 0.0
        assert snap.breaker_state is BreakerState.NORMAL

    def test_equity_upnl_day_pnl_drawdown_after_loss(self):
        b = broker(starting_cash=2000.0)
        b.place_order(make_order(qty=10, limit=100.0))
        b.step({"NVDA": bar(open=100, high=101, low=89, close=90)})  # fill @100, mark 90
        snap = b.get_account()
        assert snap.cash == pytest.approx(999.0)  # 2000 - 1000 - 1
        assert snap.equity == pytest.approx(999.0 + 10 * 90.0)  # 1899
        assert snap.upnl == pytest.approx((90.0 - 100.0) * 10)  # -100
        assert snap.day_pnl == pytest.approx(1899.0 - 2000.0)  # -101
        assert snap.drawdown_pct == pytest.approx(-101.0 / 2000.0 * 100.0)

    def test_drawdown_clamped_to_zero_on_gains(self):
        b = broker(starting_cash=2000.0)
        b.place_order(make_order(qty=10, limit=100.0))
        b.step({"NVDA": bar(open=100, high=112, low=99, close=110)})
        snap = b.get_account()
        assert snap.day_pnl == pytest.approx(99.0)  # 2099 - 2000
        assert snap.drawdown_pct == 0.0

    def test_start_of_day_resets_day_open_equity(self):
        b = broker(starting_cash=2000.0)
        b.place_order(make_order(qty=10, limit=100.0))
        b.step({"NVDA": bar(open=100, high=101, low=89, close=90)})
        assert b.get_account().day_pnl == pytest.approx(-101.0)
        b.start_of_day()
        snap = b.get_account()
        assert snap.day_pnl == 0.0
        assert snap.drawdown_pct == 0.0


# --------------------------------------------------------------------- isolation


class TestCopies:
    def test_get_orders_returns_copies(self):
        b = broker()
        b.place_order(make_order())
        b.get_orders()[0].status = OrderStatus.CANCELLED  # mutate the copy
        assert b.get_orders()[0].status is OrderStatus.SUBMITTED

    def test_get_fills_returns_copies(self):
        b = broker()
        b.place_order(make_order())
        b.step({"NVDA": bar(open=99, low=98, close=99)})
        b.get_fills()[0].qty = 12345.0
        assert b.get_fills()[0].qty == 10

    def test_get_positions_returns_copies(self):
        b = funded_broker(qty=10)
        b.get_positions()[0].qty = 0.0
        assert b.get_positions()[0].qty == 10
