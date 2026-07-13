"""On-demand single-symbol analysis (Loop.md Phase 0.75 thrust B).

Shared by the ``/v1/analyze`` API endpoint and the finance Telegram bot's
@mention / DM replies, so both give the SAME multi-agent read. READ-ONLY: it
forms a thesis (technical + fundamental + sentiment synthesized by the bull/bear
debate, plus the optional LLM voice) — it never proposes or places an order
(Loop.md §3). Deterministic given its inputs; ``now`` is injectable.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from swing_trader.analysis import (
    DebateAgent,
    FundamentalAgent,
    SentimentAgent,
    TechnicalAgent,
)
from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import DataFeed, NewsItem
from swing_trader.log import get_logger
from swing_trader.monitors import score_headline

logger = get_logger(__name__)

__all__ = ["analyze_symbol", "extract_symbols", "render_analysis_zh"]

_ANALYSIS_BARS = 120

#: Ticker shapes: US (1-5 upper letters), HK/mainland (digits + .HK/.SS/.SZ),
#: or an index (^SYM). Deliberately permissive — unknown symbols just fail to
#: load data and are reported honestly rather than guessed.
_TICKER_RE = re.compile(r"\b(\^?[A-Z]{1,5}|\d{4,6}\.(?:HK|SS|SZ))\b")

#: Common English words that match the US-ticker shape but are never tickers
#: here — filtered so "@bot analyze THE AI stock" doesn't try to price "THE".
_STOPWORDS = frozenset({
    "THE", "A", "AN", "AND", "OR", "IS", "IT", "TO", "OF", "IN", "ON", "FOR",
    "ME", "MY", "WE", "US", "AI", "OK", "PE", "HK", "CN", "USD", "ETF", "IPO",
    "BUY", "SELL", "NOW", "TODAY", "K", "DM", "BOT",
})


def extract_symbols(text: str, limit: int = 3) -> list[str]:
    """Pull likely ticker symbols out of free chat text (deduped, ordered).

    Matches on the ORIGINAL casing: a bare US ticker must be written in caps
    (NVDA, TSLA) so ordinary lowercase words ("stock", "great") are not mistaken
    for tickers; HK/mainland symbols (0700.HK, 600519.SS) and ^INDEX are
    unambiguous. Common all-caps non-tickers (AI, ETF, ...) are filtered.
    """
    out: list[str] = []
    for m in _TICKER_RE.finditer(text):
        sym = m.group(1).upper()
        base = sym.lstrip("^")
        if "." not in sym and not sym.startswith("^") and base in _STOPWORDS:
            continue
        if sym not in out:
            out.append(sym)
        if len(out) >= limit:
            break
    return out


def analyze_symbol(
    feed: DataFeed,
    symbol: str,
    *,
    fundamentals: Any = None,
    llm_analyst: Any = None,
    knowledge: Any = None,
    knowledge_index: Any = None,
    now: Optional[datetime] = None,
) -> dict:
    """One-shot multi-agent analysis of ``symbol``. Raises DataFeedError only
    when no bars are available; otherwise degrades gracefully.

    When ``knowledge`` is supplied, the LLM voice is RAG-grounded on retrieved
    research (fail-closed) and the result carries the source citations.

    Returns a JSON-friendly dict:
    ``{symbol, last, verdict, signals, news, research}`` (the API layer adds
    ``as_of``/``note``)."""
    from swing_trader.rag import research_snippets, research_sources, retrieve_research

    now = now or datetime.now(timezone.utc)
    rows = feed.get_bars(symbol, "1d", _ANALYSIS_BARS)  # DataFeedError propagates

    last: Optional[float] = None
    try:
        last = feed.get_quote(symbol).last
    except DataFeedError:
        last = rows[-1].close if rows else None

    news_scored: list[NewsItem] = []
    try:
        for n in feed.get_news(symbol, limit=12):
            s = n.sentiment if n.sentiment is not None else score_headline(n.headline)
            news_scored.append(NewsItem(symbol=n.symbol, ts=n.ts, headline=n.headline,
                                        source=n.source, url=n.url, sentiment=s))
    except DataFeedError:
        pass

    # RAG: retrieve source-attributed research to ground the LLM (fail-closed).
    query = f"{symbol} " + " ".join(n.headline for n in news_scored[:2])
    research_hits = retrieve_research(knowledge, knowledge_index, query, k=4)

    signals = []
    tech = TechnicalAgent().analyze(symbol, rows)
    if tech is not None:
        signals.append(tech)
    if fundamentals is not None:
        fund = FundamentalAgent(fundamentals).analyze(symbol)
        if fund is not None:
            signals.append(fund)
    senti = SentimentAgent().analyze(symbol, news_scored)
    if senti is not None:
        signals.append(senti)
    if llm_analyst is not None and tech is not None:
        llm_sig = llm_analyst.analyze(
            symbol, features=tech.features_json,
            headlines=[n.headline for n in news_scored],
            research=research_snippets(research_hits),
        )
        if llm_sig is not None:
            signals.append(llm_sig)

    verdict = DebateAgent().debate(symbol, signals) if signals else None

    def _sig(s) -> dict:
        return {"source_agent": s.source_agent, "direction": s.direction.value,
                "confidence": s.confidence, "thesis": s.thesis,
                "features": s.features_json}

    return {
        "symbol": symbol.upper(),
        "last": last,
        "verdict": _sig(verdict) if verdict is not None else None,
        "signals": [_sig(s) for s in signals],
        "news": [{"headline": n.headline, "source": n.source, "url": n.url,
                  "sentiment": n.sentiment} for n in news_scored[:6]],
        "research": research_sources(research_hits),
    }


def render_analysis_zh(result: dict) -> str:
    """Compact Chinese Telegram summary of an :func:`analyze_symbol` result."""
    sym = result.get("symbol", "?")
    last = result.get("last")
    v = result.get("verdict")
    lines = [f"📊 {sym} 快速分析" + (f" · 现价 {last:g}" if isinstance(last, (int, float)) else "")]
    if v:
        dir_zh = {"long": "偏多", "short": "偏空/回避", "neutral": "中性"}.get(v["direction"], v["direction"])
        lines.append(f"结论: {dir_zh} · 置信度 {round(v['confidence'] * 100)}%")
        if v.get("thesis"):
            lines.append(v["thesis"])
    for s in result.get("signals", []):
        if s["source_agent"] == "debate":
            continue
        dir_zh = {"long": "多", "short": "空", "neutral": "中"}.get(s["direction"], s["direction"])
        lines.append(f"· {s['source_agent']}: {dir_zh} {round(s['confidence'] * 100)}%")
    lines.append("仅供研究参考，非交易建议;下单需人工在门户确认。")
    return "\n".join(lines)
