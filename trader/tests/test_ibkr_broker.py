"""Tests for IBKRBroker (Loop.md §5.1, §2, §7) — the real broker adapter driven
ENTIRELY OFFLINE through a mock IB transport (FakeIBClient).

Never touches the network (Loop.md §3): constructing the broker opens no socket
and imports no ib_async (asserted by poisoning socket.socket + a sys.modules
check). The place → partial → fill → cancel → reject / bracket-OCA lifecycle is
driven by mutating the fake IB's state — the same suite a live IBKRBroker must
satisfy against real TWS.
"""

from __future__ import annotations

import importlib
import socket
import sys

import pytest

from swing_trader.ibkr_broker import (
    IBKRBroker,
    IbExec,
    IbOrderSpec,
    IbPosition,
    IbTradeState,
)
from swing_trader.interfaces import BrokerInterface
from swing_trader.schemas import Mode, Order, OrderStatus, OrderType, Role, Side, TimeInForce


# --------------------------------------------------------------- mock IB


class FakeIBClient:
    """Offline stand-in for a live IB connection; drivable from tests."""

    def __init__(self, account=None, positions=None, raise_on_place=False):
        self._connected = False
        self._n = 0
        self._trades: dict[str, IbTradeState] = {}
        self._specs: dict[str, IbOrderSpec] = {}
        self._fills: list[IbExec] = []
        self._by_broker: dict[str, str] = {}
        self._acct = account or {"NetLiquidation": "10000", "SettledCash": "10000",
                                 "UnrealizedPnL": "0"}
        self._positions = positions or []
        self._raise_on_place = raise_on_place
        self.placed: list[IbOrderSpec] = []

    # -- IBClient surface --
    def is_connected(self): return self._connected
    def connect(self): self._connected = True

    def place(self, spec: IbOrderSpec) -> str:
        if self._raise_on_place:
            raise RuntimeError("order rejected by IBKR (error 201: no trading permissions)")
        self._n += 1
        bref = f"ib-{self._n}"
        self._specs[spec.order_ref] = spec
        self._trades[spec.order_ref] = IbTradeState(order_ref=spec.order_ref,
                                                    status="Submitted", filled=0.0,
                                                    remaining=spec.qty)
        self._by_broker[bref] = spec.order_ref
        self.placed.append(spec)
        return bref

    def cancel(self, broker_ref: str) -> bool:
        ref = self._by_broker.get(broker_ref)
        if ref is None or self._trades[ref].status == "Filled":
            return False
        self._trades[ref].status = "Cancelled"
        return True

    def trades(self): return list(self._trades.values())
    def fills(self): return list(self._fills)
    def positions(self): return list(self._positions)
    def account(self): return dict(self._acct)

    # -- test drivers --
    def fill(self, order_ref: str, qty: float, px: float, commission=1.0, exec_id=None):
        st, spec = self._trades[order_ref], self._specs[order_ref]
        prev = st.filled
        st.filled += qty
        st.remaining = max(0.0, spec.qty - st.filled)
        st.avg_fill_px = px if st.avg_fill_px is None else \
            (st.avg_fill_px * prev + px * qty) / st.filled
        st.status = "Filled" if st.remaining <= 1e-9 else "Submitted"
        self._fills.append(IbExec(exec_id=exec_id or f"exec-{len(self._fills) + 1}",
                                  order_ref=order_ref, symbol=spec.symbol, side=spec.action,
                                  qty=qty, px=px, commission=commission))
        if st.status == "Filled" and spec.oca_group:  # OCA: fill cancels siblings
            for r, sp in self._specs.items():
                if (r != order_ref and sp.oca_group == spec.oca_group
                        and self._trades[r].status not in ("Filled", "Cancelled")):
                    self._trades[r].status = "Cancelled"

    def reject(self, order_ref: str):
        self._trades[order_ref].status = "Inactive"


def _broker(*, paper=True, **kw):
    fake = FakeIBClient(**kw)
    return IBKRBroker(client_factory=lambda: fake, paper=paper), fake


def _order(**over) -> Order:
    base = dict(mode=Mode.PAPER, symbol="NVDA", side=Side.BUY, qty=10,
                order_type=OrderType.LMT, limit=100.0, tif=TimeInForce.GTC)
    base.update(over)
    return Order(**base)


