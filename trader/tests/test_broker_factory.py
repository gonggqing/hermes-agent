"""Tests for the broker factory (Loop.md §5.1, §3).

Covers the paper→live account derivation from the triple gate and that
constructing an IBKR broker via the factory NEVER touches the network or
imports ib_async (a mock client is injected).
"""

from __future__ import annotations

import sys

import pytest

from swing_trader.broker_factory import build_broker
from swing_trader.config import BrokerBackend, Settings
from swing_trader.ibkr_broker import IBKRBroker
from swing_trader.paper_broker import PaperBroker
from swing_trader.schemas import Mode, Role


def _settings(**over) -> Settings:
    base = dict(broker=BrokerBackend.PAPER, human_confirm=False, dry_run=True)
    base.update(over)
    return Settings(**base)


class _FakeIB:
    def is_connected(self): return True
    def connect(self): ...
    def account(self): return {"NetLiquidation": "0", "SettledCash": "0"}
    def positions(self): return []
    def trades(self): return []
    def fills(self): return []
    def place(self, spec): return "ib-1"
    def cancel(self, ref): return True


def test_paper_backend_builds_paper_broker():
    b = build_broker(_settings(broker=BrokerBackend.PAPER), starting_cash=50_000)
    assert isinstance(b, PaperBroker)


def test_ibkr_ungated_builds_paper_account():
    # BROKER=ibkr but DRY_RUN=true / no HUMAN_CONFIRM → paper account, PAPER tag.
    b = build_broker(_settings(broker=BrokerBackend.IBKR),
                     client_factory=lambda: _FakeIB())
    assert isinstance(b, IBKRBroker)
    assert b.paper is True and b.mode is Mode.PAPER


def test_ibkr_fully_gated_builds_live_account():
    s = _settings(broker=BrokerBackend.IBKR, human_confirm=True, dry_run=False,
                  ibkr_port=7496)  # live TWS port
    assert s.live_orders_allowed is True
    b = build_broker(s, client_factory=lambda: _FakeIB())
    assert b.paper is False and b.mode is Mode.LIVE and b.port == 7496


def test_ibkr_ungated_live_port_refused():
    # Un-gated config pointed at a LIVE port must fail closed at construction.
    s = _settings(broker=BrokerBackend.IBKR, ibkr_port=7496)  # dry_run still true
    with pytest.raises(ValueError, match="LIVE port"):
        build_broker(s, client_factory=lambda: _FakeIB())


def test_ibkr_passes_connection_settings_and_role():
    s = _settings(broker=BrokerBackend.IBKR, ibkr_host="10.0.0.9",
                  ibkr_port=4002, ibkr_client_id=42)
    b = build_broker(s, client_factory=lambda: _FakeIB(),
                     role_for_symbol=lambda _s: Role.CORE)
    assert (b.host, b.port, b.client_id) == ("10.0.0.9", 4002, 42)
    assert b._role_for("X") is Role.CORE


def test_factory_never_imports_ib_async():
    build_broker(_settings(broker=BrokerBackend.IBKR), client_factory=lambda: _FakeIB())
    assert not any(n.split(".")[0] in {"ib_async", "ib_insync"} for n in sys.modules)


def test_unknown_backend_raises():
    with pytest.raises(NotImplementedError, match="no adapter"):
        build_broker(_settings(broker=BrokerBackend.ALPACA))
