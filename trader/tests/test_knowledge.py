"""Tests for swing_trader.knowledge (Loop.md §5.10).

Fully offline: the vector layer runs qdrant in EMBEDDED mode against
tmp_path, and "backend down" is simulated with an unreachable loopback
URL (http://127.0.0.1:1 — connection refused locally, zero network).

Covers: archive partitioning/append-only/roundtrip; mandatory provenance;
restricted-license refusal; content-hash dedupe; find filters; deterministic
hashing embedder; embedded index+search roundtrip with ranking; fail-closed
KnowledgeUnavailable on index/search/available; research_ready; document
store surviving a vector outage; facade attribution + graceful skip.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

import pytest

from swing_trader.knowledge import (
    DocType,
    DocumentStore,
    EmbeddingProvider,
    FactsArchive,
    FinanceKnowledge,
    HashingEmbedder,
    KnowledgeIndex,
    KnowledgeUnavailable,
    LicenseStatus,
    ResearchDocument,
    content_hash,
    research_ready,
)

UTC = timezone.utc
D0 = date(2026, 7, 10)
D1 = date(2026, 7, 13)
RETRIEVED = datetime(2026, 7, 10, 20, 0, tzinfo=UTC)

#: Nothing listens on port 1; connection is refused by the local kernel.
UNREACHABLE_URL = "http://127.0.0.1:1"


def make_doc(**overrides) -> ResearchDocument:
    base = dict(
        title="MU Q3 earnings beat",
        text="Micron reported record HBM revenue driven by AI datacenter demand.",
        source_url="https://investors.micron.com/q3-2026",
        publisher="Micron IR",
        retrieved_at=RETRIEVED,
        symbols=["MU"],
        themes=["memory", "ai"],
        trading_date_et=D0,
        doc_type=DocType.EARNINGS,
        license_status=LicenseStatus.PUBLIC,
    )
    base.update(overrides)
    return ResearchDocument(**base)


@pytest.fixture()
def store(tmp_path) -> DocumentStore:
    return DocumentStore(f"sqlite:///{tmp_path / 'knowledge.db'}")


@pytest.fixture()
def local_index(tmp_path) -> KnowledgeIndex:
    return KnowledgeIndex(path=tmp_path / "qdrant", embedder=HashingEmbedder(dim=64))


@pytest.fixture(scope="module")
def down_index() -> KnowledgeIndex:
    # Construction does not touch the network; only operations do.
    return KnowledgeIndex(url=UNREACHABLE_URL, timeout=1)


# ------------------------------------------------------------ FactsArchive


def test_archive_partitions_by_trading_date(tmp_path):
    archive = FactsArchive(tmp_path)
    path = archive.write("news", {"headline": "MU beats"}, trading_date=D0)
    assert path == tmp_path / "2026" / "2026-07-10" / "news.jsonl"
    assert path.exists()


def test_archive_append_only_roundtrip(tmp_path):
    archive = FactsArchive(tmp_path)
    archive.write("market", {"spy": 650.0}, trading_date=D0)
    archive.write("market", {"spy": 655.5}, trading_date=D0)
    archive.write("market", {"spy": 700.0}, trading_date=D1)  # other partition

    assert archive.read("market", D0) == [{"spy": 650.0}, {"spy": 655.5}]
    assert archive.read("market", D1) == [{"spy": 700.0}]
    assert archive.read("market", date(2026, 1, 2)) == []  # nothing archived
    # two writes appended two lines to ONE file
    raw = (tmp_path / "2026" / "2026-07-10" / "market.jsonl").read_text()
    assert len(raw.strip().splitlines()) == 2


def test_archive_exposes_no_mutation_api(tmp_path):
    archive = FactsArchive(tmp_path)
    for name in ("delete", "update", "remove", "overwrite", "truncate"):
        assert not hasattr(archive, name), f"append-only archive must not expose {name}"


def test_archive_rejects_unsafe_kind(tmp_path):
    archive = FactsArchive(tmp_path)
    for bad in ("../evil", "a/b", "", "dot.dot"):
        with pytest.raises(ValueError):
            archive.write(bad, {}, trading_date=D0)


# ----------------------------------------------------------- DocumentStore


def test_ingest_requires_provenance(store):
    for missing in ("source_url", "publisher", "retrieved_at"):
        doc = make_doc(**{missing: None})
        with pytest.raises(ValueError, match=missing):
            store.ingest(doc)
    assert store.count() == 0


def test_ingest_refuses_restricted_license(store):
    doc = make_doc(license_status=LicenseStatus.RESTRICTED)
    with pytest.raises(ValueError, match=r"5\.10"):
        store.ingest(doc)
    assert store.count() == 0


def test_ingest_dedupes_by_content_hash(store):
    first = store.ingest(make_doc())
    # same content modulo whitespace, different title -> SAME document
    dup = make_doc(
        title="another headline",
        text="Micron reported   record HBM revenue\n driven by AI datacenter demand. ",
    )
    assert content_hash(dup.text) == content_hash(make_doc().text)
    assert store.ingest(dup) == first
    assert store.count() == 1


def test_get_roundtrips_all_fields(store):
    event = datetime(2026, 7, 9, 21, 0, tzinfo=UTC)
    doc_id = store.ingest(make_doc(symbols=["mu", " wdc "], event_ts=event))
    got = store.get(doc_id)
    assert got is not None
    assert got.id == doc_id
    assert got.title == "MU Q3 earnings beat"
    assert got.source_url == "https://investors.micron.com/q3-2026"
    assert got.publisher == "Micron IR"
    assert got.retrieved_at == RETRIEVED
    assert got.symbols == ["MU", "WDC"]  # normalized upper
    assert got.themes == ["memory", "ai"]
    assert got.event_ts == event
    assert got.trading_date_et == D0
    assert got.doc_type is DocType.EARNINGS
    assert got.license_status is LicenseStatus.PUBLIC
    assert got.content_hash == content_hash(got.text)
    assert store.get("nope") is None


def test_find_filters(store):
    store.ingest(make_doc())
    store.ingest(
        make_doc(
            title="NVDA supply note",
            text="Nvidia rack shipments accelerate.",
            symbols=["NVDA"],
            doc_type=DocType.NEWS,
        )
    )
    store.ingest(
        make_doc(
            title="MU technical note",
            text="Micron breaks out above its 50dma on volume.",
            symbols=["MU"],
            doc_type=DocType.NOTE,
            trading_date_et=D1,
        )
    )

    assert {d.title for d in store.find(symbol="mu")} == {
        "MU Q3 earnings beat",
        "MU technical note",
    }
    assert [d.title for d in store.find(doc_type="news")] == ["NVDA supply note"]
    assert [d.title for d in store.find(trading_date=D1)] == ["MU technical note"]
    assert [d.title for d in store.find(symbol="MU", trading_date=D0)] == [
        "MU Q3 earnings beat"
    ]
    assert store.find(symbol="TSLA") == []
    assert store.count() == 3


def test_document_store_isolated_from_ledger_tables(tmp_path):
    """§5.10: research documents live in their OWN database, never the ledger's."""
    from sqlmodel import SQLModel

    db = tmp_path / "knowledge.db"
    DocumentStore(f"sqlite:///{db}")
    # knowledge table is NOT on the global SQLModel metadata -> Ledger's
    # create_all can never create it inside the ledger DB.
    assert "research_documents" not in SQLModel.metadata.tables
    # and the knowledge DB contains no ledger tables
    with sqlite3.connect(db) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "research_documents" in tables
    assert not tables & {"orders", "trades", "fills", "signals", "audit_events"}


