"""RAG grounding for the analysis LLM (Loop.md Phase 0.75) — fail-closed retrieval
+ citations wired into on-demand analyze."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import swing_trader.knowledge_pipeline as kp
from swing_trader import rag
from swing_trader.interfaces import Bar, DataFeed, NewsItem, Quote
from swing_trader.on_demand import analyze_symbol
from swing_trader.schemas import Direction, Signal

UTC = timezone.utc
T0 = datetime(2026, 3, 1, tzinfo=UTC)

_HITS = [
    {"document_id": "d1", "title": "NVDA cloud strength", "snippet": "NVDA beats on data-center",
     "source_url": "https://x/1", "publisher": "Reuters", "score": 0.9, "trading_date": "2026-07-13"},
    {"document_id": "d2", "title": "dup", "snippet": "more context",
     "source_url": "https://x/1", "publisher": "Reuters", "score": 0.5, "trading_date": "2026-07-13"},
]


# ------------------------------------------------------------- retrieval

def test_retrieve_fail_closed(monkeypatch):
    assert rag.retrieve_research(None, None, "q") == []          # no store
    assert rag.retrieve_research(object(), None, "  ") == []      # empty query
    monkeypatch.setattr(kp, "search_knowledge",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert rag.retrieve_research(object(), None, "nvda") == []    # backend down


def test_retrieve_success_and_helpers(monkeypatch):
    monkeypatch.setattr(kp, "search_knowledge", lambda *a, **k: _HITS)
    hits = rag.retrieve_research(object(), object(), "nvda")
    assert hits == _HITS
    assert rag.research_snippets(hits) == ["[Reuters] NVDA beats on data-center",
                                           "[Reuters] more context"]
    srcs = rag.research_sources(hits)
    assert len(srcs) == 1 and srcs[0]["url"] == "https://x/1"  # deduped by url


# ------------------------------------------------------- analyze_symbol RAG

class _Feed(DataFeed):
    def __init__(self):
        self._bars = {"NVDA": [Bar(symbol="NVDA", ts=T0 + timedelta(days=i),
                                    open=c - 0.2, high=c + 0.3, low=c - 0.3,
                                    close=c, volume=5e6)
                               for i, c in enumerate(100 + 0.5 * i for i in range(90))]}

    def get_quote(self, s): return Quote(symbol=s, ts=T0, last=self._bars[s][-1].close)
    def get_bars(self, s, timeframe="1d", limit=100): return self._bars[s][-limit:]
    def get_news(self, s=None, limit=20):
        return [NewsItem(symbol="NVDA", ts=T0, headline="NVDA earnings beat",
                         source="Reuters", url="https://x/9", sentiment=0.7)]


class _LLM:
    def __init__(self): self.research_seen = None
    def analyze(self, symbol, features, headlines, regime="neutral", research=None):
        self.research_seen = research
        return Signal(source_agent="llm:test", symbol=symbol, thesis="grounded",
                      direction=Direction.LONG, confidence=0.6)


def test_analyze_symbol_grounds_llm_and_returns_citations(monkeypatch):
    monkeypatch.setattr(kp, "search_knowledge", lambda *a, **k: _HITS)
    llm = _LLM()
    result = analyze_symbol(_Feed(), "NVDA", llm_analyst=llm,
                            knowledge=object(), knowledge_index=object(), now=T0)
    # LLM voice received RAG snippets...
    assert llm.research_seen == ["[Reuters] NVDA beats on data-center",
                                 "[Reuters] more context"]
    # ...and the result surfaces deduped source citations.
    assert result["research"] and result["research"][0]["url"] == "https://x/1"
    assert "llm:test" in {s["source_agent"] for s in result["signals"]}


def test_analyze_symbol_without_knowledge_has_no_research():
    result = analyze_symbol(_Feed(), "NVDA", now=T0)  # no knowledge store
    assert result["research"] == []
