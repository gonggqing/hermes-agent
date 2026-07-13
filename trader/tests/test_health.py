"""Tests for swing_trader.health — the Phase 0.8 dead-man's switch model.

``assess_health`` is pure/deterministic given ``now`` and must never raise.
We exercise every source (market/portfolio/news), reconciliation, breaker, the
level-ranking and — critically — the ``entries_allowed`` gate that halts new
entries when the loop can no longer be trusted (Loop.md §5.10).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swing_trader.health import (
    STALE_AFTER_MINUTES,
    HealthLevel,
    assess_health,
)
from swing_trader.reconcile import PositionMismatch, ReconciliationResult
from swing_trader.schemas import BreakerState

NOW = datetime(2026, 7, 13, 14, 30, tzinfo=timezone.utc)


class _Snap:
    """Minimal monitor-snapshot stand-in — only ``.ts`` is read."""

    def __init__(self, ts):
        self.ts = ts


def fresh(minutes: float = 1.0) -> _Snap:
    return _Snap(NOW - timedelta(minutes=minutes))


def stale() -> _Snap:
    return _Snap(NOW - timedelta(minutes=STALE_AFTER_MINUTES + 1))


def ok_recon() -> ReconciliationResult:
    return ReconciliationResult(ok=True, mismatches=[], n_symbols=3)


def drifted_recon() -> ReconciliationResult:
    return ReconciliationResult(
        ok=False,
        mismatches=[PositionMismatch("NVDA", 10.0, 8.0)],
        n_symbols=1,
    )


def _check(status, name):
    return next(c for c in status.checks if c.name == name)


# ------------------------------------------------------------------ happy path


class TestHealthy:
    def test_all_fresh_and_reconciled_is_ok_and_allows_entries(self):
        h = assess_health(
            market=fresh(), portfolio=fresh(), news=fresh(),
            breaker_state=BreakerState.NORMAL, reconciliation=ok_recon(), now=NOW,
        )
        assert h.level is HealthLevel.OK
        assert h.entries_allowed is True
        assert h.warnings == []
        assert _check(h, "market").level is HealthLevel.OK
        assert _check(h, "reconciliation").level is HealthLevel.OK
        assert _check(h, "breaker").level is HealthLevel.OK
        assert h.as_of == NOW


# ----------------------------------------------------- freshness / dead-man's


class TestFreshnessGate:
    def test_stale_market_halts_entries_and_is_unhealthy(self):
        h = assess_health(market=stale(), portfolio=fresh(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is False
        assert h.level is HealthLevel.UNHEALTHY
        assert _check(h, "market").level is HealthLevel.UNHEALTHY
        assert any("market" in w for w in h.warnings)

    def test_missing_market_snapshot_halts_entries(self):
        h = assess_health(market=None, portfolio=fresh(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is False
        assert h.level is HealthLevel.UNHEALTHY
        assert "missing" in _check(h, "market").detail

    def test_stale_portfolio_halts_entries(self):
        h = assess_health(market=fresh(), portfolio=stale(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is False
        assert _check(h, "portfolio").level is HealthLevel.UNHEALTHY

    def test_stale_news_is_degraded_but_still_allows_entries(self):
        """News staleness never blocks entries (only informs, Loop.md §5.10)."""
        h = assess_health(market=fresh(), portfolio=fresh(), news=stale(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is True
        assert h.level is HealthLevel.DEGRADED
        assert _check(h, "news").level is HealthLevel.DEGRADED

    def test_boundary_exactly_stale_after_is_still_fresh(self):
        """age == STALE_AFTER is not yet stale (strict > threshold)."""
        snap = _Snap(NOW - timedelta(minutes=STALE_AFTER_MINUTES))
        h = assess_health(market=snap, portfolio=fresh(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is True
        assert _check(h, "market").level is HealthLevel.OK

    def test_future_timestamp_clamped_to_zero_age(self):
        snap = _Snap(NOW + timedelta(minutes=5))
        h = assess_health(market=snap, portfolio=fresh(),
                          reconciliation=ok_recon(), now=NOW)
        assert _check(h, "market").level is HealthLevel.OK

    def test_naive_timestamp_treated_as_utc(self):
        naive = _Snap(NOW.replace(tzinfo=None) - timedelta(minutes=5))
        h = assess_health(market=naive, portfolio=fresh(),
                          reconciliation=ok_recon(), now=NOW)
        assert _check(h, "market").level is HealthLevel.OK


# --------------------------------------------------------------- reconciliation


class TestReconciliation:
    def test_drift_halts_entries_and_is_unhealthy(self):
        h = assess_health(market=fresh(), portfolio=fresh(),
                          reconciliation=drifted_recon(), now=NOW)
        assert h.entries_allowed is False
        assert h.level is HealthLevel.UNHEALTHY
        assert _check(h, "reconciliation").level is HealthLevel.UNHEALTHY
        assert any("drift" in w for w in h.warnings)

    def test_missing_reconciliation_is_degraded_but_allows_entries(self):
        h = assess_health(market=fresh(), portfolio=fresh(),
                          reconciliation=None, now=NOW)
        assert h.entries_allowed is True
        assert h.level is HealthLevel.DEGRADED
        assert _check(h, "reconciliation").level is HealthLevel.DEGRADED


# ---------------------------------------------------------------------- breaker


class TestBreaker:
    def test_tripped_breaker_is_degraded_but_switch_allows_entries(self):
        """The breaker is enforced in the RiskEngine, not the switch — health
        only reports it (so it does not double-veto), Loop.md §5.10."""
        h = assess_health(market=fresh(), portfolio=fresh(),
                          breaker_state=BreakerState.TRIPPED,
                          reconciliation=ok_recon(), now=NOW)
        assert h.entries_allowed is True
        assert h.level is HealthLevel.DEGRADED
        assert _check(h, "breaker").level is HealthLevel.DEGRADED
        assert any("breaker" in w for w in h.warnings)

    def test_no_breaker_state_omits_check(self):
        h = assess_health(market=fresh(), portfolio=fresh(),
                          breaker_state=None, reconciliation=ok_recon(), now=NOW)
        assert all(c.name != "breaker" for c in h.checks)


# ----------------------------------------------------------------- level rank


class TestLevelRanking:
    def test_worst_check_sets_overall_level(self):
        # portfolio missing (unhealthy) dominates a merely-degraded news source.
        h = assess_health(market=fresh(), portfolio=None, news=stale(),
                          reconciliation=ok_recon(), now=NOW)
        assert h.level is HealthLevel.UNHEALTHY

    def test_default_now_when_omitted_does_not_raise(self):
        # now=None takes the wall clock; just assert it produces a status.
        h = assess_health(market=fresh(), portfolio=fresh(),
                          reconciliation=ok_recon())
        assert h.level in (HealthLevel.OK, HealthLevel.DEGRADED, HealthLevel.UNHEALTHY)