# --------------------------------------------------------- HashingEmbedder


def test_hashing_embedder_deterministic_across_instances():
    text = "record HBM revenue"
    a = HashingEmbedder(dim=64).embed([text])[0]
    b = HashingEmbedder(dim=64).embed([text])[0]
    assert a == b


def test_hashing_embedder_fixed_dim_and_normalized():
    vecs = HashingEmbedder().embed(["one", "two words here", ""])
    assert all(len(v) == 256 for v in vecs)
    assert all(len(v) == 32 for v in HashingEmbedder(dim=32).embed(["one", "two"]))
    # non-empty text -> unit L2 norm (cosine-ready); empty text -> zero vector
    norm = sum(x * x for x in vecs[1]) ** 0.5
    assert norm == pytest.approx(1.0)
    assert all(x == 0.0 for x in vecs[2])
    with pytest.raises(ValueError):
        HashingEmbedder(dim=0)


def test_hashing_embedder_distinguishes_texts():
    emb = HashingEmbedder(dim=64)
    a, b = emb.embed(["uranium miners rally on supply deficit", "gold hedges drawdown"])
    assert a != b


def test_hashing_embedder_satisfies_provider_protocol():
    assert isinstance(HashingEmbedder(), EmbeddingProvider)


# ----------------------------------------------- KnowledgeIndex (embedded)


