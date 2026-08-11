"""Idempotent DocumentStore -> Qdrant migration for Finance research.

The normalized SQL document store remains authoritative.  We intentionally
re-embed its rows instead of copying embedded-Qdrant files: embedded and
server Qdrant storage layouts are not a supported migration boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from swing_trader.knowledge import DocumentStore, KnowledgeIndex, ResearchDocument


@dataclass(frozen=True)
class VectorMigrationReport:
    source_documents: int
    target_before: int
    submitted: int
    target_after: int
    sample_queries: int
    sample_hits_resolved: int
    verified: bool

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def _payload(
    doc: ResearchDocument, dim: int | None, embedding_model: str
) -> dict:
    assert doc.id is not None
    return {
        "document_id": doc.id,
        "symbols": doc.symbols,
        "themes": doc.themes,
        "doc_type": doc.doc_type.value,
        "trading_date": doc.trading_date_et.isoformat(),
        "publisher": doc.publisher,
        "source_url": doc.source_url,
        "embedding_model": embedding_model,
        "embedding_dim": dim,
    }


def migrate_document_store(
    documents: DocumentStore,
    target: KnowledgeIndex,
    *,
    batch_size: int = 128,
) -> VectorMigrationReport:
    """Backfill all normalized documents and verify count + resolvability.

    The operation is safe to replay because :class:`KnowledgeIndex` derives a
    stable Qdrant UUID from each document ID.  Verification is deliberately
    strict: the target collection must contain exactly the source rows, and
    representative searches must resolve back to authoritative documents.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    docs = documents.find()
    target_before = target.count()
    source_ids = {doc.id for doc in docs if doc.id}
    target_ids = target.document_ids()
    # The SQL store is authoritative. Remove orphaned vector references first,
    # then embed only rows absent from Qdrant. Stable point IDs make this safe
    # to resume after a crash and avoid a full paid re-embedding whenever one
    # newly ingested document changes the count.
    target.delete_document_ids(target_ids - source_ids)
    missing = [doc for doc in docs if doc.id not in target_ids]
    submitted = 0
    dim = target.embedding_dim
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        submitted += target.index_many(
            [
                (
                    doc.id or "",
                    doc.text,
                    _payload(doc, dim, target.embedding_model),
                )
                for doc in batch
            ]
        )

    target_after = target.count()
    sample_docs = []
    if docs:
        sample_docs = [docs[0], docs[len(docs) // 2], docs[-1]]
        sample_docs = list({doc.id: doc for doc in sample_docs}.values())

    target_after_ids = target.document_ids()
    sample_hits_resolved = 0
    for doc in sample_docs:
        query = doc.title or doc.text[:160]
        hits = target.search(query, k=min(5, max(1, len(docs))))
        if hits and all(hit.get("document_id") in source_ids for hit in hits):
            sample_hits_resolved += 1

    verified = (
        submitted == len(missing)
        and target_after == len(docs)
        and target_after_ids == source_ids
        and sample_hits_resolved == len(sample_docs)
    )
    return VectorMigrationReport(
        source_documents=len(docs),
        target_before=target_before,
        submitted=submitted,
        target_after=target_after,
        sample_queries=len(sample_docs),
        sample_hits_resolved=sample_hits_resolved,
        verified=verified,
    )
