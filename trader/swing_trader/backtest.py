"""Backtest harness (Loop.md §9, backlog 16).

Reuses the SAME signal + risk + execution code paths as the live daily loop:
TechnicalAgent → DebateAgent → RuleBasedDecisionCore → RiskEngine →
ExecutionEngine → PaperBroker (slippage + commission modeled) → Ledger.

Walk-forward, out-of-sample only: parameters are selected on a TRAIN window
and results are reported ONLY from the following TEST window.

APPROVAL BYPASS — READ THIS: the human confirmation step is auto-approved
here and ONLY here. The backtester is hard-wired to ``Mode.PAPER`` and an
isolated Ledger; it has no code path to a live broker. Live/production flow
always requires explicit human confirmation (Loop.md §3).

No look-ahead: orders decided on day *i* fill against day *i+1*'s bar, which
is why ``end_index`` must stay strictly below ``len(bars) - 1``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from swing_trader import watchlist
from swing_trader.analysis import DebateAgent, TechnicalAgent
from swing_trader.datafeed import DataFeedError
from swing_trader.decision import DecisionParams, RuleBasedDecisionCore, SymbolView
from swing_trader.execution import ExecutionEngine
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.ledger import Ledger, TradeRecord, TradeStats
from swing_trader.log import get_logger
from swing_trader.paper_broker import PaperBroker
from swing_trader.risk import LiquidityInfo, RiskEngine, RiskParams
from swing_trader.schemas import CandidateStatus, Mode, Role, Side

logger = get_logger(__name__)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "Backtester",
    "ReplayFeed",
    "WalkForwardBacktester",
    "WalkForwardConfig",
    "WalkForwardResult",
]


# ------------------------------------------------------------------ replay feed


class ReplayFeed(DataFeed):
    """Serves recorded daily bars with an as-of cursor (no look-ahead)."""

    def __init__(self, data: dict[str, list[Bar]]) -> None:
        if not data:
            raise ValueError("ReplayFeed needs at least one symbol")
        self._data = {sym.upper(): list(bars) for sym, bars in data.items()}
        self._today = 0

    def set_today(self, index: int) -> None:
        if index < 0:
            raise ValueError("today index must be >= 0")
        self._today = index

    @property
    def today(self) -> int:
        return self._today

    def _bars(self, symbol: str) -> list[Bar]:
        bars = self._data.get(symbol.upper())
        if not bars:
            raise DataFeedError(f"no replay data for {symbol}")
        return bars

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100) -> list[Bar]:
        if timeframe != "1d":
            raise ValueError("ReplayFeed only supports the 1d timeframe")
        if limit < 1:
            raise ValueError("limit must be >= 1")
        bars = self._bars(symbol)
        visible = bars[: self._today + 1]
        if not visible:
            raise DataFeedError(f"no bars visible for {symbol} at index {self._today}")
        return visible[-limit:]

    def get_quote(self, symbol: str) -> Quote:
        bars = self._bars(symbol)
        if self._today >= len(bars):
            raise DataFeedError(f"replay cursor past end of {symbol}")
        bar = bars[self._today]
        return Quote(symbol=symbol.upper(), ts=bar.ts, last=bar.close,
                     volume=bar.volume)

    def get_news(self, symbol: Optional[str] = None, limit: int = 20) -> list[NewsItem]:
        return []


# ------------------------------------------------------------------ configs


@dataclass(frozen=True)
class BacktestConfig:
    starting_cash: float = 10_000.0
    commission_per_order: float = 1.0
    slippage_bps: float = 5.0
    price_tolerance_pct: float = 1.5
    decision_params: Optional[DecisionParams] = None
    risk_params: Optional[RiskParams] = None
    min_warmup_bars: int = 60


@dataclass
class BacktestResult:
    equity_curve: list[tuple[datetime, float]]
    n_days: int
    stats: TradeStats
    trades: list[TradeRecord]
    final_equity: float


# ------------------------------------------------------------------ indicators


def _atr_pct(bars: list[Bar], period: int = 14) -> Optional[float]:
    """Same true-range-mean formula the monitors use (Loop.md §5.2)."""
    if len(bars) < 2:
        return None
    window = bars[-(period + 1):]
    trs: list[float] = []
    for prev, cur in zip(window, window[1:]):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close),
                 abs(cur.low - prev.close))
        trs.append(tr)
    if not trs:
        return None
    last_close = bars[-1].close
    if last_close <= 0:
        return None
    return sum(trs) / len(trs) / last_close * 100.0


def _adv(bars: list[Bar], period: int = 20) -> float:
    window = bars[-period:]
    if not window:
        return 0.0
    return sum(b.close * b.volume for b in window) / len(window)


def _sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


# ------------------------------------------------------------------ backtester


class Backtester:
    """One pass over [start_index, end_index) — the daily loop, auto-approved."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(
        self,
        data: dict[str, list[Bar]],
        start_index: int,
        end_index: int,
        ledger: Ledger,
    ) -> BacktestResult:
        cfg = self.config
        n_bars = min(len(b) for b in data.values())
        if not start_index < end_index <= n_bars - 1:
            raise ValueError(
                "need start_index < end_index <= len(bars) - 1 "
                "(orders fill on the NEXT bar; no look-ahead)"
            )

        feed = ReplayFeed(data)
        broker = PaperBroker(
            starting_cash=cfg.starting_cash,
            commission_per_order=cfg.commission_per_order,
            slippage_bps=cfg.slippage_bps,
        )
        decision = RuleBasedDecisionCore(
            params=cfg.decision_params, risk_params=cfg.risk_params
        )
        risk = RiskEngine(cfg.risk_params)
        execution = ExecutionEngine(
            broker, ledger, mode=Mode.PAPER,
            price_tolerance_pct=cfg.price_tolerance_pct,
        )
        tech = TechnicalAgent()
        debate = DebateAgent()

        equity_curve: list[tuple[datetime, float]] = []

        for i in range(start_index, end_index):
            feed.set_today(i)
            broker.start_of_day()
            entries_today = 0

            # --- signals (same agents as the live loop) -------------------
            debates = []
            views: dict[str, SymbolView] = {}
            for symbol in data:
                sym = symbol.upper()
                try:
                    bars = feed.get_bars(sym, "1d", limit=120)
                except DataFeedError:
                    continue
                tech_sig = tech.analyze(sym, bars)
                if tech_sig is None:
                    continue
                ledger.record_signal(tech_sig, Mode.PAPER)
                verdict = debate.debate(sym, [tech_sig])
                ledger.record_signal(verdict, Mode.PAPER)
                debates.append(verdict)
                item = watchlist.get(sym)
                views[sym] = SymbolView(
                    symbol=sym,
                    last=bars[-1].close,
                    atr_pct=_atr_pct(bars),
                    pool=item.role if item is not None else Role.ROTATION,
                )

            regime = self._regime(feed)
            account = broker.get_account()
            positions = [
                pos.model_copy(update={
                    "pool": (watchlist.get(pos.symbol).role
                             if watchlist.get(pos.symbol) else Role.ROTATION)
                })
                for pos in broker.get_positions()
            ]
            open_syms = {
                o.symbol for o in broker.get_orders(active_only=True)
            }

            candidates = decision.propose(
                debates, views, account, positions,
                risk_on_off=regime, open_order_symbols=open_syms,
            )

            # --- risk gate (authoritative) + AUTO-APPROVE (backtest only) --
            approved = []
            for cand in candidates:
                ledger.record_candidate(cand, Mode.PAPER)
                liquidity = None
                if cand.symbol in data or cand.symbol.upper() in data:
                    bars = feed.get_bars(cand.symbol, "1d", limit=30)
                    liquidity = LiquidityInfo(
                        avg_dollar_volume=_adv(bars), atr_pct=_atr_pct(bars)
                    )
                decision_out = risk.evaluate(
                    cand, account, positions, liquidity,
                    entries_today=entries_today,
                )
                ledger.update_candidate(
                    cand.id, decision_out.candidate.status,
                    risk_note=decision_out.candidate.risk_note,
                )
                if not decision_out.approved:
                    continue
                if decision_out.candidate.side is Side.BUY:
                    entries_today += 1
                approved.append(decision_out.candidate.model_copy(
                    update={"status": CandidateStatus.APPROVED}  # backtest-only bypass
                ))

            quotes = {sym: feed.get_quote(sym).last for sym in views}
            now = list(data.values())[0][i].ts
            execution.execute(approved, quotes, now)

            # --- next-day fills (no look-ahead) ---------------------------
            next_bars = {
                sym.upper(): bars[i + 1]
                for sym, bars in data.items()
                if i + 1 < len(bars)
            }
            broker.step(next_bars)
            execution.sync_fills()
            broker.end_of_day()
            snap = broker.get_account()
            ledger.record_snapshot(snap)
            equity_curve.append((next_bars[list(next_bars)[0]].ts, snap.equity))

        stats = ledger.stats(Mode.PAPER)
        trades = ledger.get_trades(Mode.PAPER)
        return BacktestResult(
            equity_curve=equity_curve,
            n_days=end_index - start_index,
            stats=stats,
            trades=trades,
            final_equity=equity_curve[-1][1] if equity_curve else cfg.starting_cash,
        )

    @staticmethod
    def _regime(feed: ReplayFeed) -> str:
        """Simplified regime: risk_off when SPY < its 50dma (else neutral).

        The live loop uses the richer MarketMonitor rules (VIX etc.); replay
        data usually lacks VIX, so this deliberately conservative subset is
        documented as a Phase-0 simplification.
        """
        try:
            bars = feed.get_bars("SPY", "1d", limit=60)
        except (DataFeedError, ValueError):
            return "neutral"
        closes = [b.close for b in bars]
        sma50 = _sma(closes, 50)
        if sma50 is not None and closes[-1] < sma50:
            return "risk_off"
        return "neutral"


