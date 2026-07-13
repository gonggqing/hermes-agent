"""Render a :class:`~swing_trader.brief.ResearchBrief` as a compact Telegram
message (Loop.md two-session extension).

The REPORTER bot (shared gateway token, outbound-only) sends these — both the
CN morning research brief and the US evening context. Kept deliberately short
and clamped under Telegram's 4096-char cap. Every displayed claim carries its
freshness/uncertainty context (Loop.md §5.9: stale data is an explicit
warning, never silently shown as current); no orders are represented here —
this is research, not the approval card (that is the GATEKEEPER bot's job).

Default language is Chinese (the operator reads Chinese); ``lang="en"`` gives
an English rendering. Tickers and numbers stay as-is in both.
"""

from __future__ import annotations

from typing import Optional

from swing_trader.brief import ResearchBrief
from swing_trader.reporter import clamp

__all__ = ["render_research_brief"]

_TOP_MOVERS = 4
_TOP_THEMES = 3
_TOP_NEWS = 4
_TOP_SIGNALS = 4
_MAX_WARNINGS = 3

_L = {
    "zh": {
        "title": "投资研究简报",
        "research_only": "仅研究 · 不下单",
        "regime": "市场",
        "vix": "VIX",
        "breadth": "广度",
        "up": "领涨",
        "down": "领跌",
        "themes": "主题",
        "news": "新闻",
        "signals": "信号",
        "notes": "提示",
        "none": "无",
        "source": "数据源",
        "stale": "⚠️ 数据陈旧/缺失",
    },
    "en": {
        "title": "Investment Research Brief",
        "research_only": "research only · no orders",
        "regime": "Regime",
        "vix": "VIX",
        "breadth": "Breadth",
        "up": "Leaders",
        "down": "Laggards",
        "themes": "Themes",
        "news": "News",
        "signals": "Signals",
        "notes": "Notes",
        "none": "none",
        "source": "Source",
        "stale": "⚠️ stale/missing data",
    },
}


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:+.1f}%"


def _num(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:g}"


def render_research_brief(
    brief: ResearchBrief,
    *,
    market_label: str,
    focus_note: str = "",
    lang: str = "zh",
    max_chars: int = 3800,
) -> str:
    """Compact Telegram text for one research brief.

    ``market_label`` names the session (e.g. "China / HK", "US"). ``focus_note``
    is an optional one-liner (e.g. the CN tech focus). Returns clamped text.
    """
    t = _L.get(lang, _L["zh"])
    mode = brief.mode.value.upper()
    lines: list[str] = [
        f"📈 {market_label} {t['title']} · {brief.trading_date} · [{mode}]",
    ]
    if focus_note:
        lines.append(focus_note)

    # Freshness / staleness up top — never bury a data warning.
    warnings = list(brief.freshness.warnings)
    for w in warnings[:_MAX_WARNINGS]:
        lines.append(f"{t['stale']}: {w}")

    if brief.regime is not None:
        r = brief.regime
        lines.append(
            f"{t['regime']}: {r.risk_on_off} | {t['vix']} {_num(r.vix)}"
            f" | {t['breadth']} {r.breadth_pct_above_50dma:.0f}%"
        )

    top = brief.movers.top[:_TOP_MOVERS]
    bottom = brief.movers.bottom[:_TOP_MOVERS]
    if top:
        lines.append(
            f"{t['up']}: "
            + ", ".join(f"{m.symbol} {_pct(m.dist_sma20_pct)}" for m in top)
        )
    if bottom:
        lines.append(
            f"{t['down']}: "
            + ", ".join(f"{m.symbol} {_pct(m.dist_sma20_pct)}" for m in bottom)
        )

    if brief.themes:
        parts = [
            f"{th.theme} {_pct(th.avg_dist_sma50_pct)}"
            for th in brief.themes[:_TOP_THEMES]
        ]
        lines.append(f"{t['themes']}: " + " | ".join(parts))

    news_items = brief.news.items[:_TOP_NEWS]
    if news_items:
        lines.append(f"{t['news']}:")
        for item in news_items:
            src = f" ({item.source})" if item.source else ""
            lines.append(f"  · {item.headline}{src}")

    signals = brief.signals_today[:_TOP_SIGNALS]
    if signals:
        lines.append(f"{t['signals']}:")
        for s in signals:
            lines.append(
                f"  · {s.symbol} {s.direction} {s.confidence:.2f} — {s.thesis}"
            )

    if brief.uncertainty:
        lines.append(f"{t['notes']}: {brief.uncertainty[0]}")

    if brief.provenance:
        lines.append(f"{t['source']}: {brief.provenance[0].label}")

    return clamp("\n".join(lines), max_chars)
