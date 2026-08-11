"""Finance knowledge store — historical research + semantic retrieval (Loop.md §5.10).

Three layers, strictly ordered by authority:

1. **Facts** — :class:`FactsArchive`: immutable raw-source files, one JSONL
   file per (trading date, kind) under ``<root>/<YYYY>/<YYYY-MM-DD>/``.
   Append-only BY DESIGN: the class exposes no delete/update API; retention
   is the point (Loop.md §5.8/§5.10 — sources are retained by trading date,
   never discarded).
2. **Research documents** — :class:`DocumentStore`: normalized text rows in
   the ``research_documents`` SQLModel table, in its OWN database file. It
   must never share a file with the trading Ledger; the table is registered
   on a private ``MetaData`` so neither store can ever create the other's
   tables. Provenance (source URL, publisher, retrieval time) is mandatory,
   and ``license_status="restricted"`` material is refused outright —
   Loop.md §5.10: never bypass paywalls, credentials, or licenses.
3. **Vector index** — :class:`KnowledgeIndex`: a ``finance_knowledge``
   Qdrant collection whose points carry only document IDs + metadata. It
   accelerates semantic retrieval ONLY; it never replaces source records,
   deterministic market data, or the Ledger (Loop.md §5.10). Runs embedded
   (``path=`` — local dir, fully offline, used in tests) or against the
   private ``hermes-finance-vector`` container (``url=``).

FAIL-CLOSED semantics (Loop.md §5.10): every vector operation raises
:class:`KnowledgeUnavailable` when the backend is unreachable. The decision
layer must treat that as "no research → no research-dependent new entries"
(use :func:`research_ready`). The facts archive and document store keep
working when the vector index is down — storage never depends on search.

Retrieval always returns source attribution (link + publisher + snippet),
never untraceable "model facts" (Loop.md §5.10).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field as PydanticField, field_validator
from sqlalchemy import MetaData, func
from sqlmodel import Field, Session, SQLModel, create_engine, select

from swing_trader.log import get_logger

__all__ = [
    "COLLECTION_NAME",
    "DocType",
    "DocumentStore",
    "EmbeddingProvider",
    "FactsArchive",
    "FinanceKnowledge",
    "HashingEmbedder",
    "HASHING_EMBEDDER_VERSION",
    "KnowledgeIndex",
    "KnowledgeUnavailable",
    "LicenseStatus",
    "ResearchDocument",
    "OpenAIEmbedder",
    "OPENAI_COLLECTION_NAME",
    "OPENAI_EMBEDDING_DIM",
    "OPENAI_EMBEDDING_MODEL",
    "SNIPPET_CHARS",
    "content_hash",
    "normalize_text",
    "research_ready",
]

logger = get_logger(__name__)

#: Name of the Qdrant collection (Loop.md §5.10).
COLLECTION_NAME = "finance_knowledge"

#: Production semantic-retrieval profile.  The collection name encodes both
#: provider/model and dimension because Qdrant collections cannot mix vector
#: dimensions and must not silently mix model spaces of the same dimension.
#: The legacy ``finance_knowledge`` hashing collection remains untouched as a
#: rollback path during the Phase-0 migration.
OPENAI_EMBEDDING_MODEL = "text-embedding-3-small"
OPENAI_EMBEDDING_DIM = 1536
OPENAI_COLLECTION_NAME = "finance_knowledge_openai_te3s_1536"

#: Version stamped into every vector payload.  Changing the hashing algorithm
#: or dimension requires a new collection/backfill rather than silently mixing
#: incompatible vectors in one collection.
HASHING_EMBEDDER_VERSION = "hashing-blake2b-v1"

#: Snippet length returned by facade search (source attribution, not full text).
SNIPPET_CHARS = 300


class KnowledgeUnavailable(RuntimeError):
    """The vector backend is unreachable or failed (Loop.md §5.10).

    Fail-closed: callers must treat this as "research retrieval is DOWN" —
    research-dependent new entries must not proceed. Ledger and archive
    records are unaffected.
    """


# ------------------------------------------------------------------ helpers


def normalize_text(text: str) -> str:
    """Whitespace-collapsed, stripped text used for content hashing.

    Two fetches of the same article that differ only in whitespace /
    line-wrapping hash identically and therefore dedupe.
    """
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    """sha256 hex digest of the normalized text (dedupe key)."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def _to_iso(dt: datetime) -> str:
    """Normalize to UTC and serialize as ISO-8601 TEXT (tz preserved)."""
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (use UTC)")
    return dt.astimezone(timezone.utc).isoformat()


