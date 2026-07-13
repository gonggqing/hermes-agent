"""Exhaustive branch tests for swing_trader.risk (Loop.md §5.5, §3, §9).

The RiskEngine is the safety core: 100% branch coverage is a hard gate.
Every check is exercised in both directions: every veto, every shrink,
exit paths, breaker by state and by drawdown, hard-cap clamping,
floor-to-zero edges, and copy/no-mutation semantics.
"""

from __future__ import annotations

import dataclasses

import pytest

from swing_trader.constants import (
    DAILY_DRAWDOWN_BREAKER_PCT,
    HARD_MAX_PER_TRADE_RISK_PCT,
)
from swing_trader.risk import (
    LiquidityInfo,
    RiskDecision,
    RiskEngine,
    RiskParams,
    RiskVerdict,
)
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Mode,
    OrderType,
    Position,
    Role,
    Side,
)

# --------------------------------------------------------------------- helpers


def make_candidate(**over) -> CandidateOrder:
    """BUY LMT 10 @ 100, stop 95 — passes every default check untouched."""
    base = dict(
        symbol="NVDA",
        side=Side.BUY,
        qty=10,
        order_type=OrderType.LMT,
        limit=100.0,
        sl=95.0,
        rationale="test entry",
        confidence=0.8,
        pool=Role.ROTATION,
    )
    base.update(over)
    return CandidateOrder(**base)


def make_sell(**over) -> CandidateOrder:
    base = dict(
        symbol="NVDA",
        side=Side.SELL,
        qty=10,
        order_type=OrderType.LMT,
        limit=105.0,
        rationale="test exit",
        confidence=0.8,
        pool=Role.ROTATION,
    )
    base.update(over)
    return CandidateOrder(**base)


def make_account(
    equity: float = 100_000.0,
    cash: float = 100_000.0,
    drawdown_pct: float = 0.0,
    breaker: BreakerState = BreakerState.NORMAL,
) -> AccountSnapshot:
    return AccountSnapshot(
        mode=Mode.PAPER,
        equity=equity,
        cash=cash,
        drawdown_pct=drawdown_pct,
        breaker_state=breaker,
    )


def liq(adv: float = 200_000_000.0, atr_pct: float | None = 3.0) -> LiquidityInfo:
    return LiquidityInfo(avg_dollar_volume=adv, atr_pct=atr_pct)


def evaluate(candidate=None, account=None, positions=None, liquidity=..., *,
             engine: RiskEngine | None = None, entries_today: int = 0,
             system_healthy: bool = True) -> RiskDecision:
    eng = engine if engine is not None else RiskEngine(RiskParams())
    return eng.evaluate(
        candidate if candidate is not None else make_candidate(),
        account if account is not None else make_account(),
        positions if positions is not None else [],
        liq() if liquidity is ... else liquidity,
        entries_today=entries_today,
        system_healthy=system_healthy,
    )


# --------------------------------------------------------------- 1. SELL/exits


class TestExits:
    def test_sell_within_held_approved(self):
        pos = [Position(symbol="NVDA", qty=50, avg_px=90.0, mkt_px=100.0)]
        d = evaluate(make_sell(qty=10), positions=pos)
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0
        assert d.reasons == []
        assert d.candidate.status is CandidateStatus.RISK_APPROVED

    def test_sell_shrunk_to_held_qty(self):
        pos = [Position(symbol="NVDA", qty=5, avg_px=90.0, mkt_px=100.0)]
        d = evaluate(make_sell(qty=10), positions=pos)
        assert d.approved is True
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 5.0
        assert any("held" in r for r in d.reasons)

    def test_sell_sums_held_across_positions(self):
        pos = [
            Position(symbol="NVDA", qty=3, avg_px=90.0, mkt_px=100.0),
            Position(symbol="NVDA", qty=4, avg_px=95.0, mkt_px=100.0),
        ]
        d = evaluate(make_sell(qty=10), positions=pos)
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 7.0

    def test_sell_no_positions_vetoed(self):
        d = evaluate(make_sell(), positions=[])
        assert d.approved is False
        assert d.verdict is RiskVerdict.VETOED
        assert d.final_qty == 0.0
        assert any("no long position" in r for r in d.reasons)

    def test_sell_only_other_symbols_vetoed(self):
        pos = [Position(symbol="AMD", qty=50, avg_px=90.0, mkt_px=100.0)]
        d = evaluate(make_sell(), positions=pos)
        assert d.verdict is RiskVerdict.VETOED
        assert "no shorting" in d.reasons[0]

    def test_sell_short_position_does_not_count_as_held(self):
        pos = [Position(symbol="NVDA", qty=-5, avg_px=90.0, mkt_px=100.0)]
        d = evaluate(make_sell(), positions=pos)
        assert d.verdict is RiskVerdict.VETOED

    def test_sell_allowed_when_breaker_tripped_and_no_liquidity_data(self):
        """Protection must never be blocked (Loop.md §3)."""
        pos = [Position(symbol="NVDA", qty=10, avg_px=90.0, mkt_px=80.0)]
        account = make_account(drawdown_pct=-6.0, breaker=BreakerState.TRIPPED)
        d = evaluate(make_sell(qty=10), account=account, positions=pos, liquidity=None)
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED


