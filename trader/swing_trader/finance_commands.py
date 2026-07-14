"""Finance-bot slash-command handlers (the DM "/" command menu).

The finance bot is a thin gatekeeper, not the full Hermes agent, so it has no
skills/sessions — but it can offer a small, useful command menu. These are the
handlers behind /持仓 /研究 /记账 /帮助 /start; each returns plain text (no live
price fetch, so they're fast) or None to fall through to the normal DM handling.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

__all__ = ["COMMAND_MENU", "make_command_handler"]

#: (command, description) for Telegram setMyCommands — shown in the "/" menu.
#: Telegram REQUIRES command names be lowercase [a-z0-9_] (no Chinese), so the
#: NAMES are latin and the DESCRIPTIONS carry the Chinese labels. The handler
#: also accepts the Chinese aliases (/持仓 …) typed by hand.
COMMAND_MENU: list[tuple[str, str]] = [
    ("holdings", "持仓 · 我的真实持仓 + 成本"),
    ("brief", "研究 · 今日市场简报（CN/KR）"),
    ("record", "记账 · 怎么记一笔买卖（示例）"),
    ("help", "帮助 · 这个 bot 能做什么"),
]

_HELP = (
    "我是你的财经助手 bot 📈 —— 负责【记账 · 确认 · 快速分析】。\n\n"
    "• 记一笔真实成交：直接说，例如「卖了 159813 200股 @1.893」→ 我生成草稿卡片，你点 ✅ 确认。\n"
    "• 改持仓显示名：「159518 改名 标普油气ETF嘉实」。\n"
    "• 查行情/分析：发我一个代码，如 NVDA、600519.SS、0700.HK。\n\n"
    "命令：/持仓 · /研究 · /记账 · /帮助"
)

_RECORD_HELP = (
    "记一笔真实成交，直接一句话发我（我解析 → 草稿卡片 → 你确认 ✅/❌）：\n"
    "• 买入：「513310 成交价5.762 200股」或「买入 NVDA 100股 单价204」\n"
    "• 卖出：「卖了 159813 200股 @1.893」\n"
    "• 指定账户：句子里带账户名，如「蚂蚁财富 买入 …」\n\n"
    "账户我会尽量自动判断（卖已持有的标的 → 对应账户）。卡片是最终确认，"
    "解析不确定的地方会标注出来，错了直接点 ❌。"
)


def _name(runtime: Any, symbol: str) -> str:
    from swing_trader.instrument_names import name_for

    ov = getattr(runtime, "name_overrides", None)
    if ov is not None:
        got = ov.get(symbol)
        if got:
            return got
    return name_for(symbol)


def _fmt_holdings(runtime: Any) -> str:
    journal = getattr(runtime, "portfolio", None)
    if journal is None:
        return "组合账本未就绪。"
    lines: list[str] = []
    for acct in journal.list_accounts():
        h = journal.holdings(acct.id)
        lines.append(f"【{acct.name}】")
        for pos in h.holdings:
            nm = _name(runtime, pos.symbol)
            tag = f"（{nm}）" if nm else ""
            cost = f" @ {pos.avg_cost:g}" if pos.avg_cost is not None else " @ 成本未知"
            lines.append(f"  {pos.symbol}{tag} {pos.qty:g}{cost}")
        for c in h.cash:
            amt = f"{c.amount:,.2f}" if c.known and c.amount is not None else "未知"
            lines.append(f"  现金 {c.currency} {amt}")
        if not h.holdings and not h.cash:
            lines.append("  （空）")
    lines.append("\n完整市值/盈亏见门户（此处不拉实时价，故从简）。")
    return "\n".join(lines) if lines else "暂无持仓。"


def _fmt_brief(runtime: Any) -> str:
    briefs = getattr(runtime, "latest_briefs", {}) or {}
    if not briefs:
        return "今日还没有研究简报（定时任务或手动刷新后再看）。"
    out: list[str] = []
    for mid, label in (("cn", "中港"), ("kr", "韩国半导体")):
        b = briefs.get(mid)
        if not b:
            continue
        movers = (b.get("movers") or {}).get("top", []) if isinstance(b.get("movers"), dict) else []
        out.append(f"【{label}】as-of {str(b.get('as_of', ''))[:10]}")
        for m in movers[:5]:
            nm = m.get("display_name") or ""
            d20 = m.get("dist_sma20_pct")
            d20s = f" 距SMA20 {d20:+.1f}%" if isinstance(d20, (int, float)) else ""
            out.append(f"  {m.get('symbol')} {nm}{d20s}")
    out.append("\n更完整的信号/主题/新闻见 Research 面板。")
    return "\n".join(out) if out else "今日暂无简报内容。"


def make_command_handler(runtime: Any) -> Callable[[str], Optional[str]]:
    """Build the slash-command handler: ``text -> reply`` for a known command,
    else None (caller falls through to rename/record/analysis)."""

    def handle(text: str) -> Optional[str]:
        t = (text or "").strip()
        if not t.startswith("/"):
            return None
        cmd = t[1:].split()[0].split("@")[0].lower() if len(t) > 1 else ""
        if cmd in ("start", "帮助", "help"):
            return _HELP
        if cmd in ("记账", "record"):
            return _RECORD_HELP
        if cmd in ("持仓", "holdings", "portfolio"):
            return _fmt_holdings(runtime)
        if cmd in ("研究", "brief", "research"):
            return _fmt_brief(runtime)
        return None  # unknown command → fall through

    return handle
