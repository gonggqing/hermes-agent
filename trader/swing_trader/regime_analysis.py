"""Regime-segmented walk-forward analysis (Loop.md Phase 0.95 pre-live gate).

The walk-forward backtester already reports OOS results over rolling windows.
The go-live gate additionally requires evidence the strategy was validated
across **≥2 distinct market regimes** — not a single-regime artefact. This
module classifies each OOS fold by the regime that prevailed over its TEST
window (from the benchmark's return), re-aggregates the fold's closed trades per
regime, and produces a report the human sign-off can read.

Pure and deterministic: it consumes an already-computed
:class:`~swing_trader.backtest.WalkForwardResult` plus the replay bars; it opens
no ledger, places no orders, and never touches the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from swing_trader.backtest import WalkForwardResult, _aggregate_stats
from swing_trader.interfaces import Bar
from swing_trader.ledger import TradeRecord, TradeStats
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = [
    "BENCHMARK",
    "RegimeReport",
    "RegimeSegment",
    "classify_window_regime",
    "regime_report",
]

#: Default benchmark whose trend defines the regime (matches Backtester._regime).
BENCHMARK = "SPY"

#: Regime labels.
BULL, BEAR, CHOP, UNKNOWN = "bull", "bear", "chop", "unknown"


@dataclass(frozen=True)
class RegimeSegment:
    """Aggregated OOS performance across all folds of one regime."""

    regime: str
    n_folds: int
    stats: TradeStats
    mean_benchmark_return_pct: float


@dataclass(frozen=True)
class RegimeReport:
    segments: list[RegimeSegment] = field(default_factory=list)

    @property
    def regimes_traded(self) -> list[str]:
        """Regimes in which at least one OOS trade actually closed — the honest
        coverage measure (a regime with folds but no trades proves nothing)."""
        return [s.regime for s in self.segments
                if s.regime != UNKNOWN and s.stats.n_closed > 0]

    @property
    def n_regimes_covered(self) -> int:
        return len(self.regimes_traded)

    def profitable_regimes(self) -> list[str]:
        return [s.regime for s in self.segments
                if s.stats.n_closed > 0 and s.stats.expectancy > 0]

    def passes(self, *, min_regimes: int = 2,
               require_all_nonneg: bool = False) -> bool:
        """Coverage gate: traded in ≥ ``min_regimes`` distinct regimes. When
        ``require_all_nonneg`` is set, also require non-negative OOS expectancy
        in EVERY traded regime (a strict 'robust across regimes' bar)."""
        if self.n_regimes_covered < min_regimes:
            return False
        if require_all_nonneg:
            return all(s.stats.expectancy >= 0 for s in self.segments
                       if s.stats.n_closed > 0)
        return True

    def summary(self) -> str:
        parts = [
            f"{s.regime}: {s.stats.n_closed} trades, "
            f"exp {s.stats.expectancy:+.2f}, pnl {s.stats.total_pnl:+.2f} "
            f"(bench {s.mean_benchmark_return_pct:+.1f}%)"
            for s in self.segments if s.regime != UNKNOWN
        ]
        return f"{self.n_regimes_covered} regime(s) traded — " + "; ".join(parts)


def classify_window_regime(bars: list[Bar], *, bull_pct: float = 2.0,
                           bear_pct: float = -2.0) -> str:
    """Classify a benchmark window by its total return: BULL above ``bull_pct``,
    BEAR below ``bear_pct``, else CHOP. Empty/degenerate window → UNKNOWN."""
    closes = [b.close for b in bars if b.close > 0]
    if len(closes) < 2 or closes[0] <= 0:
        return UNKNOWN
    ret_pct = (closes[-1] - closes[0]) / closes[0] * 100.0
    if ret_pct >= bull_pct:
        return BULL
    if ret_pct <= bear_pct:
        return BEAR
    return CHOP


def _window_return_pct(bars: list[Bar]) -> float:
    closes = [b.close for b in bars if b.close > 0]
    if len(closes) < 2 or closes[0] <= 0:
        return 0.0
    return (closes[-1] - closes[0]) / closes[0] * 100.0


def regime_report(
    data: dict[str, list[Bar]],
    wf: WalkForwardResult,
    *,
    benchmark: str = BENCHMARK,
    bull_pct: float = 2.0,
    bear_pct: float = -2.0,
) -> RegimeReport:
    """Bucket walk-forward OOS folds by their test-window regime and aggregate
    the closed OOS trades per regime. Regime order in the report is bull, bear,
    chop, unknown (only those that occur)."""
    bench_bars = data.get(benchmark, [])

    buckets: dict[str, list[TradeRecord]] = {}
    curves: dict[str, list[tuple[datetime, float]]] = {}
    counts: dict[str, int] = {}
    returns: dict[str, list[float]] = {}

    for fold in wf.folds:
        lo, hi = fold.test_window
        window = bench_bars[lo:hi] if bench_bars else []
        regime = classify_window_regime(window, bull_pct=bull_pct, bear_pct=bear_pct)
        buckets.setdefault(regime, []).extend(fold.test_trades)
        curves.setdefault(regime, []).extend(fold.test_equity_curve)
        counts[regime] = counts.get(regime, 0) + 1
        returns.setdefault(regime, []).append(_window_return_pct(window))

    order = [BULL, BEAR, CHOP, UNKNOWN]
    segments: list[RegimeSegment] = []
    for regime in sorted(buckets, key=lambda r: order.index(r) if r in order else 99):
        rets = returns.get(regime, [])
        segments.append(RegimeSegment(
            regime=regime,
            n_folds=counts.get(regime, 0),
            stats=_aggregate_stats(buckets[regime], curves.get(regime, [])),
            mean_benchmark_return_pct=sum(rets) / len(rets) if rets else 0.0,
        ))

    report = RegimeReport(segments=segments)
    logger.info("regime-segmented walk-forward", extra={"summary": report.summary()})
    return report
