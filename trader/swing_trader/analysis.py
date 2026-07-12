"""Analysis sub-agents + Debate agent (Loop.md §5.3; backlog items 8 & 9).

Rule-based v0: each agent turns already-fetched inputs (bars, news, metrics,
market snapshot) into a :class:`swing_trader.schemas.Signal` with
``source_agent`` set, ``features_json`` populated, ``confidence`` in [0, 1]
and a human-readable ``thesis``. Everything here is pure and deterministic —
no network, no wall-clock logic beyond the Signal timestamp (Loop.md §3).

Phase 0 notes:

- The account is CASH (Loop.md §2) so a SHORT-direction signal means
  "avoid / trim existing position", never an actual short sale. The decision
  core (§5.4) is responsible for translating that.
- The LLM upgrade path (§5.3 "start rule-based; upgrade to LLM", §8 model
  plan) stays behind the :class:`LLMClient` protocol; :class:`LLMAnalysisAgent`
  is a stub that raises ``NotImplementedError`` — no LLM calls in Phase 0 v0.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Optional, Protocol, runtime_checkable

from swing_trader.interfaces import Bar, NewsItem
from swing_trader.log import get_logger
from swing_trader.schemas import Direction, Signal

logger = get_logger(__name__)

__all__ = [
    "DebateAgent",
    "FundamentalAgent",
    "FundamentalsProvider",
    "LLMAnalysisAgent",
    "LLMClient",
    "MacroAgent",
    "SentimentAgent",
    "StaticFundamentals",
    "TechnicalAgent",
    "pct_dist",
    "rsi",
    "sma",
]

# --------------------------------------------------------------------------- tunables
# Deterministic rule-of-thumb constants for v0. Self-improvement may retune
# these (analysis quality only — Loop.md §3), never anything in risk.py.

MIN_BARS: int = 60  # TechnicalAgent needs sma50 + rsi warm-up headroom

RSI_OVERBOUGHT: float = 70.0
RSI_OVERSOLD: float = 30.0
RSI_STRETCH_HIGH: float = 65.0  # confidence penalty band
RSI_STRETCH_LOW: float = 35.0
RSI_STRETCH_PENALTY: float = 0.1

TREND_BONUS_MAX: float = 0.3
TREND_FULL_SCALE_FRAC: float = 0.10  # 10% from sma50 earns the full bonus

GROWTH_FULL_SCALE_PCT: float = 50.0  # |rev growth| of 50% -> max fundamental conf

SENTIMENT_MIN_SCORED: int = 3
SENTIMENT_LONG_TH: float = 0.2
SENTIMENT_SHORT_TH: float = -0.2
SENTIMENT_CONF_CAP: float = 0.8

MACRO_SYMBOL: str = "SPY"
BREADTH_LONG_CONFIRM: float = 60.0
BREADTH_SHORT_CONFIRM: float = 40.0

DEBATE_NET_TH: float = 0.15
DEBATE_CAMP_MIN_SHARE: float = 0.2
DEBATE_DISAGREEMENT_PENALTY: float = 0.15
DEBATE_EMPTY_CONFIDENCE: float = 0.3


def _clamp01(x: float) -> float:
    """Clamp to [0, 1] — Signal.confidence is schema-validated to this range."""
    return max(0.0, min(1.0, x))


# --------------------------------------------------------------------------- indicators


def sma(values: Sequence[float], n: int) -> Optional[float]:
    """Simple moving average of the LAST ``n`` values.

    Returns None when fewer than ``n`` values are available.
    """
    if n <= 0:
        raise ValueError(f"sma window must be positive, got {n}")
    if len(values) < n:
        return None
    window = values[-n:]
    return sum(window) / n


def rsi(closes: Sequence[float], n: int = 14) -> Optional[float]:
    """Relative Strength Index over ``n`` periods using **Wilder smoothing**.

    Choice documented per task: the seed averages are the simple means of the
    first ``n`` gains/losses; every later period uses Wilder's recursive
    smoothing ``avg = (avg * (n - 1) + current) / n``. This matches the
    classic Wilder RSI (and most charting packages) rather than the
    cutoff-window "simple" variant.

    Returns None when fewer than ``n + 1`` closes are available. Degenerate
    cases: no losses at all -> 100.0; no gains at all -> 0.0 (a perfectly
    flat series therefore reads 100.0 — RSI is undefined there and the
    agents only use it as a gate, never as the sole driver).
    """
    if n <= 0:
        raise ValueError(f"rsi period must be positive, got {n}")
    if len(closes) < n + 1:
        return None
    deltas = [closes[i + 1] - closes[i] for i in range(len(closes) - 1)]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n
    if avg_loss == 0.0:
        return 100.0
    if avg_gain == 0.0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def pct_dist(a: float, b: float) -> float:
    """Percent distance of ``a`` from reference ``b``: ``(a - b) / b * 100``.

    Guard: returns 0.0 when ``b == 0`` (adversarial input; a zero reference
    price has no meaningful distance) so callers never divide by zero.
    """
    if b == 0:
        return 0.0
    return (a - b) / b * 100.0


# --------------------------------------------------------------------------- technical


class TechnicalAgent:
    """Trend + momentum rules on daily bars (Loop.md §5.3, rule-based v0).

    LONG  when close > sma20 > sma50 and rsi < 70 (uptrend, not overbought);
    SHORT when close < sma20 < sma50 and rsi > 30 (downtrend, not oversold;
    Phase 0 = avoid/trim, cash account cannot short); else NEUTRAL.

    Confidence = 0.5 base, + up to ``TREND_BONUS_MAX`` scaled linearly by
    |close - sma50| / sma50 (full bonus at ``TREND_FULL_SCALE_FRAC`` = 10%
    distance, capped), - ``RSI_STRETCH_PENALTY`` when RSI is stretched
    (> 65 or < 35). Clamped to [0, 1].
    """

    source_agent: str = "technical"

    def analyze(self, symbol: str, bars: Sequence[Bar]) -> Optional[Signal]:
        if len(bars) < MIN_BARS:
            logger.debug(
                "technical: insufficient bars",
                extra={"symbol": symbol, "n_bars": len(bars), "min_bars": MIN_BARS},
            )
            return None
        closes = [bar.close for bar in bars]
        close = closes[-1]
        sma20 = sma(closes, 20)
        sma50 = sma(closes, 50)
        rsi14 = rsi(closes, 14)
        if sma20 is None or sma50 is None or rsi14 is None:  # pragma: no cover
            return None  # unreachable with MIN_BARS >= 60; defensive narrowing
        dist_sma50_pct = pct_dist(close, sma50)

        if close > sma20 > sma50 and rsi14 < RSI_OVERBOUGHT:
            direction = Direction.LONG
            thesis = (
                f"{symbol}: uptrend — close {close:.2f} > SMA20 {sma20:.2f} > "
                f"SMA50 {sma50:.2f}, RSI {rsi14:.1f} below overbought"
            )
        elif close < sma20 < sma50 and rsi14 > RSI_OVERSOLD:
            direction = Direction.SHORT
            thesis = (
                f"{symbol}: downtrend — close {close:.2f} < SMA20 {sma20:.2f} < "
                f"SMA50 {sma50:.2f}, RSI {rsi14:.1f} above oversold "
                f"(Phase 0 cash account: avoid/trim, no short sale)"
            )
        else:
            direction = Direction.NEUTRAL
            thesis = (
                f"{symbol}: no clear trend alignment — close {close:.2f}, "
                f"SMA20 {sma20:.2f}, SMA50 {sma50:.2f}, RSI {rsi14:.1f}"
            )

        trend_bonus = min(
            TREND_BONUS_MAX,
            (abs(dist_sma50_pct) / 100.0) / TREND_FULL_SCALE_FRAC * TREND_BONUS_MAX,
        )
        stretch_penalty = (
            RSI_STRETCH_PENALTY
            if (rsi14 > RSI_STRETCH_HIGH or rsi14 < RSI_STRETCH_LOW)
            else 0.0
        )
        confidence = _clamp01(0.5 + trend_bonus - stretch_penalty)

        return Signal(
            source_agent=self.source_agent,
            symbol=symbol,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json={
                "rsi": rsi14,
                "sma20": sma20,
                "sma50": sma50,
                "close": close,
                "dist_sma50_pct": dist_sma50_pct,
            },
        )


# --------------------------------------------------------------------------- fundamental


@runtime_checkable
class FundamentalsProvider(Protocol):
    """Port for fundamental metrics — mockable so tests never hit the network
    (Loop.md §3). Expected keys: pe, fwd_pe, rev_growth_pct, gross_margin_pct
    (any may be None when unknown). Returns None when the symbol is unknown.
    """

    def get_metrics(self, symbol: str) -> Optional[dict]: ...


class StaticFundamentals:
    """In-memory :class:`FundamentalsProvider` — v0 default and test double.

    An empty mapping (the default) makes :class:`FundamentalAgent` return
    None for every symbol, i.e. "no fundamental view yet".
    """

    def __init__(self, data: Optional[Mapping[str, dict]] = None) -> None:
        self._data: dict[str, dict] = {
            symbol.strip().upper(): dict(metrics)
            for symbol, metrics in (data or {}).items()
        }

    def get_metrics(self, symbol: str) -> Optional[dict]:
        metrics = self._data.get(symbol.strip().upper())
        return dict(metrics) if metrics is not None else None


class FundamentalAgent:
    """Growth/valuation rules (Loop.md §5.3, rule-based v0).

    SHORT bias when rev_growth_pct < 0 or fwd_pe > 80 (checked first — a
    blow-off valuation vetoes the growth story); LONG when
    rev_growth_pct > 15 and (fwd_pe is None or fwd_pe < 40); else NEUTRAL
    (covers the 0–15% growth band and rich-but-not-extreme valuations).

    Confidence in [0.4, 0.7], scaled linearly by |rev_growth_pct| with the
    max reached at ``GROWTH_FULL_SCALE_PCT`` (50%). Returns None when the
    provider has no metrics or no ``rev_growth_pct`` (no data, no signal).
    """

    source_agent: str = "fundamental"

    def __init__(self, provider: Optional[FundamentalsProvider] = None) -> None:
        self._provider: FundamentalsProvider = (
            provider if provider is not None else StaticFundamentals()
        )

    def analyze(self, symbol: str) -> Optional[Signal]:
        metrics = self._provider.get_metrics(symbol)
        if not metrics:
            logger.debug("fundamental: no metrics", extra={"symbol": symbol})
            return None
        growth = metrics.get("rev_growth_pct")
        if growth is None:
            logger.debug("fundamental: no rev_growth_pct", extra={"symbol": symbol})
            return None
        fwd_pe = metrics.get("fwd_pe")

        if growth < 0 or (fwd_pe is not None and fwd_pe > 80):
            direction = Direction.SHORT
            thesis = (
                f"{symbol}: fundamental caution — revenue growth {growth:.1f}%, "
                f"fwd P/E {'n/a' if fwd_pe is None else f'{fwd_pe:.1f}'} "
                f"(shrinking revenue or extreme valuation; avoid/trim)"
            )
        elif growth > 15 and (fwd_pe is None or fwd_pe < 40):
            direction = Direction.LONG
            thesis = (
                f"{symbol}: fundamental strength — revenue growth {growth:.1f}% "
                f"at fwd P/E {'n/a' if fwd_pe is None else f'{fwd_pe:.1f}'}"
            )
        else:
            direction = Direction.NEUTRAL
            thesis = (
                f"{symbol}: fundamentals mixed — revenue growth {growth:.1f}%, "
                f"fwd P/E {'n/a' if fwd_pe is None else f'{fwd_pe:.1f}'}"
            )

        confidence = _clamp01(
            0.4 + 0.3 * min(abs(growth) / GROWTH_FULL_SCALE_PCT, 1.0)
        )
        return Signal(
            source_agent=self.source_agent,
            symbol=symbol,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json=dict(metrics),
        )


# --------------------------------------------------------------------------- sentiment


class SentimentAgent:
    """News-sentiment aggregation (Loop.md §5.3, rule-based v0).

    Uses pre-scored ``NewsItem.sentiment`` values in [-1, 1] (unscored items
    skipped). Needs at least ``SENTIMENT_MIN_SCORED`` scored items, else None.
    avg > +0.2 -> LONG; avg < -0.2 -> SHORT; else NEUTRAL.
    Confidence = 0.4 + 0.4 * |avg|, capped at 0.8.
    ``features_json.n_items`` counts the SCORED items used.
    """

    source_agent: str = "sentiment"

    def analyze(self, symbol: str, news: Sequence[NewsItem]) -> Optional[Signal]:
        scored = [item.sentiment for item in news if item.sentiment is not None]
        if len(scored) < SENTIMENT_MIN_SCORED:
            logger.debug(
                "sentiment: insufficient scored items",
                extra={"symbol": symbol, "n_scored": len(scored)},
            )
            return None
        avg = sum(scored) / len(scored)

        if avg > SENTIMENT_LONG_TH:
            direction = Direction.LONG
            tone = "positive"
        elif avg < SENTIMENT_SHORT_TH:
            direction = Direction.SHORT
            tone = "negative"
        else:
            direction = Direction.NEUTRAL
            tone = "mixed"
        thesis = (
            f"{symbol}: news sentiment {tone} — avg {avg:+.2f} "
            f"across {len(scored)} scored items"
        )
        confidence = _clamp01(min(SENTIMENT_CONF_CAP, 0.4 + 0.4 * abs(avg)))
        return Signal(
            source_agent=self.source_agent,
            symbol=symbol,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json={"n_items": len(scored), "avg_sentiment": avg},
        )


# --------------------------------------------------------------------------- macro


class MacroAgent:
    """Market-regime read (Loop.md §5.3, rule-based v0). Always emits a
    Signal on the market proxy symbol ("SPY").

    Input ``market`` dict keys: ``risk_on_off`` ("risk_on" | "risk_off" |
    "neutral"; missing/unknown treated as neutral), ``vix``,
    ``breadth_pct_above_50dma``. risk_on -> LONG 0.6; risk_off -> SHORT 0.6;
    neutral -> NEUTRAL 0.5; +0.1 confidence when breadth confirms
    (> 60 for LONG, < 40 for SHORT).
    """

    source_agent: str = "macro"

    def analyze(self, market: Mapping[str, object]) -> Signal:
        raw_regime = market.get("risk_on_off") or "neutral"
        regime = str(raw_regime).strip().lower().replace("-", "_")
        vix = market.get("vix")
        breadth = market.get("breadth_pct_above_50dma")

        if regime == "risk_on":
            direction, confidence = Direction.LONG, 0.6
        elif regime == "risk_off":
            direction, confidence = Direction.SHORT, 0.6
        else:
            regime = "neutral"
            direction, confidence = Direction.NEUTRAL, 0.5

        breadth_confirms = False
        if isinstance(breadth, (int, float)):
            if direction is Direction.LONG and breadth > BREADTH_LONG_CONFIRM:
                breadth_confirms = True
            elif direction is Direction.SHORT and breadth < BREADTH_SHORT_CONFIRM:
                breadth_confirms = True
        if breadth_confirms:
            confidence += 0.1
        confidence = _clamp01(confidence)

        thesis = (
            f"Market regime {regime} — VIX "
            f"{'n/a' if not isinstance(vix, (int, float)) else f'{vix:.1f}'}, "
            f"breadth "
            f"{'n/a' if not isinstance(breadth, (int, float)) else f'{breadth:.0f}%'}"
            f" above 50dma{' (breadth confirms)' if breadth_confirms else ''}"
        )
        return Signal(
            source_agent=self.source_agent,
            symbol=MACRO_SYMBOL,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json={
                "risk_on_off": regime,
                "vix": vix if isinstance(vix, (int, float)) else None,
                "breadth_pct_above_50dma": (
                    breadth if isinstance(breadth, (int, float)) else None
                ),
                "breadth_confirms": breadth_confirms,
            },
        )


# --------------------------------------------------------------------------- debate


class DebateAgent:
    """Bull-vs-bear synthesis (Loop.md §5.3; backlog item 9).

    Confidence-weighted vote: LONG = +1, SHORT = -1, NEUTRAL = 0, each
    weighted by the signal's confidence. Camp weights are NORMALIZED by total
    confidence so ``net`` lives in [-1, +1] regardless of how many signals
    arrive (an unnormalized sum would trip the ±0.15 threshold on any single
    signal). net > +0.15 -> LONG; net < -0.15 -> SHORT; else NEUTRAL.

    Verdict confidence = 0.4 + 0.4 * |net|, minus a 0.15 disagreement
    penalty when BOTH the long and short camps hold > 0.2 of the total
    weight (a real two-sided argument deserves less conviction).
    Empty input -> NEUTRAL at 0.3.
    """

    source_agent: str = "debate"

    def debate(self, symbol: str, signals: Sequence[Signal]) -> Signal:
        if not signals:
            return Signal(
                source_agent=self.source_agent,
                symbol=symbol,
                thesis=(
                    "BULL: (none) | BEAR: (none) | "
                    "verdict NEUTRAL (no signals to debate)"
                ),
                direction=Direction.NEUTRAL,
                confidence=DEBATE_EMPTY_CONFIDENCE,
                features_json={
                    "n_signals": 0,
                    "net_weight": 0.0,
                    "long_weight": 0.0,
                    "short_weight": 0.0,
                    "disagreement_penalty": 0.0,
                },
            )

        total = sum(s.confidence for s in signals)
        long_raw = sum(s.confidence for s in signals if s.direction is Direction.LONG)
        short_raw = sum(s.confidence for s in signals if s.direction is Direction.SHORT)
        if total > 0:
            long_share = long_raw / total
            short_share = short_raw / total
        else:  # all-zero-confidence signals: no information, no division
            long_share = short_share = 0.0
        net = long_share - short_share

        if net > DEBATE_NET_TH:
            direction = Direction.LONG
        elif net < -DEBATE_NET_TH:
            direction = Direction.SHORT
        else:
            direction = Direction.NEUTRAL

        penalty = (
            DEBATE_DISAGREEMENT_PENALTY
            if (
                long_share > DEBATE_CAMP_MIN_SHARE
                and short_share > DEBATE_CAMP_MIN_SHARE
            )
            else 0.0
        )
        confidence = _clamp01(0.4 + 0.4 * abs(net) - penalty)

        bull = "; ".join(
            s.thesis for s in signals if s.direction is Direction.LONG
        ) or "(none)"
        bear = "; ".join(
            s.thesis for s in signals if s.direction is Direction.SHORT
        ) or "(none)"
        thesis = (
            f"BULL: {bull} | BEAR: {bear} | "
            f"verdict {direction.value.upper()} "
            f"(net {net:+.2f} from {len(signals)} signals"
            f"{', disagreement penalty applied' if penalty else ''})"
        )
        return Signal(
            source_agent=self.source_agent,
            symbol=symbol,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json={
                "n_signals": len(signals),
                "net_weight": net,
                "long_weight": long_share,
                "short_weight": short_share,
                "disagreement_penalty": penalty,
            },
        )


# --------------------------------------------------------------------------- LLM upgrade path


@runtime_checkable
class LLMClient(Protocol):
    """Model-agnostic LLM port (Loop.md §8: model chosen via config, so a
    model switch is a config change, not a rewrite). Adapters implement this;
    tests fake it. No implementation may be called in Phase 0 v0.
    """

    def complete(self, system: str, prompt: str) -> str: ...


class LLMAnalysisAgent:
    """Stub for the LLM-backed analysis upgrade (Loop.md §5.3 "start
    rule-based; upgrade to LLM"). Wire an :class:`LLMClient` in now so the
    seam exists; the analysis itself lands in Phase 0.5.
    """

    source_agent: str = "llm"

    def __init__(self, client: LLMClient) -> None:
        self._client = client

    def analyze(
        self, symbol: str, context: Optional[dict] = None
    ) -> Optional[Signal]:
        raise NotImplementedError(
            "TODO(Phase 0.5): LLM analysis via model-agnostic config — "
            "Loop.md section 8 model plan"
        )
