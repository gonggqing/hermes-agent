"""RiskEngine — the safety core (Loop.md §5.5, §3 guardrails, §9).

Pure code, deterministic, authoritative. No I/O, no randomness, no LLM input.
It can VETO or SHRINK candidate orders; nothing can override it. The hard caps
in :mod:`swing_trader.constants` are re-asserted here at *use time*, so they
cannot be loosened through :class:`RiskParams` (or any config the agent layer
could reach).

Decision sequence (first veto wins; shrinks accumulate; every check that
changes the outcome appends a human-readable reason):

 1. SELL candidates are EXITS: always allowed even when the breaker is
    tripped (protection must never be blocked); qty is shrunk down to the
    currently held qty of that symbol; vetoed if no long position exists
    (no shorting in a cash account).
 2. Non-positive equity -> veto entry.
 3. Daily drawdown breaker (Loop.md §3: −4% halts new entries): breaker
    state TRIPPED or drawdown at/below the effective breaker -> veto entry.
 4. Confidence below the configured minimum -> veto.
 5. Daily new-entry budget exhausted -> veto.
 6. Reference entry price = limit else ref_px (neither -> veto: cannot
    size). Protective stop = stop else sl (missing -> veto; stop >= entry
    for a BUY -> veto: invalid geometry).
 7. Liquidity: no liquidity data -> veto (conservative default: no data,
    no trade). Notional capped at max_adv_fraction of average dollar
    volume -> shrink (floored to whole shares); shrunk to zero -> veto.
 8. Volatility (only when atr_pct known): atr_pct above max -> veto; stop
    distance inside the noise band (< min_stop_atr_mult * atr_pct) -> veto.
 9. Per-trade risk (the 1.6% HARD cap, Loop.md §3): qty capped so that
    (entry − stop) * qty <= effective_per_trade% of equity -> shrink;
    zero shares allowed -> veto.
10. Cash account: final notional + estimated commission must fit in cash
    -> shrink; zero shares affordable -> veto.
11. Role/pool exposure cap (Loop.md §11): current pool exposure plus the
    new notional must fit under the role cap % of equity -> shrink;
    no headroom -> veto.

Result: any shrink applied and qty still > 0 -> SHRUNK (approved); untouched
-> APPROVED; otherwise VETOED with reasons.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from swing_trader.constants import (
    DAILY_DRAWDOWN_BREAKER_PCT,
    HARD_MAX_PER_TRADE_RISK_PCT,
)
from swing_trader.log import get_logger
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    CandidateOrder,
    CandidateStatus,
    Position,
    Role,
    Side,
)

__all__ = [
    "LiquidityInfo",
    "RiskDecision",
    "RiskEngine",
    "RiskParams",
    "RiskVerdict",
]

logger = get_logger(__name__)


@dataclass
class LiquidityInfo:
    """Liquidity/volatility context for a symbol (from monitors/data feed)."""

    avg_dollar_volume: float
    atr_pct: float | None = None  # daily ATR as a percent of price


def _default_role_caps() -> dict[Role, float]:
    """Default per-role exposure caps as percent of equity (Loop.md §11)."""
    return {
        Role.CORE: 60.0,
        Role.CONVICTION: 25.0,
        Role.ROTATION: 20.0,
        Role.HEDGE: 25.0,
    }


@dataclass(frozen=True)
class RiskParams:
    """Tunable risk parameters.

    These may TIGHTEN the hard caps in :mod:`swing_trader.constants`, never
    loosen them: the engine only ever uses the *effective* values below,
    which re-clamp against the constants at use time. This is deliberate —
    RiskParams is plain data that an agent/config layer could conceivably
    construct, so the clamp must live where the value is consumed, not where
    it is declared. A ``per_trade_risk_pct=5.0`` therefore acts as 1.6, and a
    ``daily_drawdown_breaker_pct=-10.0`` acts as −4.0 (Loop.md §3).
    """

    per_trade_risk_pct: float = 1.0
    daily_drawdown_breaker_pct: float = -4.0
    role_caps: Mapping[Role, float] = field(default_factory=_default_role_caps)
    max_new_entries_per_day: int = 3
    max_atr_pct: float = 15.0
    min_stop_atr_mult: float = 0.5
    max_adv_fraction: float = 0.005
    min_confidence: float = 0.0
    est_commission: float = 1.0

    @property
    def effective_per_trade_risk_pct(self) -> float:
        """Per-trade risk %, clamped to the hard cap (can only tighten)."""
        return min(self.per_trade_risk_pct, HARD_MAX_PER_TRADE_RISK_PCT)

    @property
    def effective_breaker_pct(self) -> float:
        """Breaker threshold, clamped to the hard cap (can only tighten)."""
        return max(self.daily_drawdown_breaker_pct, DAILY_DRAWDOWN_BREAKER_PCT)


class RiskVerdict(str, Enum):
    APPROVED = "APPROVED"
    SHRUNK = "SHRUNK"
    VETOED = "VETOED"


@dataclass
class RiskDecision:
    """Outcome of :meth:`RiskEngine.evaluate`.

    ``candidate`` is an UPDATED COPY of the input candidate (qty=final_qty,
    status RISK_APPROVED/RISK_VETOED, risk_note set); the original candidate
    object is never mutated.
    """

    approved: bool
    verdict: RiskVerdict
    final_qty: float
    reasons: list[str]
    candidate: CandidateOrder


class RiskEngine:
    """Deterministic, authoritative order gate (Loop.md §5.5)."""

    def __init__(self, params: RiskParams | None = None) -> None:
        self.params = params if params is not None else RiskParams()

    # ------------------------------------------------------------------ api

    def evaluate(
        self,
        candidate: CandidateOrder,
        account: AccountSnapshot,
        positions: list[Position],
        liquidity: LiquidityInfo | None,
        entries_today: int = 0,
        system_healthy: bool = True,
    ) -> RiskDecision:
        """Run the documented decision sequence over one candidate order.

        ``system_healthy`` is the dead-man's switch (Loop.md §5.10, Phase 0.8):
        when False (stale data / stalled loop / ledger-broker drift) new ENTRIES
        are vetoed; exits are unaffected (protection is never blocked)."""
        params = self.params
        reasons: list[str] = []

        def veto(reason: str) -> RiskDecision:
            reasons.append(reason)
            return self._decision(candidate, RiskVerdict.VETOED, 0.0, reasons)

        # -- 1. SELL = exit: always allowed (even breaker-tripped) ----------
        # Protection must never be blocked; but a cash account cannot short.
        if candidate.side is Side.SELL:
            held = 0.0
            for pos in positions:
                if pos.symbol == candidate.symbol and pos.qty > 0:
                    held += pos.qty
            if held <= 0:
                return veto(
                    f"exit vetoed: no long position in {candidate.symbol} "
                    "(no shorting in a cash account)"
                )
            qty = float(candidate.qty)
            if qty > held:
                reasons.append(
                    f"exit shrunk from {qty:g} to held qty {held:g} "
                    f"(cannot sell more than held)"
                )
                return self._decision(candidate, RiskVerdict.SHRUNK, held, reasons)
            return self._decision(candidate, RiskVerdict.APPROVED, qty, reasons)

        # -- Dead-man's switch (Phase 0.8): halt NEW ENTRIES when the system is
        # unhealthy (stale data / stalled loop / ledger-broker drift). Exits are
        # handled above (never blocked); research-dependent new entries fail
        # closed (Loop.md §5.10, §3). Authoritative + pure — cannot be bypassed.
        if not system_healthy:
            return veto(
                "entry vetoed: system unhealthy — new entries halted "
                "(dead-man's switch, Loop.md §5.10)"
            )

        # -- 2. Equity sanity ------------------------------------------------
        if account.equity <= 0:
            return veto(f"entry vetoed: non-positive equity ({account.equity:.2f})")

        # -- 3. Daily drawdown circuit breaker (Loop.md §3) ------------------
        effective_breaker = params.effective_breaker_pct
        if (
            account.breaker_state is BreakerState.TRIPPED
            or account.drawdown_pct <= effective_breaker
        ):
            return veto(
                "entry vetoed: daily drawdown breaker "
                f"(state={account.breaker_state.value}, "
                f"drawdown={account.drawdown_pct:.2f}% <= {effective_breaker:.2f}% "
                "halts new entries for the day)"
            )

        # -- 4. Confidence floor ---------------------------------------------
        if candidate.confidence < params.min_confidence:
            return veto(
                f"entry vetoed: confidence {candidate.confidence:.2f} below "
                f"minimum {params.min_confidence:.2f}"
            )

        # -- 5. Daily new-entry budget ----------------------------------------
        if entries_today >= params.max_new_entries_per_day:
            return veto(
                f"entry vetoed: max new entries per day reached "
                f"({entries_today}/{params.max_new_entries_per_day})"
            )

        # -- 6. Reference prices & stop geometry -------------------------------
        entry = candidate.limit
        if entry is None:
            entry = candidate.ref_px
        if entry is None:
            return veto("entry vetoed: no limit and no ref_px — cannot size the order")
        stop = candidate.stop
        if stop is None:
            stop = candidate.sl
        if stop is None:
            return veto("entry vetoed: no protective stop (stop/sl missing)")
        if stop >= entry:
            return veto(
                f"entry vetoed: stop {stop:g} >= entry {entry:g} "
                "(invalid geometry for a BUY)"
            )

        qty = float(candidate.qty)
        shrunk = False

        # -- 7. Liquidity: ADV participation cap --------------------------------
        if liquidity is None:
            return veto(
                "entry vetoed: no liquidity data "
                "(conservative default: no data, no trade)"
            )
        max_notional = params.max_adv_fraction * liquidity.avg_dollar_volume
        if qty * entry > max_notional:
            new_qty = float(math.floor(max_notional / entry))
            if new_qty <= 0:
                return veto(
                    f"entry vetoed: notional {qty * entry:.2f} exceeds "
                    f"{params.max_adv_fraction:.3%} of avg dollar volume "
                    f"({max_notional:.2f}) and cannot be shrunk to a whole share"
                )
            reasons.append(
                f"shrunk {qty:g} -> {new_qty:g} to stay within "
                f"{params.max_adv_fraction:.3%} of avg dollar volume"
            )
            qty = new_qty
            shrunk = True

        # -- 8. Volatility checks (skipped when ATR unknown) ---------------------
        if liquidity.atr_pct is not None:
            if liquidity.atr_pct > params.max_atr_pct:
                return veto(
                    f"entry vetoed: ATR {liquidity.atr_pct:.2f}% exceeds "
                    f"max {params.max_atr_pct:.2f}% (too volatile)"
                )
            stop_distance_pct = (entry - stop) / entry * 100.0
            noise_band_pct = params.min_stop_atr_mult * liquidity.atr_pct
            if stop_distance_pct < noise_band_pct:
                return veto(
                    f"entry vetoed: stop inside noise band — stop distance "
                    f"{stop_distance_pct:.2f}% < {params.min_stop_atr_mult:g} x "
                    f"ATR ({noise_band_pct:.2f}%)"
                )

        # -- 9. Per-trade risk (the 1.6% HARD cap, re-clamped at use time) --------
        effective_per_trade = params.effective_per_trade_risk_pct
        risk_per_share = entry - stop  # > 0 (guaranteed by step 6)
        max_risk_dollars = effective_per_trade / 100.0 * account.equity
        allowed = math.floor(max_risk_dollars / risk_per_share)
        if allowed <= 0:
            return veto(
                f"entry vetoed: per-trade risk cap {effective_per_trade:.2f}% of "
                f"equity ({max_risk_dollars:.2f}) allows zero shares at "
                f"{risk_per_share:.2f} risk/share"
            )
        if qty > allowed:
            reasons.append(
                f"shrunk {qty:g} -> {allowed:g} to respect per-trade risk cap "
                f"{effective_per_trade:.2f}% of equity"
            )
            qty = float(allowed)
            shrunk = True

        # -- 10. Cash account: must afford notional + commission -------------------
        if qty * entry + params.est_commission > account.cash:
            new_qty = float(math.floor((account.cash - params.est_commission) / entry))
            if new_qty <= 0:
                return veto(
                    f"entry vetoed: cash {account.cash:.2f} cannot cover a single "
                    f"share at {entry:g} plus commission {params.est_commission:.2f}"
                )
            reasons.append(
                f"shrunk {qty:g} -> {new_qty:g} to fit available cash "
                f"{account.cash:.2f} (incl. est. commission {params.est_commission:.2f})"
            )
            qty = new_qty
            shrunk = True

        # -- 11. Role/pool exposure cap (Loop.md §11) --------------------------------
        exposure = 0.0
        for pos in positions:
            if pos.pool is not candidate.pool:
                continue
            value = pos.market_value
            if value is None:  # no market price yet -> fall back to cost basis
                value = pos.avg_px * pos.qty
            exposure += value
        role_cap_pct = float(params.role_caps.get(candidate.pool, 0.0))
        cap_dollars = role_cap_pct / 100.0 * account.equity
        if exposure + qty * entry > cap_dollars:
            new_qty = float(math.floor((cap_dollars - exposure) / entry))
            if new_qty <= 0:
                return veto(
                    f"entry vetoed: {candidate.pool.value} pool exposure "
                    f"{exposure:.2f} leaves no headroom under its "
                    f"{role_cap_pct:.1f}% cap ({cap_dollars:.2f})"
                )
            reasons.append(
                f"shrunk {qty:g} -> {new_qty:g} to fit {candidate.pool.value} "
                f"pool cap {role_cap_pct:.1f}% of equity ({cap_dollars:.2f}, "
                f"current exposure {exposure:.2f})"
            )
            qty = new_qty
            shrunk = True

        verdict = RiskVerdict.SHRUNK if shrunk else RiskVerdict.APPROVED
        return self._decision(candidate, verdict, qty, reasons)

    # -------------------------------------------------------------- internals

    @staticmethod
    def _decision(
        candidate: CandidateOrder,
        verdict: RiskVerdict,
        final_qty: float,
        reasons: list[str],
    ) -> RiskDecision:
        """Build the decision + updated candidate copy (input never mutated)."""
        approved = verdict is not RiskVerdict.VETOED
        status = CandidateStatus.RISK_APPROVED if approved else CandidateStatus.RISK_VETOED
        note = "; ".join(reasons) if reasons else "all risk checks passed"
        updated = candidate.model_copy(
            update={"qty": final_qty, "status": status, "risk_note": note}
        )
        logger.info(
            "risk decision",
            extra={
                "symbol": candidate.symbol,
                "side": candidate.side.value,
                "verdict": verdict.value,
                "final_qty": final_qty,
                "risk_note": note,
            },
        )
        return RiskDecision(
            approved=approved,
            verdict=verdict,
            final_qty=final_qty,
            reasons=list(reasons),
            candidate=updated,
        )