def _bracket(**over) -> Order:
    base = dict(mode=Mode.PAPER, symbol="NVDA", side=Side.BUY, qty=10,
                order_type=OrderType.BRACKET, limit=100.0, stop=95.0, tp=110.0,
                tif=TimeInForce.GTC)
    base.update(over)
    return Order(**base)


# --------------------------------------------------------- construction


def test_is_broker_interface():
    assert isinstance(IBKRBroker(), BrokerInterface)


def test_constructor_defaults():
    b = IBKRBroker()
    assert (b.host, b.port, b.client_id, b.paper) == ("127.0.0.1", 7497, 1, True)


def test_constructor_custom():
    b = IBKRBroker(host="10.0.0.5", port=4002, client_id=7, paper=False)
    assert (b.host, b.port, b.client_id, b.paper) == ("10.0.0.5", 4002, 7, False)


def test_paper_true_on_live_port_refused():
    for live_port in (7496, 4001):
        with pytest.raises(ValueError, match="LIVE port"):
            IBKRBroker(port=live_port, paper=True)


def test_no_network_at_construct(monkeypatch):
    def _no_net(*a, **k):
        raise AssertionError("network access at construct")
    monkeypatch.setattr(socket, "socket", _no_net)
    module = importlib.reload(sys.modules["swing_trader.ibkr_broker"])
    b = module.IBKRBroker(host="example.invalid", port=7496, paper=False)
    assert b.port == 7496


def test_never_imports_ib_async():
    # FakeIBClient path never triggers the lazy ib_async import.
    b, fake = _broker()
    b.place_order(_order())
    assert not any(n.split(".")[0] in {"ib_async", "ib_insync"} for n in sys.modules)


# --------------------------------------------------------- place_order


class TestPlaceOrder:
    def test_lmt_accepted_and_submitted(self):
        b, fake = _broker()
        o = _order()
        r = b.place_order(o)
        assert r.accepted and r.order.status is OrderStatus.SUBMITTED
        # paper account (default) → PAPER tag; broker_ref stamped from IBKR.
        assert r.order.broker_ref == "ib-1" and r.order.mode is Mode.PAPER
        assert len(fake.placed) == 1 and fake.placed[0].order_type == "LMT"

    def test_live_account_tags_mode_live(self):
        b, fake = _broker(paper=False)
        r = b.place_order(_order())
        assert r.accepted and r.order.mode is Mode.LIVE  # real account → LIVE tag

    def test_input_order_not_mutated(self):
        b, _ = _broker()
        o = _order()
        b.place_order(o)
        assert o.status is OrderStatus.NEW and o.broker_ref is None  # caller's copy pristine

    def test_settled_cash_reject(self):
        b, _ = _broker(account={"NetLiquidation": "500", "SettledCash": "500"})
        r = b.place_order(_order(qty=10, limit=100.0))  # needs ~1000 > 500 settled
        assert not r.accepted and r.order.status is OrderStatus.REJECTED
        assert "settled cash" in r.reason

    def test_sell_skips_settled_cash_check(self):
        b, _ = _broker(account={"SettledCash": "0"})
        r = b.place_order(_order(side=Side.SELL, order_type=OrderType.MOC, limit=None))
        assert r.accepted

    def test_api_error_is_rejection(self):
        b, _ = _broker(raise_on_place=True)
        r = b.place_order(_order())
        assert not r.accepted and r.order.status is OrderStatus.REJECTED
        assert "IBKR rejected" in r.reason

    def test_idempotent_replay(self):
        b, fake = _broker()
        o = _order()
        b.place_order(o)
        r2 = b.place_order(o)  # same order.id → must NOT double-submit
        assert r2.accepted and "idempotent" in r2.reason
        assert len(fake.placed) == 1


