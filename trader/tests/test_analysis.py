"""Tests for swing_trader.analysis (Loop.md §5.3; backlog items 8 & 9).

Fully deterministic and network-free (Loop.md §3): every input is a
synthetic in-memory fixture; the FundamentalsProvider / LLMClient ports are
implemented by in-test fakes, never by anything that could do I/O.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest

from swing_trader.analysis import (
    DEBATE_EMPTY_CONFIDENCE,
    MIN_BARS,
    DebateAgent,
    FundamentalAgent,
    FundamentalsProvider,
    LLMAnalysisAgent,
    LLMClient,
    MacroAgent,
    SentimentAgent,
    StaticFundamentals,
    TechnicalAgent,
    pct_dist,
    rsi,
    sma,
)
from swing_trader.interfaces import Bar, NewsItem
from swing_trader.schemas import Direction, Signal

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


# --------------------------------------------------------------------------- builders


def make_bars(closes: list[float], symbol: str = "TEST") -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            ts=T0 + timedelta(days=i),
            open=c,
            high=c,
            low=c,
            close=c,
            volume=1_000_000.0,
        )
        for i, c in enumerate(closes)
    ]


def uptrend_closes(n: int = 80, start: float = 100.0) -> list[float]:
    """Zigzag uptrend (+1.5, -1.0 repeating): +0.25/bar average so
    close > sma20 > sma50, with pullbacks keeping Wilder RSI ~62 (< 70)."""
    closes = [start]
    for i in range(n - 1):
        closes.append(closes[-1] + (1.5 if i % 2 == 0 else -1.0))
    return closes


def downtrend_closes(n: int = 80, start: float = 100.0) -> list[float]:
    """Mirror zigzag downtrend: close < sma20 < sma50, RSI ~38 (> 30)."""
    closes = [start]
    for i in range(n - 1):
        closes.append(closes[-1] + (-1.5 if i % 2 == 0 else 1.0))
    return closes


def chop_closes(n: int = 80, base: float = 100.0) -> list[float]:
    """Alternating 100/101: sma20 == sma50 == 100.5, strict trend
    inequalities can never hold -> NEUTRAL."""
    return [base + (i % 2) for i in range(n)]


def make_news(sentiments: list[Optional[float]], symbol: str = "TEST") -> list[NewsItem]:
    return [
        NewsItem(
            symbol=symbol,
            ts=T0 + timedelta(hours=i),
            headline=f"headline {i}",
            sentiment=s,
        )
        for i, s in enumerate(sentiments)
    ]


def make_signal(
    direction: Direction,
    confidence: float,
    thesis: str = "t",
    source: str = "technical",
    symbol: str = "TEST",
) -> Signal:
    return Signal(
        source_agent=source,
        symbol=symbol,
        thesis=thesis,
        direction=direction,
        confidence=confidence,
    )


def assert_valid_signal(sig: Signal, source_agent: str) -> None:
    assert sig.source_agent == source_agent
    assert 0.0 <= sig.confidence <= 1.0
    assert sig.thesis  # human-readable, non-empty
    assert sig.ts.tzinfo is not None


# --------------------------------------------------------------------------- indicators


class TestIndicators:
    def test_sma_hand_checked(self) -> None:
        assert sma([1.0, 2.0, 3.0, 4.0], 2) == pytest.approx(3.5)  # last two
        assert sma([2.0, 4.0, 6.0], 3) == pytest.approx(4.0)

    def test_sma_insufficient_returns_none(self) -> None:
        assert sma([1.0, 2.0], 3) is None
        assert sma([], 1) is None

    def test_sma_invalid_window_raises(self) -> None:
        with pytest.raises(ValueError):
            sma([1.0, 2.0], 0)

    def test_rsi_hand_checked_wilder(self) -> None:
        # closes [10, 11, 10, 12], n=2: deltas [+1, -1, +2]
        # seed: avg_gain = 0.5, avg_loss = 0.5
        # Wilder step on +2: avg_gain = (0.5*1 + 2)/2 = 1.25,
        #                    avg_loss = (0.5*1 + 0)/2 = 0.25
        # RS = 5 -> RSI = 100 - 100/6 = 83.3333...
        assert rsi([10.0, 11.0, 10.0, 12.0], 2) == pytest.approx(83.3333, abs=1e-3)

    def test_rsi_degenerate_extremes(self) -> None:
        up = [float(i) for i in range(1, 31)]  # only gains
        down = [float(i) for i in range(31, 1, -1)]  # only losses
        assert rsi(up) == 100.0
        assert rsi(down) == 0.0

    def test_rsi_insufficient_returns_none(self) -> None:
        assert rsi([1.0] * 14, 14) is None  # needs n + 1 closes
        assert rsi([1.0] * 15, 14) is not None

    def test_rsi_invalid_period_raises(self) -> None:
        with pytest.raises(ValueError):
            rsi([1.0, 2.0], 0)

    def test_pct_dist_hand_checked(self) -> None:
        assert pct_dist(110.0, 100.0) == pytest.approx(10.0)
        assert pct_dist(90.0, 100.0) == pytest.approx(-10.0)
        assert pct_dist(5.0, 0.0) == 0.0  # zero-reference guard


# --------------------------------------------------------------------------- technical


class TestTechnicalAgent:
    def test_insufficient_bars_returns_none(self) -> None:
        bars = make_bars(uptrend_closes(MIN_BARS - 1))
        assert TechnicalAgent().analyze("TEST", bars) is None
        assert TechnicalAgent().analyze("TEST", []) is None

    def test_uptrend_long(self) -> None:
        sig = TechnicalAgent().analyze("nvda", make_bars(uptrend_closes()))
        assert sig is not None
        assert_valid_signal(sig, "technical")
        assert sig.symbol == "NVDA"
        assert sig.direction is Direction.LONG
        f = sig.features_json
        assert set(f) == {"rsi", "sma20", "sma50", "close", "dist_sma50_pct"}
        assert f["close"] > f["sma20"] > f["sma50"]
        assert f["rsi"] < 70.0
        # no stretch penalty (RSI in [35, 65]) so confidence > base 0.5
        assert 35.0 < f["rsi"] < 65.0
        assert sig.confidence > 0.5

    def test_downtrend_short(self) -> None:
        sig = TechnicalAgent().analyze("TEST", make_bars(downtrend_closes()))
        assert sig is not None
        assert sig.direction is Direction.SHORT
        f = sig.features_json
        assert f["close"] < f["sma20"] < f["sma50"]
        assert f["rsi"] > 30.0
        assert f["dist_sma50_pct"] < 0

    def test_chop_neutral(self) -> None:
        sig = TechnicalAgent().analyze("TEST", make_bars(chop_closes()))
        assert sig is not None
        assert sig.direction is Direction.NEUTRAL

    def test_overbought_uptrend_gated_to_neutral(self) -> None:
        # monotone rise -> RSI 100 -> the `rsi < 70` LONG gate must fail
        closes = [100.0 + i for i in range(80)]
        sig = TechnicalAgent().analyze("TEST", make_bars(closes))
        assert sig is not None
        assert sig.features_json["rsi"] == 100.0
        assert sig.direction is Direction.NEUTRAL

    def test_flat_series_exact_confidence(self) -> None:
        # flat: close == sma20 == sma50 (zero trend bonus), degenerate
        # RSI = 100 > 65 -> stretch penalty: 0.5 + 0.0 - 0.1 = 0.4
        sig = TechnicalAgent().analyze("TEST", make_bars([100.0] * 60))
        assert sig is not None
        assert sig.direction is Direction.NEUTRAL
        assert sig.confidence == pytest.approx(0.4)
        assert sig.features_json["dist_sma50_pct"] == 0.0

    def test_confidence_formula_matches_features(self) -> None:
        # documented formula: 0.5 + min(0.3, |dist%|/10 * 0.3) - (0.1 if RSI stretched)
        for closes in (uptrend_closes(), downtrend_closes(), chop_closes()):
            sig = TechnicalAgent().analyze("TEST", make_bars(closes))
            assert sig is not None
            f = sig.features_json
            bonus = min(0.3, abs(f["dist_sma50_pct"]) / 10.0 * 0.3)
            penalty = 0.1 if (f["rsi"] > 65.0 or f["rsi"] < 35.0) else 0.0
            assert sig.confidence == pytest.approx(
                max(0.0, min(1.0, 0.5 + bonus - penalty))
            )

    def test_adversarial_inputs_confidence_in_bounds(self) -> None:
        agent = TechnicalAgent()
        adversarial = [
            [1e-6] * 70 + [1e9] * 5,  # 15-order-of-magnitude spike
            [0.0] * 60,  # all-zero prices (zero-division bait)
            [1e12] * 30 + [1e-12] * 30,  # crash to ~zero
        ]
        for closes in adversarial:
            sig = agent.analyze("TEST", make_bars(closes))
            assert sig is not None
            assert 0.0 <= sig.confidence <= 1.0

    def test_determinism(self) -> None:
        agent = TechnicalAgent()
        bars = make_bars(uptrend_closes())
        a = agent.analyze("TEST", bars)
        b = agent.analyze("TEST", bars)
        assert a is not None and b is not None
        assert a.id != b.id  # fresh identity per Signal
        assert (a.direction, a.confidence, a.features_json, a.thesis) == (
            b.direction,
            b.confidence,
            b.features_json,
            b.thesis,
        )


# --------------------------------------------------------------------------- fundamental


def metrics(
    rev_growth_pct: Optional[float] = None,
    fwd_pe: Optional[float] = None,
    pe: Optional[float] = None,
    gross_margin_pct: Optional[float] = None,
) -> dict:
    return {
        "pe": pe,
        "fwd_pe": fwd_pe,
        "rev_growth_pct": rev_growth_pct,
        "gross_margin_pct": gross_margin_pct,
    }


class TestFundamentalAgent:
    def test_static_provider_satisfies_protocol(self) -> None:
        assert isinstance(StaticFundamentals(), FundamentalsProvider)

    def test_empty_provider_returns_none(self) -> None:
        agent = FundamentalAgent(StaticFundamentals({}))
        assert agent.analyze("NVDA") is None
        # default provider is empty as well
        assert FundamentalAgent().analyze("NVDA") is None

    def test_missing_rev_growth_returns_none(self) -> None:
        agent = FundamentalAgent(StaticFundamentals({"NVDA": metrics(fwd_pe=30.0)}))
        assert agent.analyze("NVDA") is None

    def test_long_growth_and_reasonable_pe(self) -> None:
        m = metrics(rev_growth_pct=25.0, fwd_pe=30.0, pe=45.0, gross_margin_pct=70.0)
        agent = FundamentalAgent(StaticFundamentals({"NVDA": m}))
        sig = agent.analyze("NVDA")
        assert sig is not None
        assert_valid_signal(sig, "fundamental")
        assert sig.direction is Direction.LONG
        assert sig.features_json == m  # features_json = metrics
        assert sig.confidence == pytest.approx(0.4 + 0.3 * 25.0 / 50.0)  # 0.55

    def test_long_when_fwd_pe_unknown(self) -> None:
        agent = FundamentalAgent(
            StaticFundamentals({"MU": metrics(rev_growth_pct=20.0, fwd_pe=None)})
        )
        sig = agent.analyze("MU")
        assert sig is not None
        assert sig.direction is Direction.LONG

    def test_neutral_low_growth(self) -> None:
        agent = FundamentalAgent(
            StaticFundamentals({"KO": metrics(rev_growth_pct=10.0, fwd_pe=20.0)})
        )
        sig = agent.analyze("KO")
        assert sig is not None
        assert sig.direction is Direction.NEUTRAL
        assert sig.confidence == pytest.approx(0.4 + 0.3 * 10.0 / 50.0)  # 0.46

    def test_neutral_good_growth_but_rich_pe(self) -> None:
        # growth qualifies but fwd_pe in [40, 80]: neither LONG nor SHORT
        agent = FundamentalAgent(
            StaticFundamentals({"NOW": metrics(rev_growth_pct=22.0, fwd_pe=55.0)})
        )
        sig = agent.analyze("NOW")
        assert sig is not None
        assert sig.direction is Direction.NEUTRAL

    def test_short_negative_growth(self) -> None:
        agent = FundamentalAgent(
            StaticFundamentals({"WDC": metrics(rev_growth_pct=-5.0, fwd_pe=12.0)})
        )
        sig = agent.analyze("WDC")
        assert sig is not None
        assert sig.direction is Direction.SHORT

    def test_short_extreme_fwd_pe_overrides_growth(self) -> None:
        agent = FundamentalAgent(
            StaticFundamentals({"PLTR": metrics(rev_growth_pct=30.0, fwd_pe=120.0)})
        )
        sig = agent.analyze("PLTR")
        assert sig is not None
        assert sig.direction is Direction.SHORT

    def test_confidence_bounds_extreme_growth(self) -> None:
        for growth in (0.0, 1e6, -1e6):
            agent = FundamentalAgent(
                StaticFundamentals({"X": metrics(rev_growth_pct=growth)})
            )
            sig = agent.analyze("X")
            assert sig is not None
            assert 0.4 <= sig.confidence <= 0.7
        # scaling saturates at |growth| = 50
        agent = FundamentalAgent(
            StaticFundamentals({"X": metrics(rev_growth_pct=1e6)})
        )
        assert agent.analyze("X").confidence == pytest.approx(0.7)


# --------------------------------------------------------------------------- sentiment


class TestSentimentAgent:
    def test_fewer_than_three_scored_returns_none(self) -> None:
        agent = SentimentAgent()
        # 5 items but only 2 scored -> None (unscored are skipped)
        news = make_news([0.9, None, None, 0.8, None])
        assert agent.analyze("TEST", news) is None
        assert agent.analyze("TEST", []) is None

    def test_positive_avg_long(self) -> None:
        sig = SentimentAgent().analyze("TEST", make_news([0.5, 0.4, 0.3, None]))
        assert sig is not None
        assert_valid_signal(sig, "sentiment")
        assert sig.direction is Direction.LONG
        assert sig.features_json == {"n_items": 3, "avg_sentiment": pytest.approx(0.4)}
        assert sig.confidence == pytest.approx(0.4 + 0.4 * 0.4)  # 0.56

    def test_negative_avg_short(self) -> None:
        sig = SentimentAgent().analyze("TEST", make_news([-0.5, -0.4, -0.6]))
        assert sig is not None
        assert sig.direction is Direction.SHORT
        assert sig.confidence == pytest.approx(0.4 + 0.4 * 0.5)  # 0.6

    def test_mixed_avg_neutral(self) -> None:
        sig = SentimentAgent().analyze("TEST", make_news([0.3, -0.3, 0.1]))
        assert sig is not None
        assert sig.direction is Direction.NEUTRAL

    def test_confidence_capped_for_extreme_scores(self) -> None:
        # adversarial: scores way outside the documented [-1, 1]
        sig = SentimentAgent().analyze("TEST", make_news([50.0, 60.0, 70.0]))
        assert sig is not None
        assert sig.confidence == pytest.approx(0.8)  # hard cap
        assert 0.0 <= sig.confidence <= 1.0


# --------------------------------------------------------------------------- macro


class TestMacroAgent:
    def test_risk_on_long(self) -> None:
        sig = MacroAgent().analyze(
            {"risk_on_off": "risk_on", "vix": 15.0, "breadth_pct_above_50dma": 55.0}
        )
        assert_valid_signal(sig, "macro")
        assert sig.symbol == "SPY"
        assert sig.direction is Direction.LONG
        assert sig.confidence == pytest.approx(0.6)  # breadth 55 does not confirm
        assert sig.features_json["vix"] == 15.0
        assert sig.features_json["breadth_pct_above_50dma"] == 55.0

    def test_risk_off_short(self) -> None:
        sig = MacroAgent().analyze(
            {"risk_on_off": "risk_off", "vix": 32.0, "breadth_pct_above_50dma": 45.0}
        )
        assert sig.direction is Direction.SHORT
        assert sig.confidence == pytest.approx(0.6)

    def test_neutral(self) -> None:
        sig = MacroAgent().analyze(
            {"risk_on_off": "neutral", "vix": 18.0, "breadth_pct_above_50dma": 70.0}
        )
        assert sig.direction is Direction.NEUTRAL
        assert sig.confidence == pytest.approx(0.5)  # breadth never boosts NEUTRAL

    def test_breadth_confirmation_bonus(self) -> None:
        long_sig = MacroAgent().analyze(
            {"risk_on_off": "risk_on", "vix": 14.0, "breadth_pct_above_50dma": 72.0}
        )
        assert long_sig.direction is Direction.LONG
        assert long_sig.confidence == pytest.approx(0.7)
        short_sig = MacroAgent().analyze(
            {"risk_on_off": "risk_off", "vix": 35.0, "breadth_pct_above_50dma": 25.0}
        )
        assert short_sig.direction is Direction.SHORT
        assert short_sig.confidence == pytest.approx(0.7)

    def test_missing_keys_defaults_neutral(self) -> None:
        sig = MacroAgent().analyze({})
        assert sig.direction is Direction.NEUTRAL
        assert sig.confidence == pytest.approx(0.5)
        assert sig.features_json["vix"] is None
        assert sig.features_json["breadth_pct_above_50dma"] is None


# --------------------------------------------------------------------------- debate


class TestDebateAgent:
    def test_empty_signals_neutral_low_confidence(self) -> None:
        sig = DebateAgent().debate("TEST", [])
        assert_valid_signal(sig, "debate")
        assert sig.direction is Direction.NEUTRAL
        assert sig.confidence == pytest.approx(DEBATE_EMPTY_CONFIDENCE)
        assert sig.features_json["n_signals"] == 0

    def test_unanimous_long(self) -> None:
        signals = [
            make_signal(Direction.LONG, 0.7, thesis="trend up"),
            make_signal(Direction.LONG, 0.5, thesis="growth strong"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.LONG
        # net = +1.0, no short camp -> no penalty: 0.4 + 0.4 = 0.8
        assert sig.confidence == pytest.approx(0.8)
        assert sig.features_json["net_weight"] == pytest.approx(1.0)
        assert sig.features_json["disagreement_penalty"] == 0.0

    def test_majority_long_no_penalty_at_boundary(self) -> None:
        # long share 0.8, short share 0.2 (NOT > 0.2 -> no penalty)
        signals = [
            make_signal(Direction.LONG, 0.8, thesis="bull case"),
            make_signal(Direction.SHORT, 0.2, thesis="bear case"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.LONG
        assert sig.features_json["net_weight"] == pytest.approx(0.6)
        assert sig.features_json["disagreement_penalty"] == 0.0
        assert sig.confidence == pytest.approx(0.4 + 0.4 * 0.6)  # 0.64

    def test_tie_is_neutral_with_penalty(self) -> None:
        signals = [
            make_signal(Direction.LONG, 0.6, thesis="bull"),
            make_signal(Direction.SHORT, 0.6, thesis="bear"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.NEUTRAL
        assert sig.features_json["net_weight"] == pytest.approx(0.0)
        # both camps at 0.5 share > 0.2 -> penalty: 0.4 + 0 - 0.15 = 0.25
        assert sig.confidence == pytest.approx(0.25)

    def test_disagreement_penalty_applied(self) -> None:
        # long 0.7 / short 0.3 shares: net +0.4 -> LONG, both camps > 0.2
        signals = [
            make_signal(Direction.LONG, 0.7, thesis="bull"),
            make_signal(Direction.SHORT, 0.3, thesis="bear"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.LONG
        assert sig.features_json["disagreement_penalty"] == pytest.approx(0.15)
        assert sig.confidence == pytest.approx(0.4 + 0.4 * 0.4 - 0.15)  # 0.41

    def test_neutral_signals_dilute_but_dont_vote(self) -> None:
        # LONG 0.5 + NEUTRAL 0.5: net = 0.5/1.0 = +0.5 -> LONG
        signals = [
            make_signal(Direction.LONG, 0.5, thesis="bull"),
            make_signal(Direction.NEUTRAL, 0.5, thesis="meh"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.LONG
        assert sig.features_json["net_weight"] == pytest.approx(0.5)

    def test_small_net_within_threshold_is_neutral(self) -> None:
        # net = (0.55 - 0.45) / 1.0 = 0.10, inside the ±0.15 band
        signals = [
            make_signal(Direction.LONG, 0.55, thesis="bull"),
            make_signal(Direction.SHORT, 0.45, thesis="bear"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.NEUTRAL

    def test_short_verdict(self) -> None:
        signals = [
            make_signal(Direction.SHORT, 0.8, thesis="bear"),
            make_signal(Direction.NEUTRAL, 0.2, thesis="meh"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.direction is Direction.SHORT
        assert sig.features_json["net_weight"] == pytest.approx(-0.8)

    def test_thesis_format_bull_bear_verdict(self) -> None:
        signals = [
            make_signal(Direction.LONG, 0.9, thesis="strong uptrend"),
            make_signal(Direction.SHORT, 0.1, thesis="valuation stretched"),
        ]
        sig = DebateAgent().debate("TEST", signals)
        assert sig.thesis.startswith("BULL: strong uptrend")
        assert "| BEAR: valuation stretched" in sig.thesis
        assert "verdict LONG" in sig.thesis
        # empty camps rendered explicitly
        lonely = DebateAgent().debate("TEST", [make_signal(Direction.LONG, 0.9)])
        assert "| BEAR: (none)" in lonely.thesis

    def test_all_zero_confidence_signals_safe(self) -> None:
        signals = [
            make_signal(Direction.LONG, 0.0),
            make_signal(Direction.SHORT, 0.0),
        ]
        sig = DebateAgent().debate("TEST", signals)  # no ZeroDivisionError
        assert sig.direction is Direction.NEUTRAL
        assert 0.0 <= sig.confidence <= 1.0

    def test_determinism(self) -> None:
        signals = [
            make_signal(Direction.LONG, 0.7, thesis="bull"),
            make_signal(Direction.SHORT, 0.3, thesis="bear"),
            make_signal(Direction.NEUTRAL, 0.5, thesis="meh"),
        ]
        a = DebateAgent().debate("TEST", signals)
        b = DebateAgent().debate("TEST", signals)
        assert a.id != b.id
        assert (a.direction, a.confidence, a.features_json, a.thesis) == (
            b.direction,
            b.confidence,
            b.features_json,
            b.thesis,
        )


# --------------------------------------------------------------------------- LLM stub


class FakeLLMClient:
    """In-test LLMClient implementation; must never be invoked in Phase 0."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return "{}"


class TestLLMUpgradePath:
    def test_fake_satisfies_protocol(self) -> None:
        assert isinstance(FakeLLMClient(), LLMClient)

    def test_analyze_raises_not_implemented_and_never_calls_llm(self) -> None:
        client = FakeLLMClient()
        agent = LLMAnalysisAgent(client)
        with pytest.raises(NotImplementedError, match="Phase 0.5"):
            agent.analyze("NVDA", context={"bars": []})
        assert client.calls == []  # no LLM calls in Phase 0 v0 (Loop.md §3)
