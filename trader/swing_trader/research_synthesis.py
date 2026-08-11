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
    headline: Optional[str] = None
    summary: Optional[str] = None


class CrossMarketTheme(BaseModel):
    theme: str
    cn_symbols: list[str] = Field(default_factory=list)
    hk_symbols: list[str] = Field(default_factory=list)
    evidence_urls: list[str] = Field(default_factory=list)
    cn_strength_pct: Optional[float] = None
    hk_strength_pct: Optional[float] = None
    relationship: str = ""


class ResearchSynthesis(BaseModel):
    as_of: datetime
    status: str
    markets: dict[str, MarketState]
    shared_themes: list[CrossMarketTheme] = Field(default_factory=list)
    headline: str = ""
    analysis: list[str] = Field(default_factory=list)
    watch_next: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def _state(market: str, brief: Optional[dict[str, Any]]) -> MarketState:
    if not brief:
        return MarketState(market=market, available=False)
    freshness = brief.get("freshness") or {}
    regime = brief.get("regime") or {}
    narrative = brief.get("narrative") or {}
    if not freshness:
        freshness_status = "missing"
    elif any(bool(freshness.get(key)) for key in ("market_stale", "news_stale", "portfolio_stale")):
        freshness_status = "stale"
    else:
        freshness_status = "fresh"
    return MarketState(
        market=market,
        available=True,
        as_of=str(brief.get("as_of") or "") or None,
        trading_date=str(brief.get("trading_date") or "") or None,
        freshness_status=str(freshness.get("status") or freshness_status),
        regime=str(regime.get("risk_on_off") or "") or None,
        headline=str(narrative.get("headline") or "") or None,
        summary=str(narrative.get("summary") or "") or None,
    )


def _canonical_theme(raw: str) -> str:
    value = raw.strip().lower()
    aliases = (
        (("semiconductor", "chip", "半导体", "芯片"), "半导体与硬件"),
        (("ai", "software", "cloud", "人工智能", "软件", "云"), "AI、软件与云"),
        (("internet", "platform", "互联网", "平台"), "互联网平台"),
        (("ev", "battery", "energy", "power", "新能源", "电池", "电力"), "新能源与电力"),
        (("consumer", "electronics", "消费", "电子"), "消费与电子"),
        (("biotech", "health", "医药", "生物"), "医药与生物科技"),
        (("robot", "机器人"), "机器人产业链"),
    )
    for needles, label in aliases:
        if any(needle in value for needle in needles):
            return label
    return raw.strip()


def _themes(brief: Optional[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if not brief:
        return found
    for row in brief.get("themes") or []:
        raw = str(row.get("theme") or row.get("name") or "").strip()
        if not raw:
            continue
        key = _canonical_theme(raw)
        bucket = found.setdefault(key, {"symbols": set(), "urls": set(), "strengths": []})
        for symbol in row.get("leaders") or []:
            bucket["symbols"].add(str(symbol))
        strength = row.get("avg_dist_sma50_pct")
        if isinstance(strength, (int, float)):
            bucket["strengths"].append(float(strength))
    discovery = brief.get("discovery") or {}
    for row in discovery.get("candidates") or []:
        raw = str(row.get("theme") or "").strip()
        if not raw:
            continue
        key = _canonical_theme(raw)
        bucket = found.setdefault(key, {"symbols": set(), "urls": set(), "strengths": []})
        bucket["symbols"].add(str(row.get("symbol") or ""))
        for evidence in row.get("evidence") or []:
            url = str(evidence.get("url") or "")
            if url:
                bucket["urls"].add(url)
    return found


def _average(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _relationship(theme: str, cn: Optional[float], hk: Optional[float]) -> str:
    if cn is None or hk is None:
        return f"{theme}在一侧缺少可比强度，先比较各自证据，不做联动推断。"
    if cn >= 0 and hk >= 0:
        state = "两地同向偏强"
    elif cn < 0 and hk < 0:
        state = "两地同步承压"
    else:
        state = "A/H 表现分化"
    return (
        f"{theme}{state}：A股相对50日均线 {cn:+.1f}%，"
        f"港股 {hk:+.1f}%。这反映价格结构对照，不自动推断因果。"
    )


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
        cn_bucket = cn_themes[theme]
        hk_bucket = hk_themes[theme]
        cn_strength = _average(cn_bucket["strengths"])
        hk_strength = _average(hk_bucket["strengths"])
        shared.append(
            CrossMarketTheme(
            theme=theme,
                cn_symbols=sorted(s for s in cn_bucket["symbols"] if s),
                hk_symbols=sorted(s for s in hk_bucket["symbols"] if s),
                evidence_urls=sorted(cn_bucket["urls"] | hk_bucket["urls"]),
                cn_strength_pct=cn_strength,
                hk_strength_pct=hk_strength,
                relationship=_relationship(theme, cn_strength, hk_strength),
            )
        )
    both = cn_state.available and hk_state.available
    notes = [] if both else ["synthesis degraded: CN and HK must each publish an independent brief"]
    analysis = []
    if cn_state.summary:
        analysis.append(f"A股主模型观点：{cn_state.summary}")
    if hk_state.summary:
        analysis.append(f"港股主模型观点：{hk_state.summary}")
    analysis.extend(row.relationship for row in shared[:4])
    watch_next: list[str] = []
    for brief in (cn, hk):
        narrative = (brief or {}).get("narrative") or {}
        for item in narrative.get("watch_next") or []:
            value = str(item).strip()
            if value and value not in watch_next:
                watch_next.append(value)
    return ResearchSynthesis(
        as_of=now or datetime.now(timezone.utc),
        status="complete" if both else "degraded",
        markets={"cn": cn_state, "hk": hk_state},
        shared_themes=shared,
        headline=(
            f"A股：{cn_state.headline}｜港股：{hk_state.headline}"
            if cn_state.headline and hk_state.headline
            else (cn_state.headline or hk_state.headline or "")
        ),
        analysis=analysis[:6],
        watch_next=watch_next[:6],
        notes=notes,
    )
