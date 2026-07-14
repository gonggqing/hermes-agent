"""Tests for the DATA as-of-bar on signals/verdicts (Loop.md §5.10).

A verdict's close/SMA/RSI reflect the LAST BAR it analyzed, not "now". Recording
that bar date (``Signal.as_of_bar``) — distinct from the runtime ``ts`` — is what
stops a weekend-old verdict from being misread against a later live price. This
covers the whole flow: technical agent → debate inheritance → ledger round-trip
→ brief SignalView → analyze render.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from swing_trader.analysis import DebateAgent, TechnicalAgent
from swing_trader.interfaces import Bar
from swing_trader.ledger import Ledger
from swing_trader.on_demand import render_analysis_zh
from swing_trader.schemas import Direction, Mode, Signal

UTC = timezone.utc
T0 = datetime(2026, 6, 1, 20, 0, tzinfo=UTC)


def _bars(closes, symbol="NVDA"):
    return [Bar(symbol=symbol, ts=T0 + timedelta(days=i), open=c, high=c, low=c,
                close=c, volume=1e6) for i, c in enumerate(closes)]


class TestTechnicalAsOfBar:
    def test_technical_records_last_bar_date(self):
        bars = _bars([100 + i for i in range(70)])  # steady uptrend, ≥ MIN_BARS
        sig = TechnicalAgent().analyze("NVDA", bars)
        assert sig is not None
        assert sig.as_of_bar == bars[-1].ts  # last bar, NOT now
        # and it differs from the runtime ts (which is ~now)
        assert sig.as_of_bar != sig.ts


class TestDebateInheritance:
    def test_debate_inherits_freshest_bar(self):
        old = Signal(source_agent="technical", symbol="NVDA", thesis="a",
                     direction=Direction.LONG, confidence=0.6,
                     as_of_bar=datetime(2026, 6, 30, tzinfo=UTC))
        fresh = Signal(source_agent="fundamental", symbol="NVDA", thesis="b",
                       direction=Direction.LONG, confidence=0.5,
                       as_of_bar=datetime(2026, 7, 13, tzinfo=UTC))
        verdict = DebateAgent().debate("NVDA", [old, fresh])
        assert verdict.as_of_bar == datetime(2026, 7, 13, tzinfo=UTC)  # freshest

    def test_debate_none_when_no_bar_signals(self):
        senti = Signal(source_agent="sentiment", symbol="NVDA", thesis="x",
                       direction=Direction.NEUTRAL, confidence=0.5)  # no as_of_bar
        assert DebateAgent().debate("NVDA", [senti]).as_of_bar is None

    def test_empty_debate_has_no_bar(self):
        assert DebateAgent().debate("NVDA", []).as_of_bar is None


class TestLedgerRoundTrip:
    def test_signal_persists_as_of_bar_without_schema_change(self, tmp_path):
        led = Ledger(url=f"sqlite:///{tmp_path/'s.db'}")
        bar_date = datetime(2026, 6, 30, 20, 0, tzinfo=UTC)
        sig = Signal(source_agent="technical", symbol="NVDA", thesis="t",
                     direction=Direction.LONG, confidence=0.8, as_of_bar=bar_date,
                     features_json={"close": 155.38, "sma20": 149.76})
        led.record_signal(sig, Mode.PAPER)
        got = led.get_signals(Mode.PAPER)
        assert len(got) == 1
        assert got[0].as_of_bar == bar_date
        # the internal storage key never leaks back into features_json
        assert "_as_of_bar" not in got[0].features_json
        assert got[0].features_json == {"close": 155.38, "sma20": 149.76}


class TestRenderShowsBarDate:
    def test_render_analysis_zh_shows_data_as_of(self):
        result = {
            "symbol": "NVDA", "last": 155.38,
            "verdict": {"source_agent": "debate", "direction": "long",
                        "confidence": 0.8, "thesis": "…",
                        "as_of_bar": "2026-06-30T20:00:00+00:00"},
            "signals": [],
        }
        text = render_analysis_zh(result)
        assert "数据截至 2026-06-30" in text  # bar date, not "now"
        assert "收 155.38" in text  # labelled as CLOSE, not 现价
