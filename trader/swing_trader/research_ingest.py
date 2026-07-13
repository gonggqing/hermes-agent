"""Deeper research ingestion (Loop.md §5.10, Phase 0.75).

Beyond daily news, this archives **fundamentals** and **earnings-calendar**
documents into the knowledge store so RAG retrieval has real, citable substance
when the LLM analyses a symbol. Same guarantees as
:func:`~swing_trader.knowledge_pipeline.ingest_news_snapshot`:

- **provenance-mandatory** — every document carries a source URL + publisher
  (Yahoo Finance) or it is skipped;
- **content-hash deduped** — unchanged fundamentals re-ingested day after day
  count as duplicates and are NOT re-indexed; a new document appears only when
  the numbers change (fresh P/E, new earnings date);
- **fail-closed** — a vector outage stops indexing but keeps writing
  documents/facts; this function never raises into the trading loop.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Optional

from swing_trader.knowledge import (
    DocType,
    KnowledgeUnavailable,
    LicenseStatus,
    ResearchDocument,
)
from swing_trader.knowledge_pipeline import IngestReport
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = [
    "build_earnings_doc",
    "build_fundamentals_doc",
    "ingest_research_documents",
]

FUNDAMENTALS_PARSER_VERSION = "fundamentals-v1"
EARNINGS_PARSER_VERSION = "earnings-cal-v1"
FACTS_KIND_RESEARCH = "research"
_PUBLISHER = "Yahoo Finance"

#: (metric key, "template {}") pairs rendered into the fundamentals doc text.
_FUND_FIELDS: tuple[tuple[str, str], ...] = (
    ("pe", "trailing P/E {}"),
    ("fwd_pe", "forward P/E {}"),
    ("rev_growth_pct", "revenue growth {}%"),
    ("gross_margin_pct", "gross margin {}%"),
    ("profit_margin_pct", "profit margin {}%"),
    ("earnings_growth_pct", "earnings growth {}%"),
)


def _yahoo_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/quote/{symbol}"


def _now(retrieved_at: Optional[datetime]) -> datetime:
    return retrieved_at or datetime.now(timezone.utc)


def build_fundamentals_doc(
    symbol: str,
    metrics: dict,
    trading_date: date,
    retrieved_at: Optional[datetime] = None,
) -> Optional[ResearchDocument]:
    """A fundamentals research document, or None when there is no usable metric.

    Text is a compact, citable one-liner; dedupe on content means it is only
    re-indexed when the numbers actually change."""
    parts: list[str] = []
    for key, tmpl in _FUND_FIELDS:
        v = metrics.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            parts.append(tmpl.format(f"{v:g}"))
    if not parts:
        return None
    name = metrics.get("name")
    label = f"{symbol} ({name})" if isinstance(name, str) and name else symbol
    text = f"{label} fundamentals as of {trading_date.isoformat()}: " + "; ".join(parts) + "."
    at = _now(retrieved_at)
    return ResearchDocument(
        title=f"{symbol} fundamentals {trading_date.isoformat()}",
        text=text,
        source_url=_yahoo_url(symbol),
        publisher=_PUBLISHER,
        retrieved_at=at,
        symbols=[symbol.upper()],
        event_ts=at,
        trading_date_et=trading_date,
        doc_type=DocType.RESEARCH,
        license_status=LicenseStatus.PUBLIC,
        parser_version=FUNDAMENTALS_PARSER_VERSION,
    )


def build_earnings_doc(
    symbol: str,
    earnings_date: str,
    days_until: int,
    trading_date: date,
    retrieved_at: Optional[datetime] = None,
) -> ResearchDocument:
    """An earnings-calendar research document (next scheduled report)."""
    at = _now(retrieved_at)
    text = (
        f"{symbol} next scheduled earnings report: {earnings_date} "
        f"(in {days_until} day(s) as of {trading_date.isoformat()})."
    )
    return ResearchDocument(
        title=f"{symbol} earnings {earnings_date}",
        text=text,
        source_url=_yahoo_url(symbol),
        publisher=_PUBLISHER,
        retrieved_at=at,
        symbols=[symbol.upper()],
        event_ts=at,
        trading_date_et=trading_date,
        doc_type=DocType.EARNINGS,
        license_status=LicenseStatus.PUBLIC,
        parser_version=EARNINGS_PARSER_VERSION,
    )


def ingest_research_documents(
    knowledge: Any,
    index: Any,
    docs: list[ResearchDocument],
    trading_date: date,
) -> IngestReport:
    """Ingest pre-built research documents (facts + documents + vector index).

    Mirrors :func:`ingest_news_snapshot`: provenance-mandatory, content-hash
    dedupe, best-effort indexing of NEW documents only, fail-closed on vector
    trouble. Never raises."""
    report = IngestReport()
    report.n_items = len(docs)
    documents = getattr(knowledge, "documents", None) or knowledge._documents  # noqa: SLF001
    facts = getattr(knowledge, "facts", None)

    vector_ok = False
    if index is None:
        report.warnings.append(
            "no vector index — research documents stored without semantic "
            "indexing (search fails closed, Loop.md §5.10)"
        )
    else:
        try:
            vector_ok = index.available()
        except KnowledgeUnavailable as exc:
            report.warnings.append(f"vector index down at batch start: {exc}")

    for doc in docs:
        if not doc.source_url or not doc.publisher:
            report.n_skipped_no_provenance += 1
            continue
        before = documents.count()
        doc_id = documents.ingest(doc)
        if documents.count() > before:
            report.n_docs_written += 1
        else:
            report.n_duplicates += 1
            continue  # already indexed on a prior day — nothing more to do

        if facts is not None:
            facts.write(FACTS_KIND_RESEARCH, doc.model_dump(mode="json"), trading_date)

        if index is not None and vector_ok:
            try:
                index.index(
                    doc_id,
                    doc.text,
                    payload={
                        "document_id": doc_id,
                        "symbols": doc.symbols,
                        "doc_type": doc.doc_type.value,
                        "trading_date": trading_date.isoformat(),
                        "publisher": doc.publisher,
                    },
                )
                report.n_indexed += 1
            except KnowledgeUnavailable as exc:
                vector_ok = False
                report.warnings.append(
                    f"vector index failed mid-batch — remaining documents stored "
                    f"WITHOUT indexing (fail closed): {exc}"
                )

    report.vector_ok = vector_ok
    logger.info(
        "research documents ingested",
        extra={"n_docs": report.n_docs_written, "n_dupes": report.n_duplicates,
               "n_indexed": report.n_indexed, "vector_ok": report.vector_ok},
    )
    return report