class TestBracket:
    def test_parent_and_children_with_oca_and_transmit(self):
        b, fake = _broker()
        r = b.place_order(_bracket())
        assert r.accepted and len(r.child_orders) == 2
        stp = next(c for c in r.child_orders if c.order_type is OrderType.STP)
        tp = next(c for c in r.child_orders if c.order_type is OrderType.LMT)
        assert stp.side is Side.SELL and stp.stop == 95.0
        assert tp.side is Side.SELL and tp.limit == 110.0
        assert stp.oca_group == tp.oca_group == r.order.oca_group
        # transmit flags: parent False, last child True → IBKR activates the
        # whole group atomically (parent is never live-and-unprotected).
        specs = fake.placed
        assert specs[0].order_type == "LMT" and specs[0].transmit is False  # parent
        assert specs[-1].transmit is True  # last child transmits the group

    def test_stop_only_bracket(self):
        b, _ = _broker()
        r = b.place_order(_bracket(tp=None))
        assert len(r.child_orders) == 1 and r.child_orders[0].order_type is OrderType.STP


class TestLifecycle:
    def test_partial_then_full_fill(self):
        b, fake = _broker()
        o = _order(qty=10, limit=100.0)
        b.place_order(o)
        fake.fill(o.id, 4, 99.5)
        orders = {x.id: x for x in b.get_orders()}
        assert orders[o.id].status is OrderStatus.PARTIALLY_FILLED
        assert orders[o.id].filled_qty == 4
        fake.fill(o.id, 6, 100.0)
        assert b.get_orders()[0].status is OrderStatus.FILLED
        fills = b.get_fills()
        assert len(fills) == 2 and all(f.mode is Mode.PAPER for f in fills)
        assert {f.id for f in fills} == {"exec-1", "exec-2"}  # execId -> Fill.id
        assert all(f.order_id == o.id for f in fills)

    def test_active_only_filter(self):
        b, fake = _broker()
        o1, o2 = _order(), _order(symbol="AMD")
        b.place_order(o1)
        b.place_order(o2)
        fake.fill(o2.id, 10, 100.0)  # o2 fully filled
        active = [o.symbol for o in b.get_orders(active_only=True)]
        assert active == ["NVDA"]  # filled AMD excluded

    def test_cancel(self):
        b, fake = _broker()
        o = _order()
        b.place_order(o)
        assert b.cancel_order(o.id) is True
        assert b.get_orders()[0].status is OrderStatus.CANCELLED
        assert b.cancel_order("unknown-id") is False

    def test_cancel_filled_returns_false(self):
        b, fake = _broker()
        o = _order()
        b.place_order(o)
        fake.fill(o.id, 10, 100.0)
        assert b.cancel_order(o.id) is False

    def test_reject_surfaces_as_rejected(self):
        b, fake = _broker()
        o = _order()
        b.place_order(o)
        fake.reject(o.id)
        assert b.get_orders()[0].status is OrderStatus.REJECTED

    def test_bracket_stop_fill_cancels_tp_sibling(self):
        b, fake = _broker()
        r = b.place_order(_bracket())
        parent = r.order
        fake.fill(parent.id, 10, 100.0)  # parent fills
        stp = next(c for c in r.child_orders if c.order_type is OrderType.STP)
        fake.fill(stp.id, 10, 95.0)  # stop fills → OCA cancels tp
        by_id = {o.id: o for o in b.get_orders()}
        tp = next(c for c in r.child_orders if c.order_type is OrderType.LMT)
        assert by_id[stp.id].status is OrderStatus.FILLED
        assert by_id[tp.id].status is OrderStatus.CANCELLED


class TestAccountPositions:
    def test_account_maps_settled_cash(self):
        b, _ = _broker(account={"NetLiquidation": "12000", "SettledCash": "3000",
                                "AvailableFunds": "9000", "UnrealizedPnL": "150"})
        acct = b.get_account()
        assert acct.equity == 12000 and acct.cash == 3000  # SETTLED, not AvailableFunds
        assert acct.upnl == 150 and acct.mode is Mode.PAPER  # paper account (default)
        assert acct.breaker_state.value == "NORMAL"

    def test_positions_mapped(self):
        b, _ = _broker(positions=[IbPosition("NVDA", 10, 98.5), IbPosition("AMD", 0, 0)])
        pos = b.get_positions()
        assert len(pos) == 1 and pos[0].symbol == "NVDA" and pos[0].qty == 10
        assert pos[0].avg_px == 98.5 and pos[0].pool is Role.ROTATION

    def test_role_lookup_used(self):
        b = IBKRBroker(client_factory=lambda: FakeIBClient(positions=[IbPosition("NVDA", 5, 100)]),
                       role_for_symbol=lambda s: Role.CORE)
        assert b.get_positions()[0].pool is Role.CORE