# ---------------------------------------------- 1b. dead-man's switch (P0.8)


class TestDeadMansSwitch:
    """Loop.md §5.10: when the system is unhealthy (stale data / loop stall /
    ledger-broker drift) new entries fail closed, but exits are never blocked.
    Both branches of ``system_healthy`` are exercised to hold 100% coverage."""

    def test_unhealthy_vetoes_new_entry(self):
        d = evaluate(make_candidate(), system_healthy=False)
        assert d.approved is False
        assert d.verdict is RiskVerdict.VETOED
        assert d.final_qty == 0.0
        assert "dead-man's switch" in d.reasons[0]
        assert "unhealthy" in d.reasons[0]

    def test_healthy_allows_new_entry(self):
        d = evaluate(make_candidate(), system_healthy=True)
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED

    def test_unhealthy_still_allows_exit(self):
        """Protection is never gated by the dead-man's switch (Loop.md §3)."""
        pos = [Position(symbol="NVDA", qty=10, avg_px=90.0, mkt_px=80.0)]
        d = evaluate(make_sell(qty=10), positions=pos, system_healthy=False)
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0

    def test_switch_precedes_equity_and_breaker_checks(self):
        """The switch fires before other entry checks — its reason wins even
        when equity/breaker would also veto (proves ordering)."""
        account = make_account(equity=0.0, cash=0.0, breaker=BreakerState.TRIPPED)
        d = evaluate(make_candidate(), account=account, system_healthy=False)
        assert d.verdict is RiskVerdict.VETOED
        assert "dead-man's switch" in d.reasons[0]


# ------------------------------------------------------- 2/3. equity & breaker


