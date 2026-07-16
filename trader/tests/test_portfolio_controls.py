from __future__ import annotations

import pytest

from swing_trader.portfolio_controls import PortfolioControlStore, PortfolioControls


def test_default_controls_preserve_cash_and_bound_agent_window(tmp_path):
    store = PortfolioControlStore(f"sqlite:///{tmp_path / 'controls.db'}")
    controls = store.get()
    assert controls.invested_target_pct == 60.0
    assert controls.invested_ceiling_pct == 65.0
    assert controls.cash_reserve_floor_pct == 35.0
    assert controls.agent_budget_pct == 30.0
    assert controls.agent_ceiling_pct == 35.0
    assert controls.max_position_pct == 12.0
    assert controls.per_trade_risk_pct == 1.0
    assert controls.max_new_positions_per_day == 3


def test_controls_persist_and_reject_incoherent_allocations(tmp_path):
    url = f"sqlite:///{tmp_path / 'controls.db'}"
    store = PortfolioControlStore(url)
    updated = store.update({"invested_target_pct": 55.0, "agent_budget_pct": 25.0})
    assert updated.invested_ceiling_pct == 60.0
    assert PortfolioControlStore(url).get().agent_budget_pct == 25.0

    with pytest.raises(ValueError, match="agent budget"):
        PortfolioControls(
            invested_target_pct=20,
            invested_tolerance_pct=0,
            agent_budget_pct=30,
            agent_budget_tolerance_pct=0,
        )
    with pytest.raises(ValueError, match="preserve at least 5% cash"):
        PortfolioControls(invested_target_pct=95, invested_tolerance_pct=1)
