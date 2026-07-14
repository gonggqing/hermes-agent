"""Explicit CN↔HK synthesis built from two independent persisted briefs."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

__all__ = ["CrossMarketTheme", "MarketState", "ResearchSynthesis", "build_cn_hk_synthesis"]


class MarketState(BaseModel):
    market: str
    available: bool
    as_of: Optional[str] = None
    trading_date: Optional[str] = None
    freshness_status: str = "missing"
    regime: Optional[str] = None


class CrossMarketTheme(BaseModel):
    theme: str
    cn_symbols: list[str] = Field(default_factory=list)
    hk_symbols: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)


class ResearchSynthesis(BaseModel):
    as_of: datetime
    status: str
    markets: dict[str, MarketState]
    shared_themes: list[CrossMarketTheme] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _state(market: str, brief: Optional[dict[str, Any]]) -> MarketState:
    if not brief:
        return MarketState(market=market, available=False)
    freshness = brief.get("freshness") or {}
    regime = brief.get("regime") or {}
    return MarketState(
        market=market,
        available=True,
        as_of=str(brief.get("as_of") or "") or None,
        trading_date=str(brief.get("trading_date") or "") or None,
        freshness_status=str(freshness.get("status") or "unknown"),
        regime=str(regime.get("risk_on_off") or "") or None,
    )


def _themes(brief: Optional[dict[str, Any]]) -> dict[str, tuple[set[str], set[str]]]:
    found: dict[str, tuple[set[str], set[str]]] = {}
    if not brief:
        return found
    for row in brief.get("themes") or []:
        key = str(row.get("theme") or row.get("name") or "").strip()
        if not key:
            continue
        symbols, urls = found.setdefault(key, (set(), set()))
        for symbol in row.get("leaders") or []:
            symbols.add(str(symbol))
    discovery = brief.get("discovery") or {}
    for row in discovery.get("candidates") or []:
        key = str(row.get("theme") or "").strip()
        if not key:
            continue
        symbols, urls = found.setdefault(key, (set(), set()))
        symbols.add(str(row.get("symbol") or ""))
        for evidence in row.get("evidence") or []:
            url = str(evidence.get("url") or "")
            if url:
                urls.add(url)
    return found


def build_cn_hk_synthesis(
    cn: Optional[dict[str, Any]],
    hk: Optional[dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> ResearchSynthesis:
    """Join only explicit common themes; never copy regime/freshness fields."""
    cn_state, hk_state = _state("CN", cn), _state("HK", hk)
    cn_themes, hk_themes = _themes(cn), _themes(hk)
    shared = []
    for theme in sorted(set(cn_themes).intersection(hk_themes)):
        cn_symbols, cn_urls = cn_themes[theme]
        hk_symbols, hk_urls = hk_themes[theme]
        shared.append(CrossMarketTheme(
            theme=theme,
            cn_symbols=sorted(s for s in cn_symbols if s),
            hk_symbols=sorted(s for s in hk_symbols if s),
            evidence_urls=sorted(cn_urls | hk_urls),
        ))
    both = cn_state.available and hk_state.available
    notes = [] if both else [
        "synthesis degraded: CN and HK must each publish an independent brief"
    ]
    return ResearchSynthesis(
        as_of=now or datetime.now(timezone.utc),
        status="complete" if both else "degraded",
        markets={"cn": cn_state, "hk": hk_state},
        shared_themes=shared,
        notes=notes,
    )
