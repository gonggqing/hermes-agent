from datetime import date, datetime, timezone

from swing_trader.knowledge import (
    DocType,
    DocumentStore,
    HashingEmbedder,
    KnowledgeIndex,
    ResearchDocument,
)
from swing_trader.vector_migration import migrate_document_store


def test_document_store_migration_is_verified_and_idempotent(tmp_path):
    store = DocumentStore(f"sqlite:///{tmp_path / 'documents.db'}")
    for idx, text in enumerate(
        [
            "Micron HBM memory demand remains strong",
            "Gold rises as real yields decline",
            "Robot reducer supply chain capacity expands",
        ]
    ):
        store.ingest(
            ResearchDocument(
                title=f"Research {idx}",
                text=text,
                source_url=f"https://example.com/{idx}",
                publisher="Example Research",
                retrieved_at=datetime(2026, 7, 16, idx, tzinfo=timezone.utc),
                symbols=["MU"] if idx == 0 else [],
                trading_date_et=date(2026, 7, 16),
                doc_type=DocType.RESEARCH,
            )
        )
    index = KnowledgeIndex(
        path=tmp_path / "target-qdrant",
        embedder=HashingEmbedder(dim=64),
    )

    first = migrate_document_store(store, index, batch_size=2)
    assert first.source_documents == 3
    assert first.target_before == 0
    assert first.target_after == 3
    assert first.sample_hits_resolved == first.sample_queries == 3
    assert first.verified is True

    second = migrate_document_store(store, index, batch_size=2)
    assert second.target_before == second.target_after == 3
    assert second.submitted == 3
    assert second.verified is True


def test_document_store_migration_rejects_invalid_batch_size(tmp_path):
    store = DocumentStore(f"sqlite:///{tmp_path / 'documents.db'}")
    index = KnowledgeIndex(
        path=tmp_path / "target-qdrant",
        embedder=HashingEmbedder(dim=64),
    )
    try:
        migrate_document_store(store, index, batch_size=0)
    except ValueError as exc:
        assert "batch_size" in str(exc)
    else:
        raise AssertionError("invalid batch size must fail")
