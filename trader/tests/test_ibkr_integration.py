"""Integration: the serve-path assembly for IBKR (Loop.md §5.1 / Phase 0.95).

Proves the pieces the `serve` command wires together actually compose — the
broker factory builds an IBKRBroker from Settings, a DailyLoop drives it, and an
engaged kill-switch flows factory→loop→assess_health→RiskEngine so NEW entries
are halted. Fully offline (FakeIBClient + kill-switch in tmp_path).
"""

from __future__ import annotations

from datetime import datetime, timezone

from swing_trader.broker_factory import build_broker
from swing_trader.config import BrokerBackend, Settings
from swing_trader.dailyloop import DailyLoop
from swing_trader.ibkr_broker import IbPosition
from swing_trader.killswitch import KillSwitch
from swing_trader.ledger import Ledger
from swing_trader.simulate import SimFeed

NOW = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)


class _FakeIB:
    def __init__(self):
        self._c = False

    def is_connected(self): return self._c
    def connect(self): self._c = True
    def account(self): return {"NetLiquidation": "50000", "SettledCash": "50000"}
    def positions(self): return [IbPosition("NVDA", 0, 0)]
    def trades(self): return []
    def fills(self): return []
    def place(self, s): return "ib-1"
    def cancel(self, r): return True


def _settings(**over):
    base = dict(broker=BrokerBackend.IBKR, human_confirm=False, dry_run=True)
    base.update(over)
    return Settings(**base)


def test_factory_builds_ibkr_for_daily_loop(tmp_path):
    # Import fresh here (not the module-top binding): another test module reloads
    # swing_trader.ibkr_broker, which rebinds the class object — build_broker
    # imports it fresh too, so a fresh import keeps isinstance reload-proof.
    from swing_trader.ibkr_broker import IBKRBroker as _IBKR

    b = build_broker(_settings(), client_factory=lambda: _FakeIB())
    assert isinstance(b, _IBKR)
    ledger = Ledger(url=f"sqlite:///{tmp_path/'i.db'}")
    # The broker satisfies the interface the loop reads at startup.
    loop = DailyLoop(SimFeed({}), b, ledger, clock=lambda: NOW)
    assert loop.broker is b
    assert b.get_account().equity == 50000


def test_kill_switch_flows_factory_to_health(tmp_path):
    b = build_broker(_settings(), client_factory=lambda: _FakeIB())
    ledger = Ledger(url=f"sqlite:///{tmp_path/'k.db'}")
    ks = KillSwitch(tmp_path / "KILL", clock=lambda: NOW)
    loop = DailyLoop(SimFeed({}), b, ledger, clock=lambda: NOW, kill_switch=ks)

    # Seed fresh snapshots so health would otherwise ALLOW entries.
    class _Snap:
        ts = NOW
    loop._market = _Snap()
    loop._portfolio = _Snap()

    healthy = loop._assess_health(b.get_account())
    assert healthy.entries_allowed is True  # baseline: entries permitted

    ks.engage(reason="integration drill", actor="tester")
    halted = loop._assess_health(b.get_account())
    assert halted.entries_allowed is False  # kill-switch → halted end to end
    assert any(c.name == "kill_switch" for c in halted.checks)
