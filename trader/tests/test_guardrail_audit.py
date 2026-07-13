"""End-to-end guardrail audit (Loop.md §3 / Phase 0.95 exit gate).

A single, human-readable place that asserts every safety invariant that stands
between the system and an unintended real-money order. Individual modules test
these in depth; THIS file is the consolidated checklist the human sign-off can
point to — one class per §3 invariant. If any test here fails, do NOT go live.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from swing_trader.api import FinanceRuntime, create_app
from swing_trader.broker_factory import build_broker
from swing_trader.config import BrokerBackend, Settings
from swing_trader.constants import DAILY_DRAWDOWN_BREAKER_PCT, HARD_MAX_PER_TRADE_RISK_PCT
from swing_trader.execution import ExecutionEngine, GuardrailError
from swing_trader.health import assess_health
from swing_trader.ledger import Ledger
from swing_trader.paper_broker import PaperBroker
from swing_trader.portfolio_draft import DraftResultCode, PortfolioDraftService
from swing_trader.portfolio_journal import PortfolioJournal
from swing_trader.risk import RiskEngine, RiskParams
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    Mode,
    OrderType,
    Role,
    Side,
)


def _settings(**over) -> Settings:
    base = dict(broker=BrokerBackend.PAPER, human_confirm=False, dry_run=True)
    base.update(over)
    return Settings(**base)


# ---- Invariant 1: the live-order TRIPLE GATE ----
# No live orders without HUMAN_CONFIRM ∧ BROKER≠paper ∧ ¬DRY_RUN.


class TestTripleGate:
    def test_default_config_blocks_live(self):
        assert _settings().live_orders_allowed is False
        assert _settings().mode is Mode.PAPER

    @pytest.mark.parametrize("over", [
        dict(human_confirm=True, dry_run=False),                    # broker=paper
        dict(broker=BrokerBackend.IBKR, dry_run=False),             # no human_confirm
        dict(broker=BrokerBackend.IBKR, human_confirm=True),        # dry_run=True
    ])
    def test_any_missing_leg_blocks_live(self, over):
        assert _settings(**over).live_orders_allowed is False

    def test_all_three_legs_allow_live(self):
        s = _settings(broker=BrokerBackend.IBKR, human_confirm=True, dry_run=False)
        assert s.live_orders_allowed is True and s.mode is Mode.LIVE


# ---- Invariant 2: ExecutionEngine independently refuses live ----


class TestExecutionGate:
    def test_live_mode_without_permission_raises(self, tmp_path):
        broker = PaperBroker(starting_cash=1000)
        ledger = Ledger(url=f"sqlite:///{tmp_path/'g.db'}")
        eng = ExecutionEngine(broker, ledger, mode=Mode.LIVE, live_orders_allowed=False)
        with pytest.raises(GuardrailError, match="Loop.md"):
            eng.execute([], {}, __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc))


# ---- Invariant 3: broker factory fails closed on a live IBKR port ----


class _FakeIB:
    def is_connected(self): return True
    def connect(self): ...
    def account(self): return {"SettledCash": "0", "NetLiquidation": "0"}
    def positions(self): return []
    def trades(self): return []
    def fills(self): return []
    def place(self, s): return "ib-1"
    def cancel(self, r): return True


class TestFactoryFailClosed:
    def test_ungated_live_port_refused(self):
        s = _settings(broker=BrokerBackend.IBKR, ibkr_port=7496)  # live port, un-gated
        with pytest.raises(ValueError, match="LIVE port"):
            build_broker(s, client_factory=lambda: _FakeIB())

    def test_ungated_ibkr_is_paper_account(self):
        b = build_broker(_settings(broker=BrokerBackend.IBKR),
                         client_factory=lambda: _FakeIB())
        assert b.paper is True and b.mode is Mode.PAPER


# ---- Invariant 4: risk hard caps can only tighten, never loosen ----


class TestRiskCapsImmutable:
    def test_per_trade_cap_enforced(self):
        with pytest.raises(ValueError, match="hard cap"):
            _settings(per_trade_risk_pct=HARD_MAX_PER_TRADE_RISK_PCT + 0.1)

    def test_breaker_cannot_be_loosened(self):
        # "Looser" = trips later = MORE negative than the -4% hard cap.
        with pytest.raises(ValueError, match="hard cap|looser"):
            _settings(daily_drawdown_breaker_pct=DAILY_DRAWDOWN_BREAKER_PCT - 1.0)

    def test_riskparams_clamps_at_use_time(self):
        # Even if constructed loose, the engine re-clamps to the hard cap.
        p = RiskParams(per_trade_risk_pct=5.0)
        assert p.effective_per_trade_risk_pct <= HARD_MAX_PER_TRADE_RISK_PCT


# ---- Invariant 5: dead-man's switch / kill-switch vetoes NEW entries ----


def _account() -> AccountSnapshot:
    return AccountSnapshot(mode=Mode.PAPER, equity=100_000.0, cash=100_000.0,
                           drawdown_pct=0.0, breaker_state=BreakerState.NORMAL)


def _buy() -> CandidateOrder:
    return CandidateOrder(symbol="NVDA", side=Side.BUY, qty=10, order_type=OrderType.LMT,
                          limit=100.0, sl=95.0, rationale="e", confidence=0.8,
                          pool=Role.ROTATION)


class TestKillSwitchVetoesEntries:
    def test_kill_switch_forces_entries_halted(self):
        import datetime as _dt

        class _Snap:
            ts = _dt.datetime(2026, 7, 14, 12, tzinfo=_dt.timezone.utc)

        now = _Snap.ts
        h = assess_health(market=_Snap(), portfolio=_Snap(),
                          kill_switch_engaged=True, now=now)
        assert h.entries_allowed is False

    def test_risk_engine_vetoes_entry_when_unhealthy(self):
        # The authoritative RiskEngine refuses new entries when system_healthy
        # is False — exits still flow (tested elsewhere). Cannot be bypassed.
        eng = RiskEngine(RiskParams())
        d = eng.evaluate(_buy(), _account(), [], None, entries_today=0,
                         system_healthy=False)
        assert d.approved is False
        assert any("halted" in r or "unhealthy" in r for r in d.reasons)


# ---- Invariant 6: the HTTP surface exposes NO order-placement route ----


class TestNoOrderPlacementRoute:
    def test_no_place_order_endpoint(self, tmp_path):
        ledger = Ledger(url=f"sqlite:///{tmp_path/'a.db'}")
        client = TestClient(create_app(FinanceRuntime(ledger=ledger)))
        paths = {r.path for r in client.app.routes}
        placement = {p for p in paths
                     if "order" in p and p not in {"/v1/orders", "/v1/orders/cancel-all"}}
        assert placement == set()
        assert client.post("/v1/orders", json={}).status_code == 405


# ---- Invariant 7: free text can't mutate holdings — human-only confirm ----


class TestHumanOnlyPortfolioWrites:
    @pytest.mark.parametrize("actor", ["system", "llm", "hermes", "agent", "bot"])
    def test_system_actor_confirm_refused_not_human(self, tmp_path, actor):
        # A non-human ACTOR is refused NOT_HUMAN even from a human surface — the
        # gate is checked before draft existence, so it can't be probed away.
        svc = PortfolioDraftService(PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}"))
        res = svc.confirm_draft("any-id", actor=actor, surface="web",
                                idempotency_key="k1")
        assert res.ok is False and res.code is DraftResultCode.NOT_HUMAN

    def test_system_surface_confirm_refused_not_human(self, tmp_path):
        # The SYSTEM surface is refused regardless of actor name.
        svc = PortfolioDraftService(PortfolioJournal(url=f"sqlite:///{tmp_path/'p.db'}"))
        res = svc.confirm_draft("any-id", actor="gongqing", surface="system",
                                idempotency_key="k2")
        assert res.ok is False and res.code is DraftResultCode.NOT_HUMAN