class TestEquityAndBreaker:
    def test_zero_equity_vetoes_entry(self):
        d = evaluate(account=make_account(equity=0.0, cash=0.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "equity" in d.reasons[0]

    def test_breaker_state_tripped_vetoes_entry(self):
        d = evaluate(account=make_account(breaker=BreakerState.TRIPPED))
        assert d.verdict is RiskVerdict.VETOED
        assert "breaker" in d.reasons[0]

    def test_drawdown_at_threshold_vetoes_entry(self):
        d = evaluate(account=make_account(drawdown_pct=-4.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "breaker" in d.reasons[0]

    def test_drawdown_above_threshold_passes(self):
        d = evaluate(account=make_account(drawdown_pct=-3.9))
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED

    def test_breaker_param_cannot_loosen_hard_cap(self):
        """-10 acts as -4: a -5% drawdown still halts entries (Loop.md §3)."""
        engine = RiskEngine(RiskParams(daily_drawdown_breaker_pct=-10.0))
        d = evaluate(account=make_account(drawdown_pct=-5.0), engine=engine)
        assert d.verdict is RiskVerdict.VETOED

    def test_breaker_param_may_tighten(self):
        engine = RiskEngine(RiskParams(daily_drawdown_breaker_pct=-2.0))
        d = evaluate(account=make_account(drawdown_pct=-3.0), engine=engine)
        assert d.verdict is RiskVerdict.VETOED


# ------------------------------------------------ 4/5. confidence & entry budget


class TestConfidenceAndEntryBudget:
    def test_low_confidence_vetoed(self):
        engine = RiskEngine(RiskParams(min_confidence=0.5))
        d = evaluate(make_candidate(confidence=0.3), engine=engine)
        assert d.verdict is RiskVerdict.VETOED
        assert "confidence" in d.reasons[0]

    def test_confidence_at_minimum_passes(self):
        engine = RiskEngine(RiskParams(min_confidence=0.5))
        d = evaluate(make_candidate(confidence=0.5), engine=engine)
        assert d.verdict is RiskVerdict.APPROVED

    def test_entry_budget_exhausted_vetoed(self):
        d = evaluate(entries_today=3)
        assert d.verdict is RiskVerdict.VETOED
        assert "max new entries" in d.reasons[0]

    def test_entry_budget_remaining_passes(self):
        d = evaluate(entries_today=2)
        assert d.verdict is RiskVerdict.APPROVED


# ----------------------------------------------------- 6. prices & stop geometry


class TestPricesAndStops:
    def test_ref_px_used_when_limit_missing(self):
        cand = make_candidate(order_type=OrderType.MOC, limit=None, ref_px=100.0, qty=500)
        d = evaluate(cand)
        # sized off ref_px=100: 1% of 100k / (100-95) = 200 shares
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 200.0

    def test_no_limit_and_no_ref_px_vetoed(self):
        cand = make_candidate(order_type=OrderType.MOC, limit=None, ref_px=None)
        d = evaluate(cand)
        assert d.verdict is RiskVerdict.VETOED
        assert "cannot size" in d.reasons[0]

    def test_stop_field_preferred_over_sl(self):
        cand = make_candidate(
            order_type=OrderType.BRACKET, limit=100.0, stop=90.0, sl=95.0, qty=500
        )
        d = evaluate(cand)
        # risk/share = 100-90 = 10 (stop, not sl): 1000/10 = 100 shares
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 100.0

    def test_missing_stop_and_sl_vetoed(self):
        # schema forbids this for BUY entries; bypass validation to re-check the guard
        cand = make_candidate().model_copy(update={"sl": None})
        d = evaluate(cand)
        assert d.verdict is RiskVerdict.VETOED
        assert "protective stop" in d.reasons[0]

    def test_stop_at_or_above_entry_vetoed(self):
        d = evaluate(make_candidate(sl=100.0))  # == entry
        assert d.verdict is RiskVerdict.VETOED
        assert "invalid geometry" in d.reasons[0]


# --------------------------------------------------------------- 7. liquidity


class TestLiquidity:
    def test_no_liquidity_data_vetoes_entry(self):
        d = evaluate(liquidity=None)
        assert d.verdict is RiskVerdict.VETOED
        assert "no liquidity data" in d.reasons[0]

    def test_adv_shrink_floors_to_whole_shares(self):
        # cap = 0.005 * 1_010_000 = 5050 -> 50.5 shares -> floor 50
        d = evaluate(make_candidate(qty=60), liquidity=liq(adv=1_010_000.0))
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 50.0
        assert any("avg dollar volume" in r for r in d.reasons)

    def test_adv_shrink_to_zero_vetoed(self):
        # cap = 0.005 * 10_000 = 50 < one share at 100
        d = evaluate(liquidity=liq(adv=10_000.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "whole share" in d.reasons[0]

    def test_notional_exactly_at_adv_cap_not_shrunk(self):
        # cap = 0.005 * 200_000 = 1000 == 10 * 100
        d = evaluate(liquidity=liq(adv=200_000.0))
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0


# --------------------------------------------------------------- 8. volatility


class TestVolatility:
    def test_missing_atr_skips_volatility_checks(self):
        # 0.5% stop distance would fail any noise-band check if ATR were known
        cand = make_candidate(sl=99.5)
        d = evaluate(cand, liquidity=liq(atr_pct=None))
        assert d.verdict is RiskVerdict.APPROVED

    def test_atr_above_max_vetoed(self):
        d = evaluate(liquidity=liq(atr_pct=20.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "ATR" in d.reasons[0]

    def test_atr_exactly_at_max_passes(self):
        # band = 0.5 * 15 = 7.5% ; stop distance = 8%
        d = evaluate(make_candidate(sl=92.0), liquidity=liq(atr_pct=15.0))
        assert d.verdict is RiskVerdict.APPROVED

    def test_stop_inside_noise_band_vetoed(self):
        # band = 0.5 * 8 = 4% ; stop distance = 3%
        d = evaluate(make_candidate(sl=97.0), liquidity=liq(atr_pct=8.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "noise band" in d.reasons[0]

    def test_stop_exactly_at_noise_band_passes(self):
        # band = 0.5 * 10 = 5% ; stop distance = 5%
        d = evaluate(make_candidate(sl=95.0), liquidity=liq(atr_pct=10.0))
        assert d.verdict is RiskVerdict.APPROVED


# ---------------------------------------------------------- 9. per-trade risk


class TestPerTradeRisk:
    def test_oversized_qty_shrunk_to_risk_cap(self):
        d = evaluate(make_candidate(qty=500))
        # 1% of 100k = 1000 ; risk/share 5 -> 200 shares
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 200.0
        assert any("per-trade risk" in r for r in d.reasons)

    def test_loose_param_clamped_to_hard_cap(self):
        """per_trade_risk_pct=5.0 must act as the 1.6% hard cap (Loop.md §3)."""
        engine = RiskEngine(RiskParams(per_trade_risk_pct=5.0))
        d = evaluate(make_candidate(qty=1000, pool=Role.CORE), engine=engine)
        # 1.6% of 100k = 1600 ; risk/share 5 -> 320 shares (5% would allow 1000)
        assert d.final_qty == 320.0

    def test_tighter_param_respected(self):
        engine = RiskEngine(RiskParams(per_trade_risk_pct=0.5))
        d = evaluate(make_candidate(qty=200), engine=engine)
        # 0.5% of 100k = 500 ; risk/share 5 -> 100 shares
        assert d.final_qty == 100.0

    def test_zero_shares_within_risk_budget_vetoed(self):
        # 1% of 100 = 1.0 ; risk/share 5 -> floor(0.2) = 0
        d = evaluate(account=make_account(equity=100.0, cash=100.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "zero shares" in d.reasons[0]


# ------------------------------------------------------------------- 10. cash


class TestCash:
    def test_insufficient_cash_shrinks(self):
        d = evaluate(account=make_account(cash=1_000.0))
        # floor((1000 - 1) / 100) = 9
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 9.0
        assert any("cash" in r for r in d.reasons)

    def test_exact_cash_fit_not_shrunk(self):
        d = evaluate(account=make_account(cash=1_001.0))  # 10*100 + 1 commission
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0

    def test_cannot_afford_one_share_vetoed(self):
        d = evaluate(account=make_account(cash=50.0))
        assert d.verdict is RiskVerdict.VETOED
        assert "cash" in d.reasons[0]

    def test_cash_below_commission_vetoed(self):
        d = evaluate(account=make_account(cash=0.5))  # negative headroom
        assert d.verdict is RiskVerdict.VETOED


# --------------------------------------------------------- 11. role/pool caps


class TestRoleCaps:
    def test_pool_exposure_shrinks_entry(self):
        # rotation cap 20% of 100k = 20k ; existing 19.5k -> headroom 500 -> 5 shares
        pos = [Position(symbol="AMD", qty=100, avg_px=150.0, mkt_px=195.0,
                        pool=Role.ROTATION)]
        d = evaluate(positions=pos)
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 5.0
        assert any("pool cap" in r for r in d.reasons)

    def test_pool_exposure_falls_back_to_avg_px_when_no_mkt_px(self):
        pos = [Position(symbol="AMD", qty=100, avg_px=195.0, mkt_px=None,
                        pool=Role.ROTATION)]
        d = evaluate(positions=pos)
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 5.0

    def test_pool_full_vetoed(self):
        pos = [Position(symbol="AMD", qty=100, avg_px=150.0, mkt_px=200.0,
                        pool=Role.ROTATION)]
        d = evaluate(positions=pos)
        assert d.verdict is RiskVerdict.VETOED
        assert "no headroom" in d.reasons[0]

    def test_other_pool_exposure_ignored(self):
        pos = [Position(symbol="GLD", qty=450, avg_px=200.0, mkt_px=200.0,
                        pool=Role.HEDGE)]
        d = evaluate(positions=pos)
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0

    def test_exactly_at_pool_cap_not_shrunk(self):
        pos = [Position(symbol="AMD", qty=100, avg_px=150.0, mkt_px=190.0,
                        pool=Role.ROTATION)]  # 19k + 1k == 20k cap
        d = evaluate(positions=pos)
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0

    def test_pool_missing_from_role_caps_vetoed(self):
        """No configured allocation for a pool -> conservative zero cap."""
        engine = RiskEngine(RiskParams(role_caps={Role.CORE: 60.0}))
        d = evaluate(make_candidate(pool=Role.ROTATION), engine=engine)
        assert d.verdict is RiskVerdict.VETOED
        assert "no headroom" in d.reasons[0]


# ------------------------------------------------------------ result semantics


class TestResultSemantics:
    def test_clean_approval(self):
        d = evaluate()
        assert d.approved is True
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 10.0
        assert d.reasons == []
        assert d.candidate.status is CandidateStatus.RISK_APPROVED
        assert d.candidate.qty == 10.0
        assert d.candidate.risk_note == "all risk checks passed"

    def test_fractional_qty_untouched_when_no_shrink(self):
        d = evaluate(make_candidate(qty=2.5))
        assert d.verdict is RiskVerdict.APPROVED
        assert d.final_qty == 2.5

    def test_original_candidate_not_mutated_on_shrink(self):
        cand = make_candidate(qty=500)
        d = evaluate(cand)
        assert d.verdict is RiskVerdict.SHRUNK
        assert cand.qty == 500
        assert cand.status is CandidateStatus.PROPOSED
        assert cand.risk_note == ""
        assert d.candidate is not cand
        assert d.candidate.qty == d.final_qty == 200.0
        assert d.candidate.id == cand.id

    def test_original_candidate_not_mutated_on_veto(self):
        cand = make_candidate()
        d = evaluate(cand, liquidity=None)
        assert d.verdict is RiskVerdict.VETOED
        assert cand.qty == 10
        assert cand.status is CandidateStatus.PROPOSED
        assert cand.risk_note == ""
        assert d.candidate.status is CandidateStatus.RISK_VETOED
        assert d.candidate.qty == 0.0
        assert d.reasons[0] in d.candidate.risk_note

    def test_shrinks_accumulate_across_checks(self):
        cand = make_candidate(qty=1000)
        account = make_account(cash=10_000.0)
        d = evaluate(cand, account=account, liquidity=liq(adv=6_000_000.0))
        # ADV: 30k cap -> 300 ; per-trade: -> 200 ; cash: floor(9999/100) -> 99
        assert d.approved is True
        assert d.verdict is RiskVerdict.SHRUNK
        assert d.final_qty == 99.0
        assert len(d.reasons) == 3
        for reason in d.reasons:
            assert reason in d.candidate.risk_note

    def test_first_veto_wins_single_reason(self):
        engine = RiskEngine(RiskParams(min_confidence=0.5))
        account = make_account(
            equity=0.0, cash=0.0, drawdown_pct=-10.0, breaker=BreakerState.TRIPPED
        )
        d = evaluate(
            make_candidate(confidence=0.1),
            account=account,
            liquidity=None,
            engine=engine,
            entries_today=99,
        )
        assert d.verdict is RiskVerdict.VETOED
        assert len(d.reasons) == 1
        assert "equity" in d.reasons[0]


# ------------------------------------------------------------------ parameters


class TestRiskParams:
    def test_defaults(self):
        p = RiskParams()
        assert p.per_trade_risk_pct == 1.0
        assert p.daily_drawdown_breaker_pct == -4.0
        assert p.role_caps == {
            Role.CORE: 60.0,
            Role.CONVICTION: 25.0,
            Role.ROTATION: 20.0,
            Role.HEDGE: 25.0,
        }
        assert p.max_new_entries_per_day == 3
        assert p.max_atr_pct == 15.0
        assert p.min_stop_atr_mult == 0.5
        assert p.max_adv_fraction == 0.005
        assert p.min_confidence == 0.0
        assert p.est_commission == 1.0

    def test_effective_per_trade_clamps_to_hard_cap(self):
        assert (
            RiskParams(per_trade_risk_pct=5.0).effective_per_trade_risk_pct
            == HARD_MAX_PER_TRADE_RISK_PCT
        )
        assert RiskParams(per_trade_risk_pct=0.5).effective_per_trade_risk_pct == 0.5

    def test_effective_breaker_clamps_to_hard_cap(self):
        assert (
            RiskParams(daily_drawdown_breaker_pct=-10.0).effective_breaker_pct
            == DAILY_DRAWDOWN_BREAKER_PCT
        )
        assert RiskParams(daily_drawdown_breaker_pct=-2.0).effective_breaker_pct == -2.0

    def test_params_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            RiskParams().per_trade_risk_pct = 99.0  # type: ignore[misc]

    def test_engine_defaults_and_injection(self):
        assert RiskEngine().params == RiskParams()
        custom = RiskParams(min_confidence=0.9)
        assert RiskEngine(custom).params is custom
