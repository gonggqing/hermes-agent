"""Retrieval-augmented grounding for the analysis LLM (Loop.md Phase 0.75).

Pulls source-attributed research from the local knowledge store (§5.10) so the
LLM analyst reasons over RETRIEVED documents with citations rather than
untraceable model facts. Everything here is **fail-closed**: no store, an
unconfigured vector index, or a backend outage yields an empty context — the
LLM simply falls back to its ungrounded opinion, and rule-based agents remain in
charge (§3). Retrieval NEVER raises into the trading loop.
"""

from __future__ import annotations

from typing import Any

from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["retrieve_research", "research_snippets", "research_sources"]

_SNIPPET_CHARS = 240


def retrieve_research(
    knowledge: Any,
    index: Any,
    query: str,
    *,
    k: int = 4,
) -> list[dict]:
    """Source-attributed hits for ``query``; ``[]`` on any unavailability.

    Hits are the JSON dicts from :func:`~swing_trader.knowledge_pipeline.
    search_knowledge` (``{document_id, title, snippet, source_url, publisher,
    score, trading_date}``)."""
    if knowledge is None or not query or not query.strip():
        return []
    try:
        from swing_trader.knowledge_pipeline import search_knowledge

        return search_knowledge(knowledge, index, query, k=k)
    except Exception as exc:  # noqa: BLE001 — fail closed, never raise
        logger.debug("rag retrieval unavailable", extra={"error": str(exc)[:160]})
        return []


def research_snippets(hits: list[dict], limit: int = 4) -> list[str]:
    """Compact ``[publisher] snippet`` strings for the LLM prompt context."""
    out: list[str] = []
    for h in hits[:limit]:
        pub = str(h.get("publisher") or "?")
        snip = str(h.get("snippet") or "").strip()[:_SNIPPET_CHARS]
        if snip:
            out.append(f"[{pub}] {snip}")
    return out


def research_sources(hits: list[dict], limit: int = 4) -> list[dict]:
    """Deduped citations (title/url/publisher) for display and provenance."""
    seen: set[str] = set()
    out: list[dict] = []
    for h in hits[:limit]:
        url = str(h.get("source_url") or "").strip()
        key = url or str(h.get("document_id") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append({
            "title": h.get("title"),
            "url": url,
            "publisher": h.get("publisher"),
            "trading_date": h.get("trading_date"),
        })
    return out
