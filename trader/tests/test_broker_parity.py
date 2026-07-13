"""Broker parity harness (Loop.md §5.1 / #37): PaperBroker vs IBKRBroker.

Proves the two adapters are SUBSTITUTABLE at the BrokerInterface contract the
ExecutionEngine depends on — so IBKR slots in the moment the account funds
without changing the decision/execution path. Fill *mechanics* differ (the
PaperBroker simulates from quotes; IBKRBroker's fills come from the IB
transport), so each broker is driven to the same state by its own mechanism and
the OBSERVABLE domain contract is asserted equal. Fully offline (FakeIBClient).
"""

from __future__ import annotations

import pytest

from swing_trader.ibkr_broker import (
    IBKRBroker,
    IbExec,
    IbOrderSpec,
    IbTradeState,
)
from swing_trader.interfaces import BrokerInterface
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import (
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

START_CASH = 10_000.0


class _FakeIBClient:
    """Minimal drivable IB transport (mirrors tests/test_ibkr_broker.py)."""

    def __init__(self, cash=START_CASH):
        self._connected = False
        self._n = 0
        self._trades: dict[str, IbTradeState] = {}
        self._specs: dict[str, IbOrderSpec] = {}
        self._fills: list[IbExec] = []
        self._by_broker: dict[str, str] = {}
        self._acct = {"NetLiquidation": str(cash), "SettledCash": str(cash)}

    def is_connected(self): return self._connected
    def connect(self): self._connected = True

    def place(self, spec):
        self._n += 1
        bref = f"ib-{self._n}"
        self._specs[spec.order_ref] = spec
        self._trades[spec.order_ref] = IbTradeState(order_ref=spec.order_ref,
                                                    status="Submitted", filled=0.0,
                                                    remaining=spec.qty)
        self._by_broker[bref] = spec.order_ref
        return bref

    def cancel(self, broker_ref):
        ref = self._by_broker.get(broker_ref)
        if ref is None or self._trades[ref].status == "Filled":
            return False
        self._trades[ref].status = "Cancelled"
        return True

    def trades(self): return list(self._trades.values())
    def fills(self): return list(self._fills)
    def positions(self): return []
    def account(self): return dict(self._acct)

    def fill(self, order_ref, qty, px):
        st, spec = self._trades[order_ref], self._specs[order_ref]
        st.filled += qty
        st.remaining = max(0.0, spec.qty - st.filled)
        st.status = "Filled" if st.remaining <= 1e-9 else "Submitted"
        st.avg_fill_px = px
        self._fills.append(IbExec(exec_id=f"x{len(self._fills)+1}", order_ref=order_ref,
                                  symbol=spec.symbol, side=spec.action, qty=qty, px=px))


def _paper():
    # LMT/bracket BUY reference price is the limit itself, so no quote feed
    # is needed to reserve cash.
    return PaperBroker(starting_cash=START_CASH)


def _ibkr():
    return IBKRBroker(client_factory=lambda: _FakeIBClient(), paper=True)


BROKERS = {"paper": _paper, "ibkr": _ibkr}


def _order(**over) -> Order:
    base = dict(mode=Mode.PAPER, symbol="NVDA", side=Side.BUY, qty=10,
                order_type=OrderType.LMT, limit=100.0, stop=95.0, tif=TimeInForce.GTC)
    base.update(over)
    return Order(**base)


@pytest.fixture(params=list(BROKERS))
def broker(request):
    return request.param, BROKERS[request.param]()


class TestSubstitutability:
    def test_both_are_broker_interface(self, broker):
        _, b = broker
        assert isinstance(b, BrokerInterface)

    def test_accepts_valid_buy(self, broker):
        name, b = broker
        o = _order()
        r = b.place_order(o)
        assert r.accepted is True
        assert r.order.status is OrderStatus.SUBMITTED
        assert r.order.id == o.id
        # broker_ref is broker-specific: the internal PaperBroker simulator has
        # no external reference; the IBKR adapter stamps IBKR's. (Documented
        # difference — NOT part of the substitutable contract.)
        if name == "ibkr":
            assert r.order.broker_ref is not None
        else:
            assert r.order.broker_ref is None

    def test_input_order_never_mutated(self, broker):
        _, b = broker
        o = _order()
        b.place_order(o)
        assert o.status is OrderStatus.NEW and o.broker_ref is None

    def test_rejects_buy_exceeding_cash(self, broker):
        _, b = broker
        # 200 sh * $100 = $20k > $10k available/settled → both must reject.
        r = b.place_order(_order(qty=200, limit=100.0))
        assert r.accepted is False
        assert r.order.status is OrderStatus.REJECTED
        assert r.reason  # a human-readable reason on both

    def test_duplicate_id_never_double_places(self, broker):
        name, b = broker
        o = _order()
        r1 = b.place_order(o)
        r2 = b.place_order(o)  # same id again
        assert r1.accepted
        # Neither broker creates a second live order for the same id: paper
        # rejects the duplicate; ibkr idempotently replays the first.
        active = [x for x in b.get_orders() if x.id == o.id]
        assert len(active) == 1
        if name == "ibkr":
            assert r2.accepted and "idempotent" in r2.reason
        else:
            assert not r2.accepted and "duplicate" in r2.reason

    def test_cancel_semantics(self, broker):
        _, b = broker
        o = _order()
        b.place_order(o)
        assert b.cancel_order(o.id) is True
        assert b.cancel_order("nope-unknown") is False
        cancelled = [x for x in b.get_orders() if x.id == o.id][0]
        assert cancelled.status is OrderStatus.CANCELLED

    def test_get_orders_and_account_types(self, broker):
        _, b = broker
        b.place_order(_order())
        acct = b.get_account()
        assert acct.equity >= 0 and acct.cash >= 0
        assert all(isinstance(x, Order) for x in b.get_orders())


def test_account_cash_matches_starting_capital():
    """Both brokers report the same spendable cash for the same capital."""
    assert _paper().get_account().cash == pytest.approx(START_CASH)
    assert _ibkr().get_account().cash == pytest.approx(START_CASH)
