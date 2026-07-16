"""Automatic market-close forecast evaluation (deterministic, no network)."""

from datetime import date, datetime, timezone

import pytest

from swing_trader.brief import (
    ForecastClaim,
    FreshnessInfo,
    NarrativeSection,
    ResearchBrief,
    ResearchNarrative,
    SignalView,
)
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed
from swing_trader.prediction_evaluator import (
    PredictionCloseEvaluator,
    latest_closed_trading_date,
)
from swing_trader.prediction_ledger import PredictionLedger
from swing_trader.schemas import Mode


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def _brief() -> ResearchBrief:
    section = NarrativeSection(title="structure", analysis="Evidence-bound analysis.")
    return ResearchBrief(
        as_of=NOW,
        trading_date="2026-07-16",
        mode=Mode.PAPER,
        freshness=FreshnessInfo(),
        signals_today=[
            SignalView(
                signal_id="sig-vst",
                symbol="VST",
                direction="long",
                confidence=0.68,
                source_agent="debate",
                thesis="debate verdict",
                baseline_value=165.0,
                as_of_bar=NOW.isoformat(),
            )
        ],
        narrative=ResearchNarrative(
            generated_at=NOW,
            market="US",
            language="zh-CN",
            model="primary-test",
            prompt_version="brief-v3",
            evidence_hash="evaluation-packet",
            edition="morning",
            headline="US view",
            summary="A measurable view.",
            sections=[section, section, section, section],
            claims=[
                ForecastClaim(
                    entity_type="instrument",
                    entity_key="VST",
                    claim_type="swing_direction",
                    direction="long",
                    confidence=0.72,
                    horizons=[1, 3, 5, 10, 20],
                    thesis="power demand and trend support the view",
                    invalidation="close below support",
                    benchmark="SPY",
                    expected_condition="positive excess return",
                    evidence_refs=["debate"],
                )
            ],
        ),
    )


def _bar(symbol: str, day: int, close: float, *, high: float, low: float) -> Bar:
    return Bar(
        symbol=symbol,
        # 20:00 UTC is the regular US close in July and keeps the ET date.
        ts=datetime(2026, 7, day, 20, 0, tzinfo=timezone.utc),
        open=close,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


class _Feed(DataFeed):
    def __init__(self, missing_due: bool = False) -> None:
        self.missing_due = missing_due
        self.calls: list[str] = []

    def get_bars(self, symbol: str, timeframe: str = "1d", limit: int = 100):
        self.calls.append(symbol)
        rows = {
            "VST": [
                _bar("VST", 16, 165.0, high=166.0, low=163.0),
                _bar("VST", 17, 181.5, high=183.15, low=160.0),
            ],
            "SPY": [
                _bar("SPY", 16, 100.0, high=101.0, low=99.0),
                _bar("SPY", 17, 102.0, high=103.0, low=98.0),
            ],
        }.get(symbol)
        if rows is None:
            raise DataFeedError(f"no {symbol}")
        return rows[:-1] if self.missing_due else rows

    def get_quote(self, symbol):  # pragma: no cover - evaluator never calls it
        raise AssertionError(symbol)

    def get_news(self, symbol=None, limit=20):  # pragma: no cover
        raise AssertionError(symbol)


def test_latest_closed_session_observes_market_delay_and_weekend():
    assert latest_closed_trading_date(
        "US", datetime(2026, 7, 17, 19, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 16)
    assert latest_closed_trading_date(
        "US", datetime(2026, 7, 17, 21, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 17)
    assert latest_closed_trading_date(
        "US", datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    ) == date(2026, 7, 17)


def test_close_worker_scores_due_paths_and_aggregate_is_idempotent(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    ledger.record_brief("us", _brief())
    feed = _Feed()
    evaluator = PredictionCloseEvaluator(ledger, feed)

    report = evaluator.run_market("us", "2026-07-17")
    assert report.due == 2
    assert report.evaluated == 2
    assert report.deferred == 0
    # Both forecast producers share the fetched VST path; SPY is cached too.
    assert feed.calls == ["VST", "SPY"]

    summary = ledger.aggregate_statistics(due_as_of="2026-07-17")
    assert summary["overview"]["evaluated_checkpoints"] == 2
    assert summary["overview"]["directional_samples"] == 2
    assert summary["overview"]["directional_accuracy"] == 1.0
    assert summary["overview"]["sample_mature"] is False
    assert summary["recent_evaluations"][0]["return_pct"] == pytest.approx(10.0)
    assert summary["recent_evaluations"][0]["excess_return_pct"] == pytest.approx(8.0)
    assert {row["key"] for row in summary["by_horizon"]} == {"1"}
    # The metrics retain both producers, while the dashboard read model
    # collapses their identical market/entity/claim row.
    assert len(summary["active_forecasts"]) == 1

    replay = evaluator.run_market("us", "2026-07-17")
    assert replay.due == 0
    assert ledger.aggregate_statistics()["overview"]["evaluated_checkpoints"] == 2


def test_missing_close_is_deferred_not_scored_as_failure(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    ledger.record_brief("us", _brief())
    report = PredictionCloseEvaluator(ledger, _Feed(missing_due=True)).run_market(
        "us", "2026-07-17"
    )
    assert report.due == 2
    assert report.deferred == 2
    summary = ledger.aggregate_statistics(due_as_of="2026-07-17")
    assert summary["overview"]["evaluated_checkpoints"] == 0
    assert summary["overview"]["due_checkpoints"] == 2
