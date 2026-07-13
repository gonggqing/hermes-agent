"""System health / heartbeat model + dead-man's switch (Loop.md §5.10, Phase 0.8).

Assesses whether the loop can be TRUSTED right now, from a few cheap signals —
data freshness (market/portfolio/news snapshot age), ledger↔broker
reconciliation, and the drawdown breaker — and produces:

- an overall :class:`HealthLevel` (ok / degraded / unhealthy) with per-check
  reasons the operator can read at a glance (reporter bot + Finance tab), and
- ``entries_allowed`` — the DEAD-MAN'S SWITCH: False when the data the decision
  depends on is stale/missing or the ledger and broker disagree, so
  research-dependent NEW entries fail closed (Loop.md §5.10). Exits are never
  gated here (protection is handled by the RiskEngine's SELL path).

Pure and deterministic given its inputs (``now`` injectable); never raises.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field

from swing_trader.log import get_logger
from swing_trader.reconcile import ReconciliationResult
from swing_trader.schemas import BreakerState

logger = get_logger(__name__)

__all__ = ["HealthCheck", "HealthLevel", "HealthStatus", "STALE_AFTER_MINUTES", "assess_health"]

#: Snapshot older than this is stale — new entries stop depending on it.
STALE_AFTER_MINUTES: float = 120.0


class HealthLevel(str, Enum):
    OK = "ok"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


_RANK = {HealthLevel.OK: 0, HealthLevel.DEGRADED: 1, HealthLevel.UNHEALTHY: 2}


class HealthCheck(BaseModel):
    name: str
    level: HealthLevel
    detail: str = ""


class HealthStatus(BaseModel):
    level: HealthLevel
    as_of: datetime
    #: Dead-man's switch: may the loop open NEW entries right now?
    entries_allowed: bool
    checks: list[HealthCheck] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _age_minutes(ts: Optional[datetime], now: datetime) -> Optional[float]:
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return max(0.0, (now - ts).total_seconds() / 60.0)


def assess_health(
    *,
    market=None,
    portfolio=None,
    news=None,
    breaker_state: Optional[BreakerState] = None,
    reconciliation: Optional[ReconciliationResult] = None,
    now: Optional[datetime] = None,
) -> HealthStatus:
    """Build the current :class:`HealthStatus`. Snapshots are the monitor
    outputs (each carries ``.ts``); any may be None. Never raises."""
    now = now or datetime.now(timezone.utc)
    checks: list[HealthCheck] = []
    warnings: list[str] = []

    def data_check(name: str, snap, *, critical: bool) -> bool:
        """True when the source is FRESH. ``critical`` sources gate entries."""
        age = _age_minutes(getattr(snap, "ts", None), now)
        bad = HealthLevel.UNHEALTHY if critical else HealthLevel.DEGRADED
        if snap is None or age is None:
            checks.append(HealthCheck(name=name, level=bad,
                                      detail=f"{name} data missing — monitor has not produced a snapshot"))
            warnings.append(f"{name} data missing")
            return False
        if age > STALE_AFTER_MINUTES:
            checks.append(HealthCheck(name=name, level=bad,
                                      detail=f"{name} data is {age:.0f} min old (stale after {STALE_AFTER_MINUTES:.0f})"))
            warnings.append(f"{name} data stale ({age:.0f} min)")
            return False
        checks.append(HealthCheck(name=name, level=HealthLevel.OK,
                                  detail=f"{name} data {age:.0f} min old"))
        return True

    market_fresh = data_check("market", market, critical=True)
    portfolio_fresh = data_check("portfolio", portfolio, critical=True)
    data_check("news", news, critical=False)  # news staleness never blocks entries

    recon_ok = True
    if reconciliation is None:
        checks.append(HealthCheck(name="reconciliation", level=HealthLevel.DEGRADED,
                                  detail="ledger/broker reconciliation not run this cycle"))
    elif not reconciliation.ok:
        recon_ok = False
        checks.append(HealthCheck(name="reconciliation", level=HealthLevel.UNHEALTHY,
                                  detail=f"ledger/broker DRIFT: {reconciliation.summary()}"))
        warnings.append(f"ledger/broker drift — {reconciliation.summary()}")
    else:
        checks.append(HealthCheck(name="reconciliation", level=HealthLevel.OK,
                                  detail="ledger matches broker"))

    if breaker_state is BreakerState.TRIPPED:
        checks.append(HealthCheck(name="breaker", level=HealthLevel.DEGRADED,
                                  detail="drawdown breaker TRIPPED — no new entries today"))
        warnings.append("daily drawdown breaker tripped")
    elif breaker_state is not None:
        checks.append(HealthCheck(name="breaker", level=HealthLevel.OK,
                                  detail=f"breaker {breaker_state.value}"))

    # Dead-man's switch: new entries need FRESH critical data + no ledger drift.
    # (The breaker is enforced separately in the RiskEngine, so it is not
    # duplicated here.)
    entries_allowed = market_fresh and portfolio_fresh and recon_ok

    level = HealthLevel.OK
    for c in checks:
        if _RANK[c.level] > _RANK[level]:
            level = c.level

    return HealthStatus(level=level, as_of=now, entries_allowed=entries_allowed,
                        checks=checks, warnings=warnings)
