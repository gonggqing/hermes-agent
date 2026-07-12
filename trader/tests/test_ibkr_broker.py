"""Tests for the IBKRBroker Phase 1 stub (Loop.md §5.1, §2, backlog 15).

Fully deterministic, never touches the network (Loop.md §3): the stub must
neither open sockets nor import ib_async at import/construct time — asserted
here by poisoning ``socket.socket`` while importing and constructing.
"""

from __future__ import annotations

import importlib
import socket
import sys

import pytest

from swing_trader.ibkr_broker import IBKRBroker
from swing_trader.interfaces import BrokerInterface
from swing_trader.schemas import Mode, Order, OrderStatus, OrderType, Side, TimeInForce

INTERFACE_METHODS = [
    "get_account",
    "get_positions",
    "place_order",
    "cancel_order",
    "get_orders",
    "get_fills",
]


def make_order() -> Order:
    return Order(
        mode=Mode.PAPER,
        symbol="NVDA",
        side=Side.BUY,
        qty=1,
        order_type=OrderType.LMT,
        limit=100.0,
        tif=TimeInForce.GTC,
    )


def call_method(broker: IBKRBroker, method: str) -> None:
    """Invoke an interface method with minimal valid arguments."""
    if method == "place_order":
        broker.place_order(make_order())
    elif method == "cancel_order":
        broker.cancel_order("some-order-id")
    else:
        getattr(broker, method)()


def test_is_broker_interface() -> None:
    broker = IBKRBroker()
    assert isinstance(broker, BrokerInterface)


def test_constructor_defaults() -> None:
    broker = IBKRBroker()
    assert broker.host == "127.0.0.1"
    assert broker.port == 7497  # TWS paper port (live is 7496)
    assert broker.client_id == 1
    assert broker.paper is True


def test_constructor_stores_custom_params() -> None:
    broker = IBKRBroker(host="10.0.0.5", port=4002, client_id=7, paper=False)
    assert broker.host == "10.0.0.5"
    assert broker.port == 4002
    assert broker.client_id == 7
    assert broker.paper is False


@pytest.mark.parametrize("method", INTERFACE_METHODS)
def test_every_method_raises_not_implemented_mentioning_phase_1(method: str) -> None:
    broker = IBKRBroker()
    with pytest.raises(NotImplementedError) as excinfo:
        call_method(broker, method)
    msg = str(excinfo.value)
    assert "Phase 1" in msg
    assert f"IBKRBroker.{method}" in msg
    assert "ib_async" in msg


def test_place_order_stub_does_not_mutate_order() -> None:
    broker = IBKRBroker()
    order = make_order()
    before = order.model_dump()
    with pytest.raises(NotImplementedError):
        broker.place_order(order)
    assert order.status is OrderStatus.NEW
    assert order.broker_ref is None
    assert order.model_dump() == before


def test_no_network_at_import_or_construct(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loop.md §3: the stub must never touch the network.

    ``socket.socket`` is poisoned, then the module is re-imported and a
    broker constructed. There is nothing further to assert beyond neither
    step raising: any connection attempt (ib_async import side effects,
    connect-on-construct) would trip the poisoned socket.
    """

    def _no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted by IBKRBroker stub")

    monkeypatch.setattr(socket, "socket", _no_network)
    module = importlib.reload(sys.modules["swing_trader.ibkr_broker"])
    broker = module.IBKRBroker(host="example.invalid", port=7496, paper=False)
    assert broker.port == 7496


def test_stub_never_imports_ib_async() -> None:
    """Phase 0 has no ib_async dependency; the stub must not import it."""
    assert not any(name.split(".")[0] in {"ib_async", "ib_insync"} for name in sys.modules)
