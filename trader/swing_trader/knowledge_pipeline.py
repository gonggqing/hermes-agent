"""Knowledge ingestion pipeline — daily news → facts / documents / vector index.

Loop.md Phase 0.5 backlog item 5 (backend half): "Knowledge ingestion and
semantic search: daily research/news/earnings → facts/documents/vector
index; source-linked research search in Desktop/Web."

This module WIRES the existing three-layer knowledge store
(:mod:`swing_trader.knowledge`, Loop.md §5.10) to monitor output
(:class:`swing_trader.monitors.NewsSnapshot`); it reimplements nothing:

- :class:`KnowledgeConfig` / :func:`build_knowledge` — construct
  :class:`FactsArchive` + :class:`DocumentStore` + :class:`KnowledgeIndex`
  under one root directory (embedded qdrant ``path=`` mode by default, the
  private ``hermes-finance-vector`` container via ``qdrant_url``).
- :func:`ingest_news_snapshot` — each provenance-complete news item becomes
  a normalized ``news`` research document (layer 2, deduped by content
  hash) AND an append-only raw line in the facts archive (layer 1, retained
  by trading date, Loop.md §5.8/§5.10), then is indexed (layer 3) when the
  vector backend is up.
- :func:`search_knowledge` — JSON-ready, source-attributed semantic search
  over the facade (Loop.md §5.10: return links/snippets, never untraceable
  model facts).

FAIL-CLOSED semantics (Loop.md §5.10): the vector database is search
infrastructure, not an execution or storage dependency. If it cannot be
built or goes down mid-batch, :func:`build_knowledge` and
:func:`ingest_news_snapshot` NEVER raise — documents and facts keep being
written and the outage is logged/reported — while :func:`search_knowledge`
raises :class:`KnowledgeUnavailable` so research retrieval fails closed
(the API layer maps it to 503).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from swing_trader.knowledge import (
    COLLECTION_NAME,
    DocType,
    DocumentStore,
    FactsArchive,
    FinanceKnowledge,
    HashingEmbedder,
    KnowledgeIndex,
    KnowledgeUnavailable,
    LicenseStatus,
    ResearchDocument,
)
from swing_trader.log import get_logger

if TYPE_CHECKING:  # import-light: only needed for type checking
    from swing_trader.monitors import NewsSnapshot

__all__ = [
    "IngestReport",
    "KnowledgeConfig",
    "NEWS_PARSER_VERSION",
    "PipelineKnowledge",
    "build_knowledge",
    "ingest_news_snapshot",
    "search_knowledge",
]

logger = get_logger(__name__)

#: Parser version stamped on every news document (Loop.md §5.10 layer 2:
#: documents carry parser/model version so re-parses are distinguishable).
NEWS_PARSER_VERSION = "news-v1"

#: FactsArchive kind for raw news items (Loop.md §5.10 layer 1).
FACTS_KIND_NEWS = "news"


# ------------------------------------------------------------- configuration


@dataclass
class KnowledgeConfig:
    """Where the knowledge store lives (Loop.md §5.10).

    ``qdrant_url`` selects the dedicated private vector service (for
    example ``hermes-finance-vector``); when it is None the index runs
    embedded at ``qdrant_path`` (default ``root_dir / "vector"`` — an
    initial small local corpus is explicitly acceptable per §5.10). The
    collection name stays ``finance_knowledge``.
    """

    root_dir: Path
    qdrant_url: str | None = None
    qdrant_path: Path | None = None
    collection: str = COLLECTION_NAME
    embedder_dim: int = 256

    def __post_init__(self) -> None:
        self.root_dir = Path(self.root_dir)
        if self.qdrant_path is not None:
            self.qdrant_path = Path(self.qdrant_path)
        elif self.qdrant_url is None:
            self.qdrant_path = self.root_dir / "vector"


class PipelineKnowledge(FinanceKnowledge):
    """:class:`FinanceKnowledge` facade that also carries layer 1.

    The base facade wraps documents + index only; the ingestion pipeline
    additionally needs the :class:`FactsArchive` so raw news items are
    retained by trading date (Loop.md §5.8/§5.10). ``facts`` and
    ``documents`` are public so :func:`ingest_news_snapshot` can reach all
    three layers through the single ``knowledge`` argument.
    """

    def __init__(
        self,
        facts: FactsArchive,
        documents: DocumentStore,
        index: KnowledgeIndex | None = None,
    ) -> None:
        super().__init__(documents, index)
        self.facts = facts
        self.documents = documents


def build_knowledge(
    config: KnowledgeConfig,
) -> tuple[FinanceKnowledge, KnowledgeIndex | None]:
    """Construct the full knowledge stack under ``config.root_dir``.

    Layout: ``root/facts`` (JSONL archive), ``root/documents.db`` (SQLite
    document store — its OWN file, never the Ledger's, Loop.md §5.10), and
    the ``finance_knowledge`` vector index (embedded at ``qdrant_path`` or
    remote at ``qdrant_url``).

    NEVER raises for a vector outage (Loop.md §5.10: the vector DB is not
    an execution dependency — if unavailable, research retrieval fails
    closed and the document store keeps working): when the index cannot be
    constructed or pinged this returns ``(knowledge_without_index, None)``
    and logs a warning.
    """
    root = Path(config.root_dir)
    root.mkdir(parents=True, exist_ok=True)
    facts = FactsArchive(root / "facts")
    documents = DocumentStore(f"sqlite:///{root / 'documents.db'}")
    embedder = HashingEmbedder(dim=config.embedder_dim)

    index: KnowledgeIndex | None = None
    try:
        if config.qdrant_url is not None:
            index = KnowledgeIndex(
                url=config.qdrant_url,
                embedder=embedder,
                collection=config.collection,
            )
        else:
            path = config.qdrant_path or (root / "vector")
            index = KnowledgeIndex(
                path=path, embedder=embedder, collection=config.collection
            )
        index.available()  # ping; raises KnowledgeUnavailable when down
    except Exception as exc:  # fail closed, never break the loop (§5.10)
        logger.warning(
            "vector index unavailable — knowledge store runs WITHOUT semantic "
            "search; research retrieval fails closed, documents/facts keep "
            "working (Loop.md §5.10)",
            extra={
                "backend": config.qdrant_url or str(config.qdrant_path),
                "error": str(exc),
            },
        )
        index = None

    return PipelineKnowledge(facts, documents, index), index


# ---------------------------------------------------------------- ingestion


@dataclass
class IngestReport:
    """Outcome of one snapshot ingestion batch (all counts exact)."""

    n_items: int = 0
    n_docs_written: int = 0
    n_indexed: int = 0
    n_duplicates: int = 0
    n_skipped_no_provenance: int = 0
    vector_ok: bool = False
    warnings: list[str] = field(default_factory=list)


def _parse_ts(value: Any) -> datetime | None:
    """tz-aware UTC datetime from a datetime or ISO string; None if unusable.

    Mirrors the tolerant snapshot-timestamp handling in
    :mod:`swing_trader.monitors` (naive values are assumed UTC).
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _snapshot_parts(news: Any) -> tuple[list[Mapping[str, Any]], datetime | None]:
    """(items, snapshot ts) from a NewsSnapshot model OR its model_dump dict."""
    if isinstance(news, Mapping):
        raw_items = news.get("items") or []
        snap_ts = _parse_ts(news.get("ts"))
    else:  # pydantic NewsSnapshot (duck-typed: .items / .ts)
        raw_items = getattr(news, "items", None) or []
        snap_ts = _parse_ts(getattr(news, "ts", None))
    items = [i for i in raw_items if isinstance(i, Mapping)]
    return items, snap_ts


def ingest_news_snapshot(
    knowledge: FinanceKnowledge,
    index: KnowledgeIndex | None,
    news: "NewsSnapshot | Mapping[str, Any]",
    trading_date: date,
) -> IngestReport:
    """Ingest one news snapshot into facts + documents (+ vector index).

    Per item (Loop.md §5.10):

    - items missing ``url`` or ``source`` are SKIPPED and counted —
      provenance is mandatory, sources must stay traceable;
    - otherwise a ``news`` research document is built (title/text =
      headline, publisher = source, ``event_ts`` = item ts, parser
      ``news-v1``) and ingested via the existing :class:`DocumentStore`
      (content-hash dedupe counts as duplicate, not new), and the raw item
      is ALSO appended to the :class:`FactsArchive` under kind ``news``
      (append-only retention by trading date, §5.8);
    - new documents are indexed only while ``index`` is present AND
      available. A vector failure mid-batch is logged ONCE, flips
      ``vector_ok`` to False, and documents/facts keep being written —
      search fails closed, facts are never lost. This function never
      raises for vector trouble.
    """
    if isinstance(trading_date, str):  # defensive: accept ISO date strings
        trading_date = date.fromisoformat(trading_date)

    report = IngestReport()
    items, snap_ts = _snapshot_parts(news)
    report.n_items = len(items)
    retrieved_at = snap_ts or datetime.now(timezone.utc)

    documents: DocumentStore = getattr(knowledge, "documents", None) or knowledge._documents  # noqa: SLF001
    facts: FactsArchive | None = getattr(knowledge, "facts", None)
    if facts is None:
        _warn(report, "no facts archive attached — raw news items NOT archived")

    # Vector readiness, checked once up front (fail closed, Loop.md §5.10).
    vector_ok = False
    if index is None:
        _warn(
            report,
            "no vector index — documents stored without semantic indexing "
            "(research search fails closed, Loop.md §5.10)",
        )
    else:
        try:
            vector_ok = index.available()
        except KnowledgeUnavailable as exc:
            _warn(report, f"vector index down at batch start: {exc}")

    for item in items:
        url = str(item.get("url") or "").strip()
        source = str(item.get("source") or "").strip()
        if not url or not source:
            report.n_skipped_no_provenance += 1
            continue

        headline = str(item.get("headline") or "").strip()
        symbol = item.get("symbol")
        doc = ResearchDocument(
            title=headline,
            text=headline,
            source_url=url,
            publisher=source,
            retrieved_at=retrieved_at,
            symbols=[str(symbol)] if symbol else [],
            event_ts=_parse_ts(item.get("ts")),
            trading_date_et=trading_date,
            doc_type=DocType.NEWS,
            license_status=LicenseStatus.PUBLIC,
            parser_version=NEWS_PARSER_VERSION,
        )

        before = documents.count()
        doc_id = documents.ingest(doc)
        is_new = documents.count() > before
        if is_new:
            report.n_docs_written += 1
        else:
            report.n_duplicates += 1

        # Layer 1: append-only raw retention, regardless of layer-2 dedupe.
        if facts is not None:
            facts.write(FACTS_KIND_NEWS, dict(item), trading_date)

        # Layer 3: best-effort indexing of NEW documents only.
        if is_new and index is not None and vector_ok:
            try:
                index.index(
                    doc_id,
                    doc.text,
                    payload={
                        "document_id": doc_id,
                        "symbols": doc.symbols,
                        "doc_type": DocType.NEWS.value,
                        "trading_date": trading_date.isoformat(),
                        "publisher": source,
                    },
                )
                report.n_indexed += 1
            except KnowledgeUnavailable as exc:
                vector_ok = False  # stop indexing; keep writing documents
                _warn(
                    report,
                    "vector index failed mid-batch — remaining items stored "
                    f"WITHOUT indexing (fail closed, Loop.md §5.10): {exc}",
                )

    if report.n_skipped_no_provenance:
        _warn(
            report,
            f"skipped {report.n_skipped_no_provenance} news item(s) missing "
            "url/source — provenance is mandatory (Loop.md §5.10)",
        )

    report.vector_ok = vector_ok
    logger.info(
        "news snapshot ingested",
        extra={
            "trading_date": trading_date.isoformat(),
            "n_items": report.n_items,
            "n_docs_written": report.n_docs_written,
            "n_indexed": report.n_indexed,
            "n_duplicates": report.n_duplicates,
            "n_skipped_no_provenance": report.n_skipped_no_provenance,
            "vector_ok": report.vector_ok,
        },
    )
    return report


def _warn(report: IngestReport, message: str) -> None:
    """Log once and record on the report (single source for batch warnings)."""
    logger.warning(message)
    report.warnings.append(message)


# ------------------------------------------------------------------- search


def search_knowledge(
    knowledge: FinanceKnowledge,
    index: KnowledgeIndex | None,
    query: str,
    k: int = 5,
) -> list[dict[str, Any]]:
    """Source-attributed semantic search as JSON-ready dicts.

    Thin wrapper over :meth:`FinanceKnowledge.search`; each hit becomes
    ``{document_id, title, snippet, source_url, publisher, score,
    trading_date}`` — always citing its source (Loop.md §5.10: return
    links/snippets, never untraceable model facts).

    Raises :class:`KnowledgeUnavailable` when ``index`` is None or the
    backend is down — research retrieval fails closed; the API layer maps
    this to HTTP 503.
    """
    if index is None:
        raise KnowledgeUnavailable(
            "no vector index configured — research search fails closed "
            "(Loop.md §5.10)"
        )
    results: list[dict[str, Any]] = []
    for hit in knowledge.search(query, k=k):  # raises KnowledgeUnavailable when down
        doc: ResearchDocument = hit["document"]
        results.append(
            {
                "document_id": doc.id,
                "title": doc.title,
                "snippet": hit["snippet"],
                "source_url": hit["source_url"],
                "publisher": hit["publisher"],
                "score": hit["score"],
                "trading_date": doc.trading_date_et.isoformat(),
            }
        )
    return results
