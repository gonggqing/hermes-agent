"""Tests for the kill-switch + cancel-all HTTP endpoints (Loop.md §3 / P0.95).

ENGAGE is open to any surface (halting is always safe); RELEASE and CANCEL-ALL
are HUMAN-only (system/LLM/agent → 403), mirroring the confirmation gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.execution import ExecutionEngine
from swing_trader.killswitch import KillSwitch
from swing_trader.ledger import Ledger
from swing_trader.schemas import Mode, Order, OrderStatus, OrderType, Side

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class _StubBroker:
    def __init__(self, orders):
        self._orders = {o.id: o for o in orders}

    def get_orders(self, active_only=False):
        return list(self._orders.values())

    def cancel_order(self, order_id):
        o = self._orders.get(order_id)
        if o is None:
            return False
        self._orders[order_id] = o.model_copy(update={"status": OrderStatus.CANCELLED})
        return True


@pytest.fixture()
def env(tmp_path):
    ledger = Ledger(url=f"sqlite:///{tmp_path/'ks.db'}")
    ks = KillSwitch(tmp_path / "KILL", clock=lambda: NOW)
    entry = Order(id="e1", mode=Mode.PAPER, symbol="NVDA", side=Side.BUY, qty=2,
                  order_type=OrderType.LMT, limit=100.0, status=OrderStatus.SUBMITTED)
    stop = Order(id="s1", mode=Mode.PAPER, symbol="NVDA", side=Side.SELL, qty=2,
                 order_type=OrderType.STP, stop=90.0, status=OrderStatus.SUBMITTED)
    broker = _StubBroker([entry, stop])
    runtime = FinanceRuntime(ledger=ledger, broker=broker, clock=lambda: NOW)
    runtime.kill_switch = ks
    runtime.execution = ExecutionEngine(broker, ledger, mode=Mode.PAPER)
    return ks, TestClient(create_app(runtime))


class TestKillSwitchEndpoints:
    def test_status_starts_released(self, env):
        _, client = env
        r = client.get("/v1/killswitch")
        assert r.status_code == 200 and r.json()["engaged"] is False

    def test_engage_any_surface(self, env):
        ks, client = env
        r = client.post("/v1/killswitch/engage",
                        json={"actor": "agent", "reason": "vol spike"},
                        headers={"X-Finance-Surface": "system"})
        assert r.status_code == 200 and r.json()["engaged"] is True
        assert ks.engaged() is True
        # reflected in status + health-relevant fields
        assert client.get("/v1/killswitch").json()["reason"] == "vol spike"

    def test_release_requires_human(self, env):
        ks, client = env
        ks.engage(reason="halt")
        # system/LLM actor is refused
        r = client.post("/v1/killswitch/release",
                        json={"actor": "llm"},
                        headers={"X-Finance-Surface": "web"})
        assert r.status_code == 403
        # system surface is refused
        r = client.post("/v1/killswitch/release",
                        json={"actor": "gongqing"},
                        headers={"X-Finance-Surface": "system"})
        assert r.status_code == 403
        assert ks.engaged() is True  # still halted

    def test_release_by_human(self, env):
        ks, client = env
        ks.engage(reason="halt")
        r = client.post("/v1/killswitch/release",
                        json={"actor": "gongqing"},
                        headers={"X-Finance-Surface": "desktop"})
        assert r.status_code == 200 and r.json()["engaged"] is False
        assert ks.engaged() is False

    def test_status_503_when_unattached(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'x.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        assert client.get("/v1/killswitch").status_code == 503


class TestCancelAllEndpoint:
    def test_cancel_all_requires_human(self, env):
        _, client = env
        r = client.post("/v1/orders/cancel-all",
                        json={"actor": "agent"},
                        headers={"X-Finance-Surface": "system"})
        assert r.status_code == 403

    def test_cancel_all_by_human(self, env):
        _, client = env
        r = client.post("/v1/orders/cancel-all",
                        json={"actor": "gongqing", "include_protection": True},
                        headers={"X-Finance-Surface": "desktop"})
        assert r.status_code == 200
        body = r.json()
        assert body["n_cancelled"] == 2  # entry + stop
        assert {o["id"] for o in body["cancelled"]} == {"e1", "s1"}

    def test_cancel_all_keep_protection(self, env):
        _, client = env
        r = client.post("/v1/orders/cancel-all",
                        json={"actor": "gongqing", "include_protection": False},
                        headers={"X-Finance-Surface": "web"})
        assert r.status_code == 200
        body = r.json()
        assert [o["id"] for o in body["cancelled"]] == ["e1"]  # stop kept
