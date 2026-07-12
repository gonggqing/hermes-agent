"""Tests for swing_trader.knowledge_pipeline (Loop.md Phase 0.5 backlog #5).

Fully offline and deterministic: the vector layer runs qdrant EMBEDDED
against tmp_path; "backend down" is an unreachable loopback URL
(http://127.0.0.1:1 — connection refused by the local kernel, zero
network) or a stub index.

Covers: KnowledgeConfig defaults (embedded path mode vs url mode);
build_knowledge stack construction + never-raise on unreachable qdrant;
exact IngestReport counts on a synthetic NewsSnapshot (missing-provenance
skip, in-batch content-hash duplicate); facts archive lines parseable;
documents queryable with provenance; re-ingest idempotence (all
duplicates); source-attributed ranked search; fail-closed behavior with no
index (docs still written, search raises KnowledgeUnavailable) and on a
mid-batch vector failure; model_dump dict input parity.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from swing_trader.knowledge import (
    DocType,
    FinanceKnowledge,
    KnowledgeIndex,
    KnowledgeUnavailable,
    LicenseStatus,
    research_ready,
)
from swing_trader.knowledge_pipeline import (
    IngestReport,
    KnowledgeConfig,
    NEWS_PARSER_VERSION,
    PipelineKnowledge,
    build_knowledge,
    ingest_news_snapshot,
    search_knowledge,
)
from swing_trader.monitors import NewsSnapshot

UTC = timezone.utc
D0 = date(2026, 7, 10)
SNAP_TS = datetime(2026, 7, 10, 15, 30, tzinfo=UTC)

#: Nothing listens on port 1; connection is refused locally (zero network).
UNREACHABLE_URL = "http://127.0.0.1:1"

NVDA_HEADLINE = "Nvidia unveils Rubin GPU with record HBM4 bandwidth"
MU_HEADLINE = "Micron memory supercycle extends on datacenter demand"
FED_HEADLINE = "Fed holds rates steady as inflation cools"


def make_snapshot() -> NewsSnapshot:
    """5 items: 3 distinct valid, 1 missing url, 1 duplicate headline."""
    items = [
        {
            "symbol": "NVDA",
            "ts": "2026-07-10T14:00:00+00:00",
            "headline": NVDA_HEADLINE,
            "source": "Reuters",
            "url": "https://example.com/nvda-rubin",
            "sentiment": 0.5,
        },
        {
            "symbol": "MU",
            "ts": "2026-07-10T14:05:00+00:00",
            "headline": MU_HEADLINE,
            "source": "Bloomberg",
            "url": "https://example.com/mu-supercycle",
            "sentiment": 0.4,
        },
        {
            "symbol": None,  # market-wide item -> symbols == []
            "ts": "2026-07-10T14:10:00+00:00",
            "headline": FED_HEADLINE,
            "source": "AP",
            "url": "https://example.com/fed-hold",
            "sentiment": 0.1,
        },
        {
            "symbol": "AVGO",
            "ts": "2026-07-10T14:15:00+00:00",
            "headline": "Broadcom rumor without a link",
            "source": "SomeBlog",
            "url": "",  # missing url -> provenance skip
            "sentiment": 0.0,
        },
        {
            "symbol": "NVDA",  # same headline text -> content-hash duplicate
            "ts": "2026-07-10T14:20:00+00:00",
            "headline": NVDA_HEADLINE,
            "source": "CNBC",
            "url": "https://example.com/nvda-rubin-echo",
            "sentiment": 0.5,
        },
    ]
    return NewsSnapshot(ts=SNAP_TS, items=items)


@pytest.fixture()
def stack(tmp_path):
    """(config, knowledge, index) over an embedded qdrant at tmp_path."""
    config = KnowledgeConfig(root_dir=tmp_path / "knowledge")
    knowledge, index = build_knowledge(config)
    return config, knowledge, index


class _DownIndex:
    """Stub index that is down from the start (available() raises)."""

    def available(self) -> bool:
        raise KnowledgeUnavailable("stub backend down")

    def index(self, doc_id, text, payload=None) -> None:  # pragma: no cover
        raise AssertionError("must not index when unavailable")


class _FlakyIndex:
    """Stub index that dies after ``fail_after`` successful index calls."""

    def __init__(self, fail_after: int = 1) -> None:
        self.fail_after = fail_after
        self.indexed: list[str] = []

    def available(self) -> bool:
        return True

    def index(self, doc_id, text, payload=None) -> None:
        if len(self.indexed) >= self.fail_after:
            raise KnowledgeUnavailable("stub backend died mid-batch")
        self.indexed.append(doc_id)


# ------------------------------------------------------------ configuration


def test_config_defaults_embedded_path_mode(tmp_path):
    config = KnowledgeConfig(root_dir=tmp_path / "k")
    assert config.qdrant_url is None
    assert config.qdrant_path == tmp_path / "k" / "vector"
    assert config.collection == "finance_knowledge"  # stays per Loop.md §5.10
    assert config.embedder_dim == 256


def test_config_url_mode_leaves_path_unset(tmp_path):
    config = KnowledgeConfig(root_dir=tmp_path / "k", qdrant_url="http://vec:6333")
    assert config.qdrant_path is None
    assert config.qdrant_url == "http://vec:6333"


def test_build_knowledge_embedded_stack(tmp_path, stack):
    config, knowledge, index = stack
    assert isinstance(knowledge, FinanceKnowledge)
    assert isinstance(knowledge, PipelineKnowledge)
    assert isinstance(index, KnowledgeIndex)
    assert research_ready(index) is True
    assert (config.root_dir / "documents.db").exists()


def test_build_knowledge_unreachable_url_returns_none_index(tmp_path):
    config = KnowledgeConfig(root_dir=tmp_path / "k", qdrant_url=UNREACHABLE_URL)
    knowledge, index = build_knowledge(config)  # must NOT raise (Loop.md §5.10)
    assert index is None
    assert isinstance(knowledge, FinanceKnowledge)
    assert knowledge.documents.count() == 0  # document store still works


# ---------------------------------------------------------------- ingestion


def test_ingest_report_counts_exact(stack):
    _, knowledge, index = stack
    report = ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    assert isinstance(report, IngestReport)
    assert report.n_items == 5
    assert report.n_docs_written == 3
    assert report.n_indexed == 3
    assert report.n_duplicates == 1
    assert report.n_skipped_no_provenance == 1
    assert report.vector_ok is True
    assert any("provenance" in w for w in report.warnings)


def test_facts_archive_lines_parseable(stack):
    config, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    path = config.root_dir / "facts" / "2026" / "2026-07-10" / "news.jsonl"
    assert path.exists()
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    # 4 provenance-complete raw items retained (incl. the duplicate; layer-1
    # retention is append-only and independent of layer-2 dedupe).
    assert len(lines) == 4
    assert {l["headline"] for l in lines} == {NVDA_HEADLINE, MU_HEADLINE, FED_HEADLINE}
    assert all(l["source"] and l["url"] for l in lines)


def test_documents_queryable_with_provenance(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    docs = knowledge.documents.find(doc_type=DocType.NEWS, trading_date=D0)
    assert len(docs) == 3
    (mu_doc,) = knowledge.documents.find(symbol="MU")
    assert mu_doc.title == MU_HEADLINE
    assert mu_doc.source_url == "https://example.com/mu-supercycle"
    assert mu_doc.publisher == "Bloomberg"


def test_document_metadata_normalized(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    (nvda_doc,) = knowledge.documents.find(symbol="NVDA")
    assert nvda_doc.doc_type is DocType.NEWS
    assert nvda_doc.license_status is LicenseStatus.PUBLIC
    assert nvda_doc.parser_version == NEWS_PARSER_VERSION
    assert nvda_doc.event_ts == datetime(2026, 7, 10, 14, 0, tzinfo=UTC)
    assert nvda_doc.retrieved_at == SNAP_TS
    assert nvda_doc.trading_date_et == D0
    # market-wide item carries no symbol tag
    (fed_doc,) = [d for d in knowledge.documents.find() if d.title == FED_HEADLINE]
    assert fed_doc.symbols == []


def test_reingest_same_snapshot_all_duplicates(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    report = ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    assert report.n_docs_written == 0
    assert report.n_duplicates == 4  # 3 distinct + the in-batch echo
    assert report.n_indexed == 0
    assert report.n_skipped_no_provenance == 1
    assert knowledge.documents.count() == 3  # unchanged


def test_dict_input_model_dump_parity(stack):
    _, knowledge, index = stack
    dumped = make_snapshot().model_dump(mode="json")  # ts becomes an ISO string
    report = ingest_news_snapshot(knowledge, index, dumped, D0)
    assert report.n_docs_written == 3
    assert report.n_duplicates == 1
    assert report.n_skipped_no_provenance == 1
    assert report.vector_ok is True
    (mu_doc,) = knowledge.documents.find(symbol="MU")
    assert mu_doc.retrieved_at == SNAP_TS  # snapshot ts parsed from string


# ------------------------------------------------------------------- search


def test_search_returns_relevant_doc_first_with_source(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    results = search_knowledge(knowledge, index, "micron memory supercycle")
    assert results, "expected at least one hit"
    top = results[0]
    assert top["title"] == MU_HEADLINE
    assert top["source_url"] == "https://example.com/mu-supercycle"
    assert top["publisher"] == "Bloomberg"
    assert top["trading_date"] == "2026-07-10"
    assert top["score"] > 0.0
    assert MU_HEADLINE.startswith(top["snippet"][:20])


def test_search_results_are_json_ready(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    results = search_knowledge(knowledge, index, "nvidia rubin gpu", k=2)
    assert len(results) <= 2
    for hit in results:
        assert set(hit) == {
            "document_id",
            "title",
            "snippet",
            "source_url",
            "publisher",
            "score",
            "trading_date",
        }
        json.dumps(hit)  # must not raise: JSON-ready by contract
        assert knowledge.documents.get(hit["document_id"]) is not None


# --------------------------------------------------------------- fail closed


def test_index_none_still_writes_documents(stack):
    _, knowledge, _ = stack
    report = ingest_news_snapshot(knowledge, None, make_snapshot(), D0)
    assert report.vector_ok is False
    assert report.n_docs_written == 3
    assert report.n_indexed == 0
    assert knowledge.documents.count() == 3
    assert any("vector" in w for w in report.warnings)


def test_search_knowledge_raises_when_index_none(stack):
    _, knowledge, index = stack
    ingest_news_snapshot(knowledge, index, make_snapshot(), D0)
    with pytest.raises(KnowledgeUnavailable):
        search_knowledge(knowledge, None, "micron memory")


def test_ingest_with_index_down_at_start(stack):
    _, knowledge, _ = stack
    report = ingest_news_snapshot(knowledge, _DownIndex(), make_snapshot(), D0)
    assert report.vector_ok is False
    assert report.n_indexed == 0
    assert report.n_docs_written == 3  # storage never depends on search
    assert any("down at batch start" in w for w in report.warnings)


def test_mid_batch_vector_failure_keeps_writing_documents(stack):
    _, knowledge, _ = stack
    flaky = _FlakyIndex(fail_after=1)
    report = ingest_news_snapshot(knowledge, flaky, make_snapshot(), D0)
    assert report.n_indexed == 1  # first doc indexed, then the backend died
    assert report.vector_ok is False
    assert report.n_docs_written == 3  # all documents still written
    assert knowledge.documents.count() == 3
    assert sum("mid-batch" in w for w in report.warnings) == 1  # logged ONCE
