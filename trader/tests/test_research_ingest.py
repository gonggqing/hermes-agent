"""Deeper research ingestion (Loop.md §5.10, Phase 0.75): fundamentals +
earnings docs into the knowledge store, retrievable via RAG."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from swing_trader.knowledge import DocType
from swing_trader.knowledge_pipeline import (
    KnowledgeConfig,
    build_knowledge,
    search_knowledge,
)
from swing_trader.research_ingest import (
    build_earnings_doc,
    build_fundamentals_doc,
    ingest_research_documents,
)

UTC = timezone.utc
TD = date(2026, 7, 13)
AT = datetime(2026, 7, 13, 16, 0, tzinfo=UTC)


@pytest.fixture()
def stack(tmp_path):
    knowledge, index = build_knowledge(KnowledgeConfig(root_dir=tmp_path / "k"))
    return knowledge, index


# --------------------------------------------------------------- builders

def test_build_fundamentals_doc():
    m = {"pe": 60.0, "fwd_pe": 35.0, "rev_growth_pct": 22.0, "name": "NVIDIA"}
    doc = build_fundamentals_doc("NVDA", m, TD, AT)
    assert doc is not None and doc.doc_type is DocType.RESEARCH
    assert "trailing P/E 60" in doc.text and "revenue growth 22%" in doc.text
    assert doc.source_url and doc.publisher == "Yahoo Finance" and doc.symbols == ["NVDA"]


def test_build_fundamentals_doc_none_when_empty():
    assert build_fundamentals_doc("NVDA", {"name": "NVIDIA"}, TD, AT) is None


def test_build_earnings_doc():
    doc = build_earnings_doc("NVDA", "2026-07-15", 2, TD, AT)
    assert doc.doc_type is DocType.EARNINGS and "2026-07-15" in doc.text
    assert doc.source_url and doc.publisher == "Yahoo Finance"


# --------------------------------------------------------------- ingestion

def test_ingest_writes_indexes_and_dedupes(stack):
    knowledge, index = stack
    docs = [
        build_fundamentals_doc("NVDA", {"pe": 60.0, "rev_growth_pct": 22.0}, TD, AT),
        build_earnings_doc("NVDA", "2026-07-15", 2, TD, AT),
    ]
    r1 = ingest_research_documents(knowledge, index, docs, TD)
    assert r1.n_docs_written == 2 and r1.n_indexed == 2 and r1.vector_ok
    # re-ingesting identical docs the next day -> all duplicates, none re-indexed
    r2 = ingest_research_documents(knowledge, index, docs, TD)
    assert r2.n_docs_written == 0 and r2.n_duplicates == 2 and r2.n_indexed == 0


def test_ingest_skips_missing_provenance(stack):
    knowledge, index = stack
    doc = build_fundamentals_doc("NVDA", {"pe": 60.0}, TD, AT)
    doc.source_url = None  # strip provenance
    r = ingest_research_documents(knowledge, index, [doc], TD)
    assert r.n_skipped_no_provenance == 1 and r.n_docs_written == 0


def test_ingest_fail_closed_without_index(stack):
    knowledge, _ = stack
    docs = [build_fundamentals_doc("NVDA", {"pe": 60.0}, TD, AT)]
    r = ingest_research_documents(knowledge, None, docs, TD)  # vector unavailable
    assert r.n_docs_written == 1 and r.n_indexed == 0 and r.vector_ok is False


def test_rag_can_retrieve_ingested_fundamentals(stack):
    knowledge, index = stack
    ingest_research_documents(
        knowledge, index,
        [build_fundamentals_doc("NVDA", {"pe": 60.0, "rev_growth_pct": 22.0,
                                         "name": "NVIDIA"}, TD, AT)],
        TD,
    )
    hits = search_knowledge(knowledge, index, "NVDA fundamentals P/E growth", k=3)
    assert any("NVDA" in (h.get("title") or "") for h in hits)
    assert all(h.get("source_url") for h in hits)  # every hit is source-attributed
