"""Append-only, cross-session Finance prediction ledger.

Briefs are presentation artifacts.  This database stores the measurable view
behind them as a durable series of revisions, evidence links and evaluation
checkpoints.  It is deliberately isolated from the trading Ledger and Qdrant.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import MetaData, event
from sqlmodel import Field, Session, SQLModel, create_engine, select

from swing_trader.brief import ForecastClaim, ResearchBrief
from swing_trader.log import get_logger

logger = get_logger(__name__)

PREDICTION_METADATA = MetaData()


class _PredictionTable(SQLModel):
    metadata = PREDICTION_METADATA


class ForecastRunRow(_PredictionTable, table=True):
    __tablename__ = "forecast_runs"

    id: str = Field(primary_key=True)
    run_key: str = Field(index=True, unique=True)
    market: str = Field(index=True)
    trading_date: str = Field(index=True)
    as_of: str = Field(index=True)
    edition: str = Field(default="", index=True)
    model: str = ""
    prompt_version: str = ""
    evidence_hash: str = Field(index=True)
    brief_snapshot_id: Optional[str] = Field(default=None, index=True)
    created_at: str = Field(index=True)


class ForecastSeriesRow(_PredictionTable, table=True):
    __tablename__ = "forecast_series"

    id: str = Field(primary_key=True)
    series_key: str = Field(index=True, unique=True)
    market: str = Field(index=True)
    entity_type: str = Field(index=True)
    entity_key: str = Field(index=True)
    claim_type: str = Field(index=True)
    producer: str = Field(index=True)
    status: str = Field(default="active", index=True)
    opened_at: str = Field(index=True)
    last_updated_at: str = Field(index=True)
    latest_revision_id: Optional[str] = Field(default=None, index=True)
    closed_at: Optional[str] = Field(default=None, index=True)


class ForecastRevisionRow(_PredictionTable, table=True):
    __tablename__ = "forecast_revisions"

    id: str = Field(primary_key=True)
    series_id: str = Field(index=True)
    run_id: str = Field(index=True)
    supersedes_revision_id: Optional[str] = Field(default=None, index=True)
    as_of: str = Field(index=True)
    direction: str = Field(index=True)
    confidence: Optional[float] = None
    thesis: str = ""
    invalidation: str = ""
    expected_condition: str = ""
    benchmark: str = ""
    baseline_value: Optional[float] = None
    research_score: Optional[float] = None
    research_rank: Optional[int] = None
    producer_version: str = Field(default="", index=True)
    payload_json: str = "{}"
    created_at: str = Field(index=True)


class ForecastCheckpointRow(_PredictionTable, table=True):
    __tablename__ = "forecast_checkpoints"

    id: str = Field(primary_key=True)
    revision_id: str = Field(index=True)
    horizon_sessions: int = Field(index=True)
    due_trading_date: Optional[str] = Field(default=None, index=True)
    status: str = Field(default="pending", index=True)
    evaluated_at: Optional[str] = Field(default=None, index=True)


class ForecastEvidenceRow(_PredictionTable, table=True):
    __tablename__ = "forecast_evidence_links"

    id: str = Field(primary_key=True)
    revision_id: str = Field(index=True)
    evidence_type: str = Field(index=True)
    source_id: str = Field(default="", index=True)
    source_url: str = ""
    observed_at: Optional[str] = Field(default=None, index=True)
    stance: str = "support"
    weight: Optional[float] = None
    payload_json: str = "{}"


class OutcomeObservationRow(_PredictionTable, table=True):
    __tablename__ = "forecast_outcomes"

    id: str = Field(primary_key=True)
    checkpoint_id: str = Field(index=True)
    observed_at: str = Field(index=True)
    source: str = Field(index=True)
    entity_value: Optional[float] = None
    benchmark_value: Optional[float] = None
    return_pct: Optional[float] = None
    benchmark_return_pct: Optional[float] = None
    excess_return_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None
    payload_json: str = "{}"


class ForecastEvaluationRow(_PredictionTable, table=True):
    __tablename__ = "forecast_evaluations"

    id: str = Field(primary_key=True)
    checkpoint_id: str = Field(index=True)
    outcome_id: str = Field(index=True)
    evaluator_version: str = Field(index=True)
    evaluated_at: str = Field(index=True)
    state: str = Field(index=True)
    absolute_direction_hit: Optional[bool] = None
    excess_direction_hit: Optional[bool] = None
    brier_score: Optional[float] = None
    log_loss: Optional[float] = None
    score_json: str = "{}"


@dataclass(frozen=True)
class ForecastRecordReport:
    run_id: str
    replayed: bool
    revisions: int
    checkpoints: int
    evidence_links: int
    unchanged_revisions: int = 0


@dataclass(frozen=True)
class ForecastBackfillReport:
    snapshots: int
    new_runs: int
    replayed_runs: int
    revisions: int
    unchanged_revisions: int


@dataclass(frozen=True)
class ForecastEvaluationReport:
    checkpoint_id: str
    outcome_id: str
    evaluation_id: str
    state: str
    replayed: bool


@dataclass(frozen=True)
class _ClaimInput:
    entity_type: str
    entity_key: str
    claim_type: str
    producer: str
    direction: str
    confidence: Optional[float]
    horizons: tuple[int, ...]
    thesis: str
    invalidation: str = ""
    expected_condition: str = ""
    benchmark: str = ""
    baseline_value: Optional[float] = None
    research_score: Optional[float] = None
    research_rank: Optional[int] = None
    producer_version: str = ""
    evidence: tuple[dict[str, Any], ...] = ()
    payload: Optional[dict[str, Any]] = None


def _utc_iso(value: datetime | str | None = None) -> str:
    if value is None:
        return datetime.now(timezone.utc).isoformat()
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("prediction timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stable_evidence(brief: ResearchBrief) -> dict[str, Any]:
    value = brief.model_dump(mode="json")
    value.pop("as_of", None)
    freshness = value.get("freshness") or {}
    for key in (
        "market_as_of", "news_as_of", "portfolio_as_of",
        "market_age_minutes", "news_age_minutes", "portfolio_age_minutes",
    ):
        freshness.pop(key, None)
    narrative = value.get("narrative")
    if isinstance(narrative, dict):
        narrative.pop("generated_at", None)
    return value


def _series_key(market: str, claim: _ClaimInput) -> str:
    entity_key = (
        claim.entity_key.strip().upper()
        if claim.entity_type == "instrument"
        else claim.entity_key.strip()
    )
    return _stable_hash(
        [market, claim.entity_type, entity_key, claim.claim_type, claim.producer]
    )


def _market_schedule(market: str):
    from swing_trader.scheduler import (
        CN_SCHEDULE,
        HK_SCHEDULE,
        KR_SCHEDULE,
        US_SCHEDULE,
    )

    return {
        "CN": CN_SCHEDULE,
        "HK": HK_SCHEDULE,
        "KR": KR_SCHEDULE,
        "US": US_SCHEDULE,
    }.get(market.upper(), US_SCHEDULE)


def _advance_trading_sessions(start: date, sessions: int, market: str) -> date:
    """Advance strictly after ``start`` by market trading sessions."""
    if sessions < 1:
        raise ValueError("sessions must be positive")
    from swing_trader.scheduler import is_trading_day

    schedule = _market_schedule(market)
    current = start
    remaining = sessions
    while remaining:
        current += timedelta(days=1)
        if is_trading_day(current, schedule):
            remaining -= 1
    return current


def _direction_hit(direction: str, value: float | None) -> bool | None:
    if value is None:
        return None
    normalized = direction.strip().lower()
    if normalized in {"long", "buy", "positive", "risk_on", "bullish"}:
        return value > 0.0
    if normalized in {"short", "sell", "negative", "risk_off", "bearish"}:
        return value < 0.0
    return None


def _claim_fingerprint(claim: _ClaimInput) -> str:
    return _stable_hash(
        {
            "direction": claim.direction,
            "confidence": claim.confidence,
            "horizons": sorted(claim.horizons),
            "thesis": claim.thesis,
            "invalidation": claim.invalidation,
            "expected_condition": claim.expected_condition,
            "benchmark": claim.benchmark,
            "baseline_value": claim.baseline_value,
            "research_score": claim.research_score,
            "research_rank": claim.research_rank,
            "producer_version": claim.producer_version,
            "payload": claim.payload or {},
            "evidence": sorted(
                claim.evidence,
                key=lambda item: json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                ),
            ),
        }
    )


class PredictionLedger:
    """Independent SQLite ledger for longitudinal research evaluation."""

    def __init__(self, url: str = "sqlite:///prediction_evaluation.db") -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self._engine = create_engine(url, connect_args=connect_args)
        if url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        PREDICTION_METADATA.create_all(self._engine)
        self._backfill_checkpoint_dates()

    def _backfill_checkpoint_dates(self) -> None:
        """Fill derived due dates for databases created before this field was wired."""
        with Session(self._engine) as session:
            rows = list(
                session.exec(
                    select(ForecastCheckpointRow).where(
                        ForecastCheckpointRow.due_trading_date.is_(None)
                    )
                ).all()
            )
            changed = 0
            for checkpoint in rows:
                revision = session.get(ForecastRevisionRow, checkpoint.revision_id)
                if revision is None:
                    continue
                series = session.get(ForecastSeriesRow, revision.series_id)
                run = session.get(ForecastRunRow, revision.run_id)
                if series is None or run is None:
                    continue
                checkpoint.due_trading_date = _advance_trading_sessions(
                    date.fromisoformat(run.trading_date),
                    checkpoint.horizon_sessions,
                    series.market,
                ).isoformat()
                session.add(checkpoint)
                changed += 1
            if changed:
                session.commit()
                logger.info("prediction checkpoint dates backfilled", extra={"count": changed})

    def record_brief(
        self,
        market: str,
        brief: ResearchBrief | dict,
        *,
        brief_snapshot_id: str | None = None,
    ) -> ForecastRecordReport:
        parsed = brief if isinstance(brief, ResearchBrief) else ResearchBrief.model_validate(brief)
        market = market.strip().upper()
        narrative = parsed.narrative
        evidence_hash = (
            narrative.evidence_hash if narrative and narrative.evidence_hash
            else _stable_hash(_stable_evidence(parsed))
        )
        edition = narrative.edition if narrative else ""
        model = narrative.model if narrative else ""
        prompt_version = narrative.prompt_version if narrative else ""
        run_key = _stable_hash(
            [market, parsed.trading_date, edition, model, prompt_version, evidence_hash]
        )
        with Session(self._engine) as session:
            existing = session.exec(
                select(ForecastRunRow).where(ForecastRunRow.run_key == run_key)
            ).first()
            if existing is not None:
                return ForecastRecordReport(existing.id, True, 0, 0, 0)

            now = _utc_iso()
            run = ForecastRunRow(
                id=uuid.uuid4().hex,
                run_key=run_key,
                market=market,
                trading_date=parsed.trading_date,
                as_of=_utc_iso(parsed.as_of),
                edition=edition,
                model=model,
                prompt_version=prompt_version,
                evidence_hash=evidence_hash,
                brief_snapshot_id=brief_snapshot_id,
                created_at=now,
            )
            run_id = run.id
            session.add(run)
            session.flush()

            revisions = checkpoints = evidence_links = unchanged_revisions = 0
            for claim in self._claims(parsed):
                series_key = _series_key(market, claim)
                entity_key = (
                    claim.entity_key.strip().upper()
                    if claim.entity_type == "instrument"
                    else claim.entity_key.strip()
                )
                series = session.exec(
                    select(ForecastSeriesRow).where(
                        ForecastSeriesRow.series_key == series_key
                    )
                ).first()
                if series is None:
                    series = ForecastSeriesRow(
                        id=uuid.uuid4().hex,
                        series_key=series_key,
                        market=market,
                        entity_type=claim.entity_type,
                        entity_key=entity_key,
                        claim_type=claim.claim_type,
                        producer=claim.producer,
                        opened_at=run.as_of,
                        last_updated_at=run.as_of,
                    )
                    session.add(series)
                    session.flush()
                elif series.latest_revision_id:
                    latest = session.get(
                        ForecastRevisionRow, series.latest_revision_id
                    )
                    if latest is not None and self._revision_fingerprint(
                        session, latest
                    ) == _claim_fingerprint(claim):
                        unchanged_revisions += 1
                        continue
                revision = ForecastRevisionRow(
                    id=uuid.uuid4().hex,
                    series_id=series.id,
                    run_id=run.id,
                    supersedes_revision_id=series.latest_revision_id,
                    as_of=run.as_of,
                    direction=claim.direction,
                    confidence=claim.confidence,
                    thesis=claim.thesis,
                    invalidation=claim.invalidation,
                    expected_condition=claim.expected_condition,
                    benchmark=claim.benchmark,
                    baseline_value=claim.baseline_value,
                    research_score=claim.research_score,
                    research_rank=claim.research_rank,
                    producer_version=claim.producer_version,
                    payload_json=json.dumps(claim.payload or {}, ensure_ascii=False, default=str),
                    created_at=now,
                )
                session.add(revision)
                series.latest_revision_id = revision.id
                series.last_updated_at = run.as_of
                session.add(series)
                revisions += 1
                for horizon in claim.horizons:
                    session.add(
                        ForecastCheckpointRow(
                            id=uuid.uuid5(
                                uuid.NAMESPACE_URL, f"{revision.id}:{horizon}"
                            ).hex,
                            revision_id=revision.id,
                            horizon_sessions=horizon,
                            due_trading_date=_advance_trading_sessions(
                                date.fromisoformat(parsed.trading_date),
                                horizon,
                                market,
                            ).isoformat(),
                        )
                    )
                    checkpoints += 1
                for item in claim.evidence:
                    session.add(
                        ForecastEvidenceRow(
                            id=uuid.uuid4().hex,
                            revision_id=revision.id,
                            evidence_type=str(item.get("type") or "reference"),
                            source_id=str(item.get("source_id") or ""),
                            source_url=str(item.get("url") or ""),
                            observed_at=(
                                _utc_iso(item["observed_at"])
                                if item.get("observed_at") else None
                            ),
                            stance=str(item.get("stance") or "support"),
                            weight=item.get("weight"),
                            payload_json=json.dumps(item, ensure_ascii=False, default=str),
                        )
                    )
                    evidence_links += 1
            session.commit()
        return ForecastRecordReport(
            run_id,
            False,
            revisions,
            checkpoints,
            evidence_links,
            unchanged_revisions,
        )

    @staticmethod
    def _revision_fingerprint(
        session: Session, revision: ForecastRevisionRow
    ) -> str:
        checkpoints = session.exec(
            select(ForecastCheckpointRow).where(
                ForecastCheckpointRow.revision_id == revision.id
            )
        ).all()
        evidence_rows = session.exec(
            select(ForecastEvidenceRow).where(
                ForecastEvidenceRow.revision_id == revision.id
            )
        ).all()
        evidence = []
        for row in evidence_rows:
            try:
                evidence.append(json.loads(row.payload_json))
            except (TypeError, json.JSONDecodeError):
                evidence.append(
                    {
                        "type": row.evidence_type,
                        "source_id": row.source_id,
                        "url": row.source_url,
                        "observed_at": row.observed_at,
                        "stance": row.stance,
                        "weight": row.weight,
                    }
                )
        try:
            payload = json.loads(revision.payload_json)
        except (TypeError, json.JSONDecodeError):
            payload = {}
        return _stable_hash(
            {
                "direction": revision.direction,
                "confidence": revision.confidence,
                "horizons": sorted(row.horizon_sessions for row in checkpoints),
                "thesis": revision.thesis,
                "invalidation": revision.invalidation,
                "expected_condition": revision.expected_condition,
                "benchmark": revision.benchmark,
                "baseline_value": revision.baseline_value,
                "research_score": revision.research_score,
                "research_rank": revision.research_rank,
                "producer_version": revision.producer_version,
                "payload": payload,
                "evidence": sorted(
                    evidence,
                    key=lambda item: json.dumps(
                        item, ensure_ascii=False, sort_keys=True, default=str
                    ),
                ),
            }
        )

    def _claims(self, brief: ResearchBrief) -> list[_ClaimInput]:
        claims: list[_ClaimInput] = []
        baseline_by_symbol = {
            signal.symbol.upper(): signal.baseline_value
            for signal in brief.signals_today
            if signal.baseline_value is not None
        }
        for mover in [*brief.movers.top, *brief.movers.bottom]:
            baseline_by_symbol.setdefault(mover.symbol.upper(), mover.last)
        narrative = brief.narrative
        if narrative is not None:
            for claim in narrative.claims:
                claims.append(
                    self._narrative_claim(
                        claim,
                        narrative.model,
                        narrative.prompt_version,
                        baseline_value=baseline_by_symbol.get(claim.entity_key.upper()),
                    )
                )

        for signal in brief.signals_today:
            claims.append(
                _ClaimInput(
                    entity_type="instrument",
                    entity_key=signal.symbol,
                    claim_type="swing_direction",
                    producer=f"signal:{signal.source_agent}",
                    direction=signal.direction,
                    confidence=signal.confidence,
                    horizons=(1, 3, 5, 10, 20),
                    thesis=signal.thesis,
                    baseline_value=signal.baseline_value,
                    producer_version=signal.source_agent,
                    evidence=({
                        "type": "signal",
                        "source_id": signal.signal_id,
                        "observed_at": signal.as_of_bar,
                        "stance": "support",
                    },),
                    payload=signal.model_dump(mode="json"),
                )
            )

        if brief.regime is not None:
            claims.append(
                _ClaimInput(
                    entity_type="market",
                    entity_key=(narrative.market if narrative else "market"),
                    claim_type="market_regime",
                    producer="market_monitor",
                    direction=brief.regime.risk_on_off,
                    confidence=None,
                    horizons=(1, 3, 5),
                    thesis=(
                        f"regime={brief.regime.risk_on_off}; vix={brief.regime.vix}; "
                        f"breadth={brief.regime.breadth_pct_above_50dma}"
                    ),
                    payload=brief.regime.model_dump(mode="json"),
                )
            )

        for theme in brief.themes:
            distance = theme.avg_dist_sma50_pct
            claims.append(
                _ClaimInput(
                    entity_type="theme",
                    entity_key=theme.theme,
                    claim_type="theme_momentum",
                    producer="theme_aggregation",
                    direction="positive" if distance > 0 else "negative" if distance < 0 else "neutral",
                    confidence=min(1.0, abs(distance) / 10.0),
                    horizons=(5, 20, 60),
                    thesis=f"theme average distance to SMA50 is {distance:.4g}%",
                    research_score=distance,
                    payload=theme.model_dump(mode="json"),
                )
            )

        if brief.discovery is not None:
            for candidate in brief.discovery.candidates:
                evidence = tuple(
                    {
                        "type": f"discovery:{item.kind.value}",
                        "source_id": item.source,
                        "url": item.url,
                        "observed_at": item.observed_at,
                        "weight": item.confidence,
                    }
                    for item in candidate.evidence
                )
                claims.append(
                    _ClaimInput(
                        entity_type="instrument",
                        entity_key=candidate.symbol,
                        claim_type="discovery_priority",
                        producer="discovery:scanner",
                        direction="watch",
                        confidence=candidate.score / 100.0,
                        horizons=(5, 20, 60),
                        thesis="; ".join(candidate.reasons) or candidate.relationship,
                        research_score=candidate.score,
                        research_rank=candidate.rank,
                        producer_version="discovery-v1",
                        evidence=evidence,
                        payload=candidate.model_dump(mode="json"),
                    )
                )
        # One producer may accidentally emit the same logical claim twice in
        # one brief. Keep the higher-confidence record so a single run creates
        # at most one revision per longitudinal series.
        deduped: dict[str, _ClaimInput] = {}
        for claim in claims:
            key = _series_key("", claim)
            previous = deduped.get(key)
            if previous is None or (claim.confidence or -1.0) > (previous.confidence or -1.0):
                deduped[key] = claim
        return list(deduped.values())

    @staticmethod
    def _narrative_claim(
        claim: ForecastClaim,
        model: str,
        prompt_version: str,
        *,
        baseline_value: float | None,
    ) -> _ClaimInput:
        evidence = tuple(
            {
                "type": "model_reference",
                "source_id": ref,
                "url": ref if ref.startswith(("http://", "https://")) else "",
            }
            for ref in claim.evidence_refs
        )
        return _ClaimInput(
            entity_type=claim.entity_type,
            entity_key=claim.entity_key,
            claim_type=claim.claim_type,
            producer=f"primary_brief:{model}",
            direction=claim.direction,
            confidence=claim.confidence,
            horizons=tuple(claim.horizons),
            thesis=claim.thesis,
            invalidation=claim.invalidation,
            expected_condition=claim.expected_condition,
            benchmark=claim.benchmark,
            baseline_value=baseline_value,
            producer_version=prompt_version,
            evidence=evidence,
            payload=claim.model_dump(mode="json"),
        )

    def list_series(
        self,
        *,
        market: str | None = None,
        entity_key: str | None = None,
        producer: str | None = None,
        limit: int = 200,
    ) -> list[ForecastSeriesRow]:
        with Session(self._engine) as session:
            statement = select(ForecastSeriesRow)
            if market:
                statement = statement.where(ForecastSeriesRow.market == market.upper())
            if entity_key:
                statement = statement.where(ForecastSeriesRow.entity_key == entity_key.upper())
            if producer:
                statement = statement.where(ForecastSeriesRow.producer == producer)
            statement = statement.order_by(ForecastSeriesRow.last_updated_at.desc()).limit(
                max(1, min(limit, 1000))
            )
            return list(session.exec(statement).all())

    def get_series_history(self, series_id: str) -> dict[str, Any] | None:
        with Session(self._engine) as session:
            series = session.get(ForecastSeriesRow, series_id)
            if series is None:
                return None
            revisions = list(
                session.exec(
                    select(ForecastRevisionRow)
                    .where(ForecastRevisionRow.series_id == series_id)
                    .order_by(ForecastRevisionRow.as_of.asc())
                ).all()
            )
            revision_ids = [row.id for row in revisions]
            checkpoints = []
            evidence = []
            outcomes = []
            evaluations = []
            for revision_id in revision_ids:
                checkpoints.extend(
                    session.exec(
                        select(ForecastCheckpointRow).where(
                            ForecastCheckpointRow.revision_id == revision_id
                        )
                    ).all()
                )
                evidence.extend(
                    session.exec(
                        select(ForecastEvidenceRow).where(
                            ForecastEvidenceRow.revision_id == revision_id
                        )
                    ).all()
                )
            for checkpoint in checkpoints:
                checkpoint_outcomes = list(
                    session.exec(
                        select(OutcomeObservationRow).where(
                            OutcomeObservationRow.checkpoint_id == checkpoint.id
                        )
                    ).all()
                )
                outcomes.extend(checkpoint_outcomes)
                for outcome in checkpoint_outcomes:
                    evaluations.extend(
                        session.exec(
                            select(ForecastEvaluationRow).where(
                                ForecastEvaluationRow.outcome_id == outcome.id
                            )
                        ).all()
                    )
            return {
                "series": series.model_dump(),
                "revisions": [row.model_dump() for row in revisions],
                "checkpoints": [row.model_dump() for row in checkpoints],
                "evidence": [row.model_dump() for row in evidence],
                "outcomes": [row.model_dump() for row in outcomes],
                "evaluations": [row.model_dump() for row in evaluations],
            }

    def list_due_checkpoints(
        self,
        *,
        due_on_or_before: date | str,
        market: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Pending checkpoints due by a market trading date, with claim context."""
        due_date = (
            due_on_or_before
            if isinstance(due_on_or_before, date)
            else date.fromisoformat(str(due_on_or_before))
        )
        due = due_date.isoformat()
        with Session(self._engine) as session:
            statement = (
                select(ForecastCheckpointRow)
                .where(ForecastCheckpointRow.status == "pending")
                .where(ForecastCheckpointRow.due_trading_date <= due)
                .order_by(ForecastCheckpointRow.due_trading_date.asc())
                .limit(max(1, min(limit, 5000)))
            )
            checkpoints = list(session.exec(statement).all())
            result = []
            for checkpoint in checkpoints:
                revision = session.get(ForecastRevisionRow, checkpoint.revision_id)
                if revision is None:
                    continue
                series = session.get(ForecastSeriesRow, revision.series_id)
                if series is None or (market and series.market != market.upper()):
                    continue
                result.append(
                    {
                        "checkpoint": checkpoint.model_dump(),
                        "revision": revision.model_dump(),
                        "series": series.model_dump(),
                    }
                )
            return result

    def evaluate_checkpoint(
        self,
        checkpoint_id: str,
        *,
        observed_at: datetime | str,
        source: str,
        entity_value: float | None = None,
        return_pct: float | None = None,
        benchmark_return_pct: float | None = None,
        mfe_pct: float | None = None,
        mae_pct: float | None = None,
        evaluator_version: str = "price-direction-v1",
        payload: dict[str, Any] | None = None,
    ) -> ForecastEvaluationReport:
        """Append a deterministic observation and score one due checkpoint.

        Market-data fetching stays outside this ledger.  The evaluator accepts
        observed values, computes reproducible return/calibration fields and
        records the evaluator version so scoring logic can evolve without
        rewriting history.
        """
        observed_iso = _utc_iso(observed_at)
        with Session(self._engine) as session:
            checkpoint = session.get(ForecastCheckpointRow, checkpoint_id)
            if checkpoint is None:
                raise ValueError(f"unknown checkpoint: {checkpoint_id}")
            revision = session.get(ForecastRevisionRow, checkpoint.revision_id)
            if revision is None:
                raise ValueError(f"checkpoint has no revision: {checkpoint_id}")

            if return_pct is None and entity_value is not None and revision.baseline_value not in (None, 0.0):
                return_pct = (entity_value / revision.baseline_value - 1.0) * 100.0
            excess_return_pct = (
                return_pct - benchmark_return_pct
                if return_pct is not None and benchmark_return_pct is not None
                else None
            )
            outcome_key = _stable_hash(
                [
                    checkpoint_id,
                    observed_iso,
                    source,
                    entity_value,
                    return_pct,
                    benchmark_return_pct,
                    mfe_pct,
                    mae_pct,
                    payload or {},
                ]
            )
            outcome_id = uuid.uuid5(uuid.NAMESPACE_URL, outcome_key).hex
            outcome = session.get(OutcomeObservationRow, outcome_id)
            if outcome is None:
                outcome = OutcomeObservationRow(
                    id=outcome_id,
                    checkpoint_id=checkpoint_id,
                    observed_at=observed_iso,
                    source=source,
                    entity_value=entity_value,
                    return_pct=return_pct,
                    benchmark_return_pct=benchmark_return_pct,
                    excess_return_pct=excess_return_pct,
                    mfe_pct=mfe_pct,
                    mae_pct=mae_pct,
                    payload_json=json.dumps(payload or {}, ensure_ascii=False, default=str),
                )
                session.add(outcome)

            evaluation_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"{checkpoint_id}:{outcome_id}:{evaluator_version}",
            ).hex
            existing = session.get(ForecastEvaluationRow, evaluation_id)
            if existing is not None:
                return ForecastEvaluationReport(
                    checkpoint_id,
                    outcome_id,
                    evaluation_id,
                    existing.state,
                    True,
                )

            absolute_hit = _direction_hit(revision.direction, return_pct)
            excess_hit = _direction_hit(revision.direction, excess_return_pct)
            brier_score = log_loss = None
            if absolute_hit is not None and revision.confidence is not None:
                probability = min(1.0 - 1e-12, max(1e-12, revision.confidence))
                observed = 1.0 if absolute_hit else 0.0
                brier_score = (probability - observed) ** 2
                log_loss = -math.log(probability if absolute_hit else 1.0 - probability)
            state = (
                "confirmed" if absolute_hit is True
                else "refuted" if absolute_hit is False
                else "observed"
            )
            evaluation = ForecastEvaluationRow(
                id=evaluation_id,
                checkpoint_id=checkpoint_id,
                outcome_id=outcome_id,
                evaluator_version=evaluator_version,
                evaluated_at=_utc_iso(),
                state=state,
                absolute_direction_hit=absolute_hit,
                excess_direction_hit=excess_hit,
                brier_score=brier_score,
                log_loss=log_loss,
                score_json=json.dumps(
                    {"direction": revision.direction, "confidence": revision.confidence},
                    ensure_ascii=False,
                ),
            )
            session.add(evaluation)
            checkpoint.status = "evaluated"
            checkpoint.evaluated_at = evaluation.evaluated_at
            session.add(checkpoint)
            session.commit()
            return ForecastEvaluationReport(
                checkpoint_id,
                outcome_id,
                evaluation_id,
                state,
                False,
            )


