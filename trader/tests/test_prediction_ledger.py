from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import SQLModel

from swing_trader.brief import (
    ForecastClaim,
    FreshnessInfo,
    NarrativeSection,
    ResearchBrief,
    ResearchNarrative,
    SignalView,
)
from swing_trader.brief_store import BriefStore
from swing_trader.prediction_ledger import (
    PREDICTION_METADATA,
    PredictionLedger,
    backfill_brief_history,
)
from swing_trader.schemas import Mode


NOW = datetime(2026, 7, 16, 1, 0, tzinfo=timezone.utc)


def _brief(
    *,
    at: datetime = NOW,
    direction: str = "long",
    evidence_hash: str = "evidence-1",
) -> ResearchBrief:
    claim = ForecastClaim(
        entity_type="instrument",
        entity_key="VST",
        claim_type="swing_direction",
        direction=direction,
        confidence=0.72,
        horizons=[1, 3, 5, 10, 20],
        thesis="power demand and trend support the view",
        invalidation="close below the supplied support level",
        benchmark="SPY",
        expected_condition="positive excess return",
        evidence_refs=["debate", "https://example.com/vst"],
    )
    section = NarrativeSection(title="structure", analysis="Evidence-bound analysis.")
    return ResearchBrief(
        as_of=at,
        trading_date=at.date().isoformat(),
        mode=Mode.PAPER,
        freshness=FreshnessInfo(),
        signals_today=[
            SignalView(
                signal_id="sig-vst",
                symbol="VST",
                direction=direction,
                confidence=0.68,
                source_agent="debate",
                thesis="debate verdict",
                baseline_value=165.0,
                as_of_bar=at.isoformat(),
            )
        ],
        narrative=ResearchNarrative(
            generated_at=at,
            market="US",
            language="zh-CN",
            model="primary-test",
            prompt_version="brief-v3",
            evidence_hash=evidence_hash,
            edition="morning",
            headline="US view",
            summary="A measurable view.",
            sections=[section, section, section, section],
            claims=[claim],
        ),
    )