def _from_iso(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:  # defensive: should never happen with _to_iso
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex


# ------------------------------------------------------- layer 1: facts

#: Safe archive kinds: no path separators, no dots — payload kind can never
#: escape the partition directory.
_KIND_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class FactsArchive:
    """Immutable dated raw-source archive (Loop.md §5.10 layer 1).

    One JSONL file per (trading date, kind):
    ``<root>/<YYYY>/<YYYY-MM-DD>/<kind>.jsonl``. APPEND-ONLY: there is
    deliberately no delete/update API — raw sources are retained by trading
    date rather than discarded (Loop.md §5.8/§5.10).
    """

    def __init__(self, root_dir: Path | str) -> None:
        self._root = Path(root_dir)

    def _path(self, kind: str, trading_date: date) -> Path:
        if not _KIND_RE.match(kind):
            raise ValueError(
                f"invalid archive kind {kind!r}: must match {_KIND_RE.pattern}"
            )
        return self._root / f"{trading_date:%Y}" / trading_date.isoformat() / f"{kind}.jsonl"

    def write(self, kind: str, payload: dict, trading_date: date) -> Path:
        """Append one JSON line to the (trading_date, kind) partition."""
        path = self._path(kind, trading_date)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(payload, default=str, ensure_ascii=False)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return path

    def read(self, kind: str, trading_date: date) -> list[dict]:
        """Parsed JSON lines for (trading_date, kind); [] if none archived."""
        path = self._path(kind, trading_date)
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------- layer 2: research documents


class DocType(str, Enum):
    NEWS = "news"
    FILING = "filing"
    EARNINGS = "earnings"
    RESEARCH = "research"
    NOTE = "note"
    SNAPSHOT = "snapshot"


class LicenseStatus(str, Enum):
    PUBLIC = "public"
    OWNED = "owned"
    LICENSED = "licensed"
    RESTRICTED = "restricted"  # NEVER ingested (Loop.md §5.10)


class ResearchDocument(BaseModel):
    """Normalized research document (Loop.md §5.10 layer 2).

    ``id`` and ``content_hash`` are assigned by :meth:`DocumentStore.ingest`;
    provenance fields are Optional at the model level so ingest can enforce
    them with a clear error.
    """

    model_config = ConfigDict(validate_assignment=True)

    id: Optional[str] = None
    title: str
    text: str
    source_url: Optional[str] = None
    publisher: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    symbols: list[str] = PydanticField(default_factory=list)
    themes: list[str] = PydanticField(default_factory=list)
    event_ts: Optional[datetime] = None
    trading_date_et: date
    doc_type: DocType = DocType.NOTE
    license_status: LicenseStatus = LicenseStatus.PUBLIC
    parser_version: str = "v0"
    content_hash: Optional[str] = None

    @field_validator("symbols")
    @classmethod
    def _normalize_symbols(cls, v: list[str]) -> list[str]:
        return [s.strip().upper() for s in v if s.strip()]


# Private MetaData: the research_documents table is NOT registered on the
# global SQLModel.metadata, so Ledger.create_all can never create it in the
# ledger DB and DocumentStore.create_all can never create ledger tables here
# (Loop.md §5.10: own database file, never the ledger DB).
KNOWLEDGE_METADATA = MetaData()


class _KnowledgeTable(SQLModel):
    metadata = KNOWLEDGE_METADATA


class ResearchDocumentRow(_KnowledgeTable, table=True):
    __tablename__ = "research_documents"

    id: str = Field(primary_key=True)
    title: str
    text: str
    source_url: str
    publisher: str
    retrieved_at: str  # ISO-8601 UTC
    content_hash: str = Field(index=True, unique=True)
    symbols: str = "[]"  # JSON TEXT list
    themes: str = "[]"  # JSON TEXT list
    event_ts: Optional[str] = None  # ISO-8601 UTC | null
    trading_date_et: str = Field(index=True)  # YYYY-MM-DD (ET trading date)
    doc_type: str = Field(index=True)
    license_status: str
    parser_version: str = "v0"


def _doc_to_row(doc: ResearchDocument, doc_id: str, digest: str) -> ResearchDocumentRow:
    assert doc.source_url and doc.publisher and doc.retrieved_at  # ingest validated
    return ResearchDocumentRow(
        id=doc_id,
        title=doc.title,
        text=doc.text,
        source_url=doc.source_url,
        publisher=doc.publisher,
        retrieved_at=_to_iso(doc.retrieved_at),
        content_hash=digest,
        symbols=json.dumps(doc.symbols),
        themes=json.dumps(doc.themes),
        event_ts=_to_iso(doc.event_ts) if doc.event_ts is not None else None,
        trading_date_et=doc.trading_date_et.isoformat(),
        doc_type=doc.doc_type.value,
        license_status=doc.license_status.value,
        parser_version=doc.parser_version,
    )


def _doc_from_row(row: ResearchDocumentRow) -> ResearchDocument:
    return ResearchDocument(
        id=row.id,
        title=row.title,
        text=row.text,
        source_url=row.source_url,
        publisher=row.publisher,
        retrieved_at=_from_iso(row.retrieved_at),
        content_hash=row.content_hash,
        symbols=json.loads(row.symbols),
        themes=json.loads(row.themes),
        event_ts=_from_iso(row.event_ts) if row.event_ts else None,
        trading_date_et=date.fromisoformat(row.trading_date_et),
        doc_type=DocType(row.doc_type),
        license_status=LicenseStatus(row.license_status),
        parser_version=row.parser_version,
    )


class DocumentStore:
    """SQLite research-document store (Loop.md §5.10 layer 2).

    MUST point at its own database file — never the trading Ledger's. The
    table lives on a private MetaData so the two stores cannot create each
    other's tables even if both are opened in one process.
    """

    def __init__(self, url: str = "sqlite:///knowledge.db") -> None:
        self._engine = create_engine(url)
        KNOWLEDGE_METADATA.create_all(self._engine)
        logger.info("document store ready", extra={"url": url})

    def ingest(self, doc: ResearchDocument) -> str:
        """Persist a document; returns its id.

        - Provenance is MANDATORY: ``source_url``, ``publisher`` and
          ``retrieved_at`` must all be set (Loop.md §5.10: preserve
          citations; sources must stay traceable) -> ValueError otherwise.
        - ``license_status="restricted"`` is REFUSED: Loop.md §5.10 —
          public/owned/licensed material only; never bypass paywalls,
          credentials, robots controls, or copyright restrictions.
        - Dedupes by ``content_hash``: re-ingesting the same normalized text
          returns the existing document's id.
        """
        missing = [
            name
            for name, value in (
                ("source_url", doc.source_url),
                ("publisher", doc.publisher),
                ("retrieved_at", doc.retrieved_at),
            )
            if not value
        ]
        if missing:
            raise ValueError(
                "provenance is mandatory (Loop.md §5.10): missing "
                + ", ".join(missing)
            )
        if doc.license_status is LicenseStatus.RESTRICTED:
            raise ValueError(
                "refusing to ingest restricted-license material: Loop.md §5.10 "
                "permits public/owned/licensed sources only — never bypass "
                "paywalls, credentials, or license terms"
            )

        digest = content_hash(doc.text)
        with Session(self._engine) as session:
            existing = session.exec(
                select(ResearchDocumentRow).where(
                    ResearchDocumentRow.content_hash == digest
                )
            ).first()
            if existing is not None:
                logger.info(
                    "duplicate document (same content_hash) — returning existing id",
                    extra={"document_id": existing.id, "content_hash": digest},
                )
                return existing.id
            doc_id = doc.id or _new_id()
            session.add(_doc_to_row(doc, doc_id, digest))
            session.commit()
        logger.info(
            "document ingested",
            extra={"document_id": doc_id, "doc_type": doc.doc_type.value},
        )
        return doc_id

    def get(self, doc_id: str) -> Optional[ResearchDocument]:
        with Session(self._engine) as session:
            row = session.get(ResearchDocumentRow, doc_id)
            return _doc_from_row(row) if row is not None else None

    def find(
        self,
        symbol: str | None = None,
        doc_type: DocType | str | None = None,
        trading_date: date | str | None = None,
    ) -> list[ResearchDocument]:
        """Filter by symbol tag, document type and/or ET trading date."""
        with Session(self._engine) as session:
            stmt = select(ResearchDocumentRow)
            if doc_type is not None:
                stmt = stmt.where(
                    ResearchDocumentRow.doc_type == DocType(doc_type).value
                )
            if trading_date is not None:
                raw = (
                    trading_date.isoformat()
                    if isinstance(trading_date, date)
                    else trading_date
                )
                stmt = stmt.where(ResearchDocumentRow.trading_date_et == raw)
            rows = session.exec(stmt).all()
        docs = [_doc_from_row(r) for r in rows]
        if symbol is not None:
            wanted = symbol.strip().upper()
            docs = [d for d in docs if wanted in d.symbols]
        docs.sort(key=lambda d: (d.retrieved_at or datetime.min.replace(tzinfo=timezone.utc)))
        return docs

    def count(self) -> int:
        with Session(self._engine) as session:
            return session.exec(
                select(func.count()).select_from(ResearchDocumentRow)
            ).one()


# ------------------------------------------------- layer 3: vector index


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding port (Loop.md §8: configurable embedding provider)."""

    def embed(self, texts: list[str]) -> list[list[float]]: ...


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:'[a-z0-9]+)?")


class HashingEmbedder:
    """Deterministic offline bag-of-words feature-hashing embedder.

    *** NOT a semantic embedding. *** This is a Phase-0 OFFLINE PLACEHOLDER:
    each lowercase token is feature-hashed (signed, via blake2b — stable
    across processes, unlike Python's randomized ``hash()``) into a fixed
    ``dim``-sized vector, then L2-normalized so cosine similarity behaves as
    weighted token overlap. It retrieves lexically similar text only; it
    knows nothing about meaning, synonyms, or finance. Swap in a real
    embedding provider via the :class:`EmbeddingProvider` port when one is
    configured (Loop.md §8: configurable embedding provider) — the rest of
    the knowledge store is agnostic to the provider.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self.dim = dim
        self.model_id = HASHING_EMBEDDER_VERSION

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _TOKEN_RE.findall(text.lower()):
            h = int.from_bytes(
                hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big"
            )
            sign = 1.0 if (h >> 63) & 1 == 0 else -1.0
            vec[h % self.dim] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0.0:
            vec = [v / norm for v in vec]
        return vec


class OpenAIEmbedder:
    """OpenAI v3 embedding adapter with batched, order-stable responses."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = OPENAI_EMBEDDING_MODEL,
        dimensions: int = OPENAI_EMBEDDING_DIM,
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
        max_attempts: int = 3,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenAI embedding API key is required")
        if dimensions < 1:
            raise ValueError("dimensions must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._api_key = api_key.strip()
        self.model = model
        self.dim = dimensions
        self.model_id = f"openai:{model}:{dimensions}"
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        import requests

        response = None
        for attempt in range(self._max_attempts):
            try:
                response = requests.post(
                    f"{self._base_url}/embeddings",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "input": texts,
                        "dimensions": self.dim,
                        "encoding_format": "float",
                    },
                    timeout=self._timeout,
                )
                response.raise_for_status()
                break
            except Exception as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                retryable = status is None or status == 429 or status >= 500
                if not retryable or attempt + 1 >= self._max_attempts:
                    # Suppress chaining so credentials cannot leak through a
                    # third-party exception representation.
                    raise RuntimeError(
                        f"OpenAI embedding request failed: {type(exc).__name__}"
                    ) from None
                time.sleep(0.5 * (2**attempt))
        assert response is not None
        try:
            data = response.json().get("data") or []
            ordered = sorted(data, key=lambda item: int(item["index"]))
            vectors = [list(map(float, item["embedding"])) for item in ordered]
        except Exception as exc:
            raise RuntimeError(
                f"OpenAI embedding response invalid: {type(exc).__name__}"
            ) from None
        if len(vectors) != len(texts) or any(len(vector) != self.dim for vector in vectors):
            raise RuntimeError("OpenAI embedding response shape mismatch")
        return vectors


class KnowledgeIndex:
    """Qdrant-backed ``finance_knowledge`` index (Loop.md §5.10 layer 3).

    Exactly one of:

    - ``path=`` — embedded local persistence (small local corpus; fully
      offline; what the tests use), or
    - ``url=``  — the dedicated private ``hermes-finance-vector`` container
      (internal Docker network, no published host port).

    Points carry ``document_id`` + metadata payloads that point BACK to
    :class:`DocumentStore` rows; the index never stores authoritative facts.

    FAIL-CLOSED: :meth:`index`, :meth:`search` and :meth:`available` raise
    :class:`KnowledgeUnavailable` whenever the backend is unreachable or
    errors. Callers that want a boolean use :func:`research_ready`.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        url: str | None = None,
        embedder: EmbeddingProvider | None = None,
        collection: str = COLLECTION_NAME,
        timeout: float = 5.0,
    ) -> None:
        if (path is None) == (url is None):
            raise ValueError("provide exactly one of path= (embedded) or url= (remote)")
        # Lazy import: qdrant-client is the optional "knowledge" extra; the
        # archive/document layers must import without it.
        from qdrant_client import QdrantClient, models

        self._models = models
        self._collection = collection
        self._embedder: EmbeddingProvider = embedder or HashingEmbedder()
        self._collection_ready_dim: int | None = None
        if path is not None:
            self._where = f"embedded:{path}"
            self._client = QdrantClient(path=str(path))
        else:
            self._where = url
            self._client = QdrantClient(
                url=url, timeout=timeout, check_compatibility=False
            )
        logger.info("knowledge index configured", extra={"backend": self._where})

    # ------------------------------------------------------------ internals

    def _unavailable(self, exc: Exception) -> KnowledgeUnavailable:
        logger.warning(
            "vector backend unavailable — failing closed (Loop.md §5.10)",
            extra={"backend": self._where, "error": str(exc)},
        )
        return KnowledgeUnavailable(
            f"finance_knowledge vector backend unavailable ({self._where}): {exc}"
        )

    def _ensure_collection(self, dim: int) -> None:
        if self._collection_ready_dim == dim:
            return
        if self._client.collection_exists(self._collection):
            info = self._client.get_collection(self._collection)
            configured = info.config.params.vectors
            existing_dim = getattr(configured, "size", None)
            if existing_dim is None or int(existing_dim) != dim:
                raise ValueError(
                    f"collection {self._collection!r} has dimension "
                    f"{existing_dim!r}, expected {dim}; use a versioned collection"
                )
            self._collection_ready_dim = dim
            return
        self._client.create_collection(
            self._collection,
            vectors_config=self._models.VectorParams(
                size=dim, distance=self._models.Distance.COSINE
            ),
        )
        self._collection_ready_dim = dim

    @staticmethod
    def _point_id(doc_id: str) -> str:
        # Deterministic UUID per document id: re-indexing overwrites in place.
        return str(uuid.uuid5(uuid.NAMESPACE_URL, doc_id))

    @property
    def embedding_dim(self) -> int | None:
        """Configured vector dimension when exposed by the provider."""
        value = getattr(self._embedder, "dim", None)
        return int(value) if value is not None else None

    @property
    def embedding_model(self) -> str:
        """Stable provider/model identifier stamped into payload metadata."""
        return str(
            getattr(self._embedder, "model_id", self._embedder.__class__.__name__)
        )

    # ------------------------------------------------------------ operations

    def index(self, doc_id: str, text: str, payload: dict[str, Any] | None = None) -> None:
        """Embed ``text`` and upsert one point referencing ``doc_id``.

        ``payload`` should carry {document_id, symbols, doc_type,
        trading_date, publisher}; ``document_id`` is always enforced.
        Raises :class:`KnowledgeUnavailable` if the backend is unreachable.
        """
        self.index_many([(doc_id, text, payload or {})])

    def index_many(
        self,
        records: list[tuple[str, str, dict[str, Any]]],
    ) -> int:
        """Embed and idempotently upsert a batch of document references.

        Point IDs are deterministic, so replaying a migration updates points
        in place instead of duplicating them.  Returns the number submitted.
        """
        if not records:
            return 0
        try:
            vectors = self._embedder.embed([text for _, text, _ in records])
            if len(vectors) != len(records):
                raise ValueError("embedding provider returned the wrong vector count")
            if not vectors or not vectors[0]:
                raise ValueError("embedding provider returned an empty vector")
            self._ensure_collection(len(vectors[0]))
            self._client.upsert(
                self._collection,
                points=[
                    self._models.PointStruct(
                        id=self._point_id(doc_id),
                        vector=vector,
                        payload={**payload, "document_id": doc_id},
                    )
                    for (doc_id, _, payload), vector in zip(records, vectors, strict=True)
                ],
            )
        except Exception as exc:  # fail closed (Loop.md §5.10)
            raise self._unavailable(exc) from exc
        return len(records)

    def count(self) -> int:
        """Exact number of indexed points; zero before collection creation."""
        try:
            if not self._client.collection_exists(self._collection):
                return 0
            return int(
                self._client.count(
                    collection_name=self._collection,
                    exact=True,
                ).count
            )
        except Exception as exc:  # fail closed (Loop.md §5.10)
            raise self._unavailable(exc) from exc

    def document_ids(self) -> set[str]:
        """Return every authoritative document ID currently in the index.

        Migration uses this payload-only scroll to repair a partial collection
        without re-embedding every historical document.  Vectors are never
        transferred back to the process.
        """
        try:
            if not self._client.collection_exists(self._collection):
                return set()
            found: set[str] = set()
            offset = None
            while True:
                records, next_offset = self._client.scroll(
                    collection_name=self._collection,
                    limit=512,
                    offset=offset,
                    with_payload=["document_id"],
                    with_vectors=False,
                )
                for record in records:
                    payload = record.payload or {}
                    doc_id = payload.get("document_id")
                    if isinstance(doc_id, str) and doc_id:
                        found.add(doc_id)
                if next_offset is None:
                    break
                if next_offset == offset:  # defensive against a broken backend cursor
                    raise RuntimeError("Qdrant scroll cursor did not advance")
                offset = next_offset
            return found
        except Exception as exc:  # fail closed (Loop.md §5.10)
            raise self._unavailable(exc) from exc

    def delete_document_ids(self, document_ids: set[str]) -> int:
        """Delete stale points by deterministic document-derived point ID."""
        if not document_ids:
            return 0
        try:
            point_ids = [self._point_id(doc_id) for doc_id in sorted(document_ids)]
            for start in range(0, len(point_ids), 512):
                self._client.delete(
                    collection_name=self._collection,
                    points_selector=point_ids[start : start + 512],
                    wait=True,
                )
            return len(point_ids)
        except Exception as exc:  # fail closed (Loop.md §5.10)
            raise self._unavailable(exc) from exc

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Top-``k`` hits as ``{document_id, score, payload}`` dicts.

        Returns [] when nothing has been indexed yet; raises
        :class:`KnowledgeUnavailable` if the backend is unreachable.
        """
        try:
            vectors = self._embedder.embed([query])
            if len(vectors) != 1 or not vectors[0]:
                raise ValueError("embedding provider returned an invalid query vector")
            vector = vectors[0]
            if not self._client.collection_exists(self._collection):
                return []
            res = self._client.query_points(
                self._collection, query=vector, limit=k, with_payload=True
            )
        except Exception as exc:  # fail closed (Loop.md §5.10)
            raise self._unavailable(exc) from exc
        return [
            {
                "document_id": (p.payload or {}).get("document_id"),
                "score": p.score,
                "payload": p.payload or {},
            }
            for p in res.points
        ]

    def available(self) -> bool:
        """Ping the backend; True when it responds.

        FAIL-CLOSED: raises :class:`KnowledgeUnavailable` (rather than
        returning False) when unreachable, like every other operation; use
        :func:`research_ready` for a non-raising boolean.
        """
        try:
            self._client.get_collections()
        except Exception as exc:
            raise self._unavailable(exc) from exc
        return True


def research_ready(index: KnowledgeIndex | None) -> bool:
    """Non-raising readiness check for the decision layer (Loop.md §5.10).

    False when no index is configured or the backend is unreachable —
    research-dependent new entries must fail closed in that case.
    """
    if index is None:
        return False
    try:
        return index.available()
    except KnowledgeUnavailable:
        return False


# ------------------------------------------------------------------ facade


class FinanceKnowledge:
    """Facade over the document store + optional vector index.

    Storage never depends on search: :meth:`ingest_and_index` ALWAYS
    persists the document, and indexes only when the vector layer is up.
    :meth:`search` returns results WITH source attribution (link, publisher,
    snippet) — never untraceable facts (Loop.md §5.10).
    """

    def __init__(
        self, documents: DocumentStore, index: KnowledgeIndex | None = None
    ) -> None:
        self._documents = documents
        self._index = index

    def ingest_and_index(self, doc: ResearchDocument) -> str:
        """Persist ``doc`` (mandatory) and index it (best-effort).

        The document write always happens first; if the vector index is not
        configured or unreachable, indexing is skipped with a warning and
        the stored document id is still returned (Loop.md §5.10: the vector
        DB is search infrastructure, not a storage dependency).
        """
        doc_id = self._documents.ingest(doc)
        if self._index is None:
            logger.info(
                "no vector index configured — document stored, not indexed",
                extra={"document_id": doc_id},
            )
            return doc_id
        stored = self._documents.get(doc_id)
        assert stored is not None  # just ingested (or deduped to existing)
        try:
            self._index.index(
                doc_id,
                stored.text,
                payload={
                    "document_id": doc_id,
                    "symbols": stored.symbols,
                    "doc_type": stored.doc_type.value,
                    "trading_date": stored.trading_date_et.isoformat(),
                    "publisher": stored.publisher,
                    "embedding_model": self._index.embedding_model,
                    "embedding_dim": self._index.embedding_dim,
                },
            )
        except KnowledgeUnavailable:
            logger.warning(
                "vector index down — document stored, indexing skipped "
                "(retrieval fails closed, Loop.md §5.10)",
                extra={"document_id": doc_id},
            )
        return doc_id

    def search(self, query: str, k: int = 5) -> list[dict[str, Any]]:
        """Semantic search resolved back to stored documents.

        Each result: ``{document, snippet, source_url, publisher, score}`` —
        always source-attributed (Loop.md §5.10: return links/snippets,
        preserve citations). Raises :class:`KnowledgeUnavailable` when no
        index is configured or the backend is down (fail closed).
        """
        if self._index is None:
            raise KnowledgeUnavailable(
                "no vector index configured — research retrieval fails closed "
                "(Loop.md §5.10)"
            )
        results: list[dict[str, Any]] = []
        for hit in self._index.search(query, k=k):
            doc_id = hit["document_id"]
            doc = self._documents.get(doc_id) if doc_id else None
            if doc is None:
                logger.warning(
                    "vector hit does not resolve to a stored document — dropped",
                    extra={"document_id": doc_id},
                )
                continue
            results.append(
                {
                    "document": doc,
                    "snippet": doc.text[:SNIPPET_CHARS],
                    "source_url": doc.source_url,
                    "publisher": doc.publisher,
                    "score": hit["score"],
                }
            )
        return results