def persist_brief_artifacts(runtime, market: str, payload: dict) -> str | None:
    """Persist prose snapshot and structured forecasts independently.

    Either store may fail without preventing the other or the research loop.
    """
    snapshot_id = None
    brief_store = getattr(runtime, "brief_store", None)
    if brief_store is not None:
        try:
            snapshot_id = brief_store.save(market, payload)
        except Exception:
            logger.warning("brief snapshot archive failed", extra={"market": market})
    prediction_ledger = getattr(runtime, "prediction_ledger", None)
    if prediction_ledger is not None:
        try:
            report = prediction_ledger.record_brief(
                market, payload, brief_snapshot_id=snapshot_id
            )
            logger.info(
                "forecast revisions persisted",
                extra={
                    "market": market,
                    "run_id": report.run_id,
                    "replayed": report.replayed,
                    "revisions": report.revisions,
                    "unchanged_revisions": report.unchanged_revisions,
                },
            )
        except Exception:
            logger.exception("prediction ledger write failed", extra={"market": market})
    return snapshot_id


def backfill_brief_history(
    brief_store,
    prediction_ledger: PredictionLedger,
    *,
    limit: int = 10_000,
) -> ForecastBackfillReport:
    """Project durable historical briefs into forecast series oldest-first."""
    snapshots = new_runs = replayed_runs = revisions = unchanged_revisions = 0
    for snapshot_id, market, payload in brief_store.iter_snapshots(limit=limit):
        snapshots += 1
        try:
            report = prediction_ledger.record_brief(
                market, payload, brief_snapshot_id=snapshot_id
            )
        except Exception:
            logger.warning(
                "historical prediction backfill skipped invalid snapshot",
                extra={"snapshot_id": snapshot_id, "market": market},
            )
            continue
        if report.replayed:
            replayed_runs += 1
        else:
            new_runs += 1
            revisions += report.revisions
            unchanged_revisions += report.unchanged_revisions
    return ForecastBackfillReport(
        snapshots=snapshots,
        new_runs=new_runs,
        replayed_runs=replayed_runs,
        revisions=revisions,
        unchanged_revisions=unchanged_revisions,
    )