def test_embedded_index_search_roundtrip(local_index):
    docs = {
        "d-uranium": "Cameco and uranium miners rally as reactor demand grows",
        "d-memory": "Micron HBM memory supply remains tight through 2027",
        "d-gold": "Gold holds gains as a portfolio hedge against drawdowns",
    }
    for doc_id, text in docs.items():
        local_index.index(
            doc_id, text, payload={"publisher": "Test IR", "doc_type": "news"}
        )

    hits = local_index.search("uranium reactor demand", k=3)
    assert len(hits) == 3
    assert hits[0]["document_id"] == "d-uranium"  # distinctive terms rank first
    assert hits[0]["score"] >= hits[1]["score"] >= hits[2]["score"]
    assert hits[0]["payload"]["publisher"] == "Test IR"
    assert hits[0]["payload"]["document_id"] == "d-uranium"


def test_embedded_search_before_any_index_is_empty(local_index):
    assert local_index.search("anything") == []


def test_embedded_available_and_research_ready(local_index):
    assert local_index.available() is True
    assert research_ready(local_index) is True
    assert research_ready(None) is False


# ------------------------------------------- fail-closed (backend down)


def test_unreachable_index_raises(down_index):
    with pytest.raises(KnowledgeUnavailable):
        down_index.index("d1", "some text", payload={})


def test_unreachable_search_raises(down_index):
    with pytest.raises(KnowledgeUnavailable):
        down_index.search("anything")


def test_unreachable_available_raises(down_index):
    with pytest.raises(KnowledgeUnavailable):
        down_index.available()


def test_research_ready_false_when_backend_down(down_index):
    assert research_ready(down_index) is False


def test_archive_and_document_store_survive_vector_outage(tmp_path, store, down_index):
    """§5.10: vector DB down => retrieval fails closed, storage keeps working."""
    archive = FactsArchive(tmp_path / "facts")
    archive.write("news", {"headline": "still archiving"}, trading_date=D0)
    assert archive.read("news", D0) == [{"headline": "still archiving"}]

    facade = FinanceKnowledge(store, index=down_index)
    doc_id = facade.ingest_and_index(make_doc())  # must NOT raise
    assert store.get(doc_id) is not None
    assert store.count() == 1
    with pytest.raises(KnowledgeUnavailable):  # retrieval fails closed
        facade.search("micron")


# ------------------------------------------------------------------ facade


def test_facade_skips_indexing_when_no_index(store):
    facade = FinanceKnowledge(store, index=None)
    doc_id = facade.ingest_and_index(make_doc())
    assert store.get(doc_id) is not None
    with pytest.raises(KnowledgeUnavailable, match=r"5\.10"):
        facade.search("micron")


def test_facade_search_returns_source_attribution(store, local_index):
    facade = FinanceKnowledge(store, index=local_index)
    long_text = (
        "Cameco and the uranium complex rallied after utilities signed "
        "long-term reactor fuel contracts. " * 10
    )
    facade.ingest_and_index(
        make_doc(
            title="Uranium rally",
            text=long_text,
            source_url="https://example.com/uranium",
            publisher="Example Wire",
            symbols=["CCJ", "URA"],
            doc_type=DocType.NEWS,
        )
    )
    facade.ingest_and_index(make_doc())  # the MU earnings doc

    results = facade.search("uranium reactor fuel contracts", k=2)
    assert len(results) == 2
    top = results[0]
    # ALWAYS source-attributed (Loop.md §5.10): link + publisher + snippet
    assert top["source_url"] == "https://example.com/uranium"
    assert top["publisher"] == "Example Wire"
    assert top["snippet"] == long_text[:300]
    assert len(top["snippet"]) == 300
    assert top["document"].title == "Uranium rally"
    assert top["document"].symbols == ["CCJ", "URA"]
    assert top["score"] >= results[1]["score"]
    assert results[1]["document"].title == "MU Q3 earnings beat"


def test_facade_indexes_dedup_to_single_point(store, local_index):
    facade = FinanceKnowledge(store, index=local_index)
    id1 = facade.ingest_and_index(make_doc())
    id2 = facade.ingest_and_index(make_doc(title="re-fetched duplicate"))
    assert id1 == id2
    results = facade.search("micron HBM revenue", k=5)
    assert [r["document"].id for r in results] == [id1]  # one point, not two