# ------------------------------------------------------------------ walk-forward


@dataclass(frozen=True)
class WalkForwardConfig:
    train_days: int = 60
    test_days: int = 20
    param_grid: tuple[DecisionParams, ...] = (DecisionParams(),)


@dataclass
class WalkForwardFold:
    fold_index: int
    chosen_params_index: int
    train_stats: TradeStats
    test_stats: TradeStats
    test_equity_curve: list[tuple[datetime, float]]
    #: OOS test window as [lo, hi) bar indices into the replay data — lets a
    #: regime classifier locate the benchmark bars for this fold (Phase 0.95).
    test_window: tuple[int, int] = (0, 0)
    #: The CLOSED OOS trades from this fold (for per-regime re-aggregation).
    test_trades: list[TradeRecord] = field(default_factory=list)


@dataclass
class WalkForwardResult:
    """ONLY ``combined_test_stats`` matters — train stats exist purely for
    parameter selection (in-sample numbers are always flattering)."""

    folds: list[WalkForwardFold]
    combined_test_stats: TradeStats


def _score(stats: TradeStats) -> tuple[float, float]:
    """Rank param sets: expectancy first, total pnl as tie-break. Zero-trade
    runs rank below any run that actually traded."""
    if stats.n_closed == 0:
        return (-math.inf, stats.total_pnl)
    return (stats.expectancy, stats.total_pnl)