def test_cross_day_revision_series_preserves_history(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    first = ledger.record_brief("us", _brief())
    assert first.replayed is False
    assert first.revisions == 2  # primary claim + debate signal
    assert first.checkpoints == 10
    assert first.evidence_links == 3

    replay = ledger.record_brief("us", _brief())
    assert replay.replayed is True and replay.revisions == 0

    later = _brief(
        at=NOW + timedelta(days=1),
        direction="neutral",
        evidence_hash="evidence-2",
    )
    second = ledger.record_brief("us", later)
    assert second.replayed is False and second.revisions == 2

    primary = ledger.list_series(
        market="us", entity_key="VST", producer="primary_brief:primary-test"
    )
    assert len(primary) == 1
    history = ledger.get_series_history(primary[0].id)
    assert history is not None
    revisions = history["revisions"]
    assert [row["direction"] for row in revisions] == ["long", "neutral"]
    assert revisions[1]["supersedes_revision_id"] == revisions[0]["id"]
    assert len(history["checkpoints"]) == 10
    assert {row["horizon_sessions"] for row in history["checkpoints"]} == {
        1, 3, 5, 10, 20
    }


def test_signal_and_primary_model_are_separate_comparable_series(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    ledger.record_brief("us", _brief())
    rows = ledger.list_series(entity_key="VST")
    assert {row.producer for row in rows} == {
        "primary_brief:primary-test",
        "signal:debate",
    }


def test_changed_packet_does_not_repeat_unchanged_claim_revisions(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    first = ledger.record_brief("us", _brief(evidence_hash="packet-1"))
    assert first.revisions == 2

    # The broader packet changed, so this is a distinct observation run, but
    # neither the primary claim nor Debate signal changed. No new revision or
    # evaluation checkpoint should be manufactured for either series.
    second = ledger.record_brief("us", _brief(evidence_hash="packet-2"))
    assert second.replayed is False
    assert second.revisions == 0
    assert second.checkpoints == 0
    assert second.evidence_links == 0
    assert second.unchanged_revisions == 2

    for series in ledger.list_series(entity_key="VST"):
        history = ledger.get_series_history(series.id)
        assert len(history["revisions"]) == 1


def test_prediction_tables_are_isolated_from_trading_metadata(tmp_path):
    PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    assert "forecast_series" in PREDICTION_METADATA.tables
    assert "forecast_series" not in SQLModel.metadata.tables


def test_historical_brief_backfill_is_ordered_and_idempotent(tmp_path):
    briefs = BriefStore(f"sqlite:///{tmp_path / 'briefs.db'}")
    briefs.save("us", _brief().model_dump(mode="json"))
    briefs.save(
        "us",
        _brief(
            at=NOW + timedelta(days=1),
            direction="neutral",
            evidence_hash="evidence-2",
        ).model_dump(mode="json"),
    )
    predictions = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")

    first = backfill_brief_history(briefs, predictions)
    assert first.snapshots == 2 and first.new_runs == 2
    assert first.revisions == 4
    second = backfill_brief_history(briefs, predictions)
    assert second.new_runs == 0 and second.replayed_runs == 2


def test_checkpoints_use_market_trading_sessions_and_due_queue(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    ledger.record_brief("us", _brief())
    primary = ledger.list_series(
        entity_key="VST", producer="primary_brief:primary-test"
    )[0]
    history = ledger.get_series_history(primary.id)
    due_by_horizon = {
        row["horizon_sessions"]: row["due_trading_date"]
        for row in history["checkpoints"]
    }
    assert due_by_horizon[1] == "2026-07-17"
    assert due_by_horizon[3] == "2026-07-21"  # weekend skipped
    assert due_by_horizon[5] == "2026-07-23"

    due = ledger.list_due_checkpoints(
        due_on_or_before="2026-07-17", market="us"
    )
    assert len(due) == 2  # primary-model + debate-signal one-session checks
    assert {row["checkpoint"]["horizon_sessions"] for row in due} == {1}
    with pytest.raises(ValueError):
        ledger.list_due_checkpoints(due_on_or_before="not-a-date")


def test_checkpoint_evaluation_is_versioned_idempotent_and_visible(tmp_path):
    ledger = PredictionLedger(f"sqlite:///{tmp_path / 'predictions.db'}")
    ledger.record_brief("us", _brief())
    primary = ledger.list_series(
        entity_key="VST", producer="primary_brief:primary-test"
    )[0]
    history = ledger.get_series_history(primary.id)
    checkpoint = next(
        row for row in history["checkpoints"] if row["horizon_sessions"] == 1
    )

    first = ledger.evaluate_checkpoint(
        checkpoint["id"],
        observed_at="2026-07-17T20:00:00+00:00",
        source="adjusted-close:test",
        entity_value=181.5,
        benchmark_return_pct=2.0,
        mfe_pct=11.0,
        mae_pct=-1.5,
    )
    assert first.replayed is False
    assert first.state == "confirmed"
    replay = ledger.evaluate_checkpoint(
        checkpoint["id"],
        observed_at="2026-07-17T20:00:00+00:00",
        source="adjusted-close:test",
        entity_value=181.5,
        benchmark_return_pct=2.0,
        mfe_pct=11.0,
        mae_pct=-1.5,
    )
    assert replay.replayed is True
    assert replay.evaluation_id == first.evaluation_id

    updated = ledger.get_series_history(primary.id)
    assert len(updated["outcomes"]) == 1
    assert updated["outcomes"][0]["return_pct"] == pytest.approx(10.0)
    assert updated["outcomes"][0]["excess_return_pct"] == pytest.approx(8.0)
    assert len(updated["evaluations"]) == 1
    assert updated["evaluations"][0]["absolute_direction_hit"] is True
    assert updated["evaluations"][0]["brier_score"] == pytest.approx((0.72 - 1.0) ** 2)
    remaining_ids = {
        row["checkpoint"]["id"]
        for row in ledger.list_due_checkpoints(
            due_on_or_before="2026-07-17", market="us"
        )
    }
    assert checkpoint["id"] not in remaining_ids