class WalkForwardBacktester:
    def __init__(
        self,
        config: BacktestConfig | None = None,
        wf: WalkForwardConfig | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.wf = wf or WalkForwardConfig()

    def run(
        self,
        data: dict[str, list[Bar]],
        ledger_factory: Callable[[], Ledger],
    ) -> WalkForwardResult:
        cfg, wf = self.config, self.wf
        usable = min(len(b) for b in data.values()) - 1  # last bar only fills
        folds: list[WalkForwardFold] = []
        all_test_trades: list[TradeRecord] = []
        combined_curve: list[tuple[datetime, float]] = []

        fold_start = cfg.min_warmup_bars
        fold_index = 0
        while fold_start + wf.train_days + wf.test_days <= usable:
            train_lo = fold_start
            train_hi = fold_start + wf.train_days
            test_hi = train_hi + wf.test_days

            # -- select params on TRAIN ------------------------------------
            best_idx, best_score, best_train_stats = 0, None, None
            for p_idx, params in enumerate(wf.param_grid):
                bt = Backtester(BacktestConfig(
                    starting_cash=cfg.starting_cash,
                    commission_per_order=cfg.commission_per_order,
                    slippage_bps=cfg.slippage_bps,
                    price_tolerance_pct=cfg.price_tolerance_pct,
                    decision_params=params,
                    risk_params=cfg.risk_params,
                    min_warmup_bars=cfg.min_warmup_bars,
                ))
                result = bt.run(data, train_lo, train_hi, ledger_factory())
                score = _score(result.stats)
                if best_score is None or score > best_score:
                    best_idx, best_score, best_train_stats = p_idx, score, result.stats

            # -- evaluate chosen params OOS on TEST ------------------------
            chosen = Backtester(BacktestConfig(
                starting_cash=cfg.starting_cash,
                commission_per_order=cfg.commission_per_order,
                slippage_bps=cfg.slippage_bps,
                price_tolerance_pct=cfg.price_tolerance_pct,
                decision_params=wf.param_grid[best_idx],
                risk_params=cfg.risk_params,
                min_warmup_bars=cfg.min_warmup_bars,
            ))
            test_ledger = ledger_factory()
            test_result = chosen.run(data, train_hi, test_hi, test_ledger)
            fold_closed = [t for t in test_result.trades if not t.is_open]
            folds.append(WalkForwardFold(
                fold_index=fold_index,
                chosen_params_index=best_idx,
                train_stats=best_train_stats,
                test_stats=test_result.stats,
                test_equity_curve=test_result.equity_curve,
                test_window=(train_hi, test_hi),
                test_trades=fold_closed,
            ))
            all_test_trades.extend(fold_closed)
            combined_curve.extend(test_result.equity_curve)
            logger.info(
                "walk-forward fold complete",
                extra={"fold": fold_index, "chosen_params": best_idx,
                       "test_expectancy": test_result.stats.expectancy},
            )
            fold_start += wf.test_days
            fold_index += 1

        combined = _aggregate_stats(all_test_trades, combined_curve)
        return WalkForwardResult(folds=folds, combined_test_stats=combined)


def _aggregate_stats(
    trades: list[TradeRecord],
    curve: list[tuple[datetime, float]],
) -> TradeStats:
    """Aggregate CLOSED test-window trades across folds (OOS only)."""
    closed = [t for t in trades if t.pnl is not None]
    wins = [t.pnl for t in closed if t.pnl > 0]
    losses = [t.pnl for t in closed if t.pnl < 0]
    total = sum(t.pnl for t in closed)
    holds = [t.hold_days for t in closed if t.hold_days is not None]

    max_dd = 0.0
    peak: Optional[float] = None
    for _, equity in curve:
        if peak is None or equity > peak:
            peak = equity
        elif peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)

    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    payoff = (
        avg_win / abs(avg_loss)
        if avg_win is not None and avg_loss not in (None, 0) else None
    )
    return TradeStats(
        n_closed=len(closed),
        n_wins=len(wins),
        win_rate=len(wins) / len(closed) if closed else 0.0,
        avg_win=avg_win,
        avg_loss=avg_loss,
        payoff_ratio=payoff,
        expectancy=total / len(closed) if closed else 0.0,
        total_pnl=total,
        avg_hold_days=sum(holds) / len(holds) if holds else None,
        max_drawdown_pct=max_dd,
    )
