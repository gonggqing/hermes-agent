"""Turn a DM trade message into a proposed draft (finance-bot recording).

Ties :mod:`trade_parse` (text → ParsedTrade) to the account journal (which
account? which market/currency?) and the PortfolioDraftService, producing the
``(draft, account_label, ack)`` tuple the finance-bot pushes as a confirm card.

The user DMs the finance bot in plain language; the parse + account guess are
best-effort and land on a confirm card — the human safety net (Loop.md P0.9
boundary #4). Anything the guess is unsure about rides on the card as an
ambiguity, so it's corrected/rejected there, never silently recorded.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from swing_trader.trade_parse import ParsedTrade, parse_trade

__all__ = ["make_trade_recorder", "resolve_account"]

_EVENT_ZH = {"buy": "买入", "sell": "卖出"}


def _market_currency(symbol: str) -> tuple[str, str]:
    """Instrument market + trade currency from the symbol suffix."""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HK", "HKD"
    if s.endswith(".SS") or s.endswith(".SZ"):
        return "CN", "CNY"
    return "US", "USD"


def _name_keywords(name: str) -> list[str]:
    """Distinctive tokens to match an account by nickname in free text
    ("平安证券" → 平安证券 / 平安; keeps a full-name match plus a short prefix)."""
    name = (name or "").strip()
    kws = [name] if name else []
    if len(name) >= 2:
        kws.append(name[:2])  # 平安, 蚂蚁, IB…
    return kws


def _matching_holdings(journal: Any, account_id: str, parsed: ParsedTrade) -> list:
    """Resolve the typed code against one account's actual holdings.

    Exact input wins (``017470`` stays the bare mutual-fund code), then the
    inferred exchange form (``159813`` -> ``159813.SZ``).  Base-code fallback
    is allowed only when the user did not type an explicit suffix.
    """
    try:
        holdings = journal.holdings(account_id).holdings
    except Exception:  # a holdings read must not break recording
        return []
    raw = parsed.raw_symbol.upper()
    normalized = parsed.symbol.upper()
    exact = [h for h in holdings if h.symbol == raw]
    if exact:
        return exact
    inferred = [h for h in holdings if h.symbol == normalized]
    if inferred:
        return inferred
    if "." in raw:
        return []
    return [h for h in holdings if h.symbol.split(".", 1)[0] == raw]


def resolve_account(journal: Any, parsed: ParsedTrade, text: str):
    """Resolve account AND canonical held symbol together.

    Returns ``(account_or_None, label, ambiguities, holding_or_None)``.  SELL
    never falls back to an unheld explicit account; BUY may still target a new
    instrument in an explicitly named (or sole) account.
    """
    accounts = journal.list_accounts()
    if not accounts:
        return None, "", ["尚无账户，请先在门户创建账户"], None

    lowered = text.lower()
    for a in accounts:  # 1) explicit account nickname in the message
        if any(kw and kw.lower() in lowered for kw in _name_keywords(a.name)):
            matches = _matching_holdings(journal, a.id, parsed)
            if len(matches) == 1:
                return a, a.name, [], matches[0]
            if len(matches) > 1:
                return (a, a.name,
                        [f"{parsed.raw_symbol} 匹配到多个持仓代码，请写完整代码"],
                        None)
            if parsed.event_type == "sell":
                return (a, a.name,
                        [f"{a.name} 当前未持有 {parsed.raw_symbol}，不能卖出"],
                        None)
            return a, a.name, [], None

    held = [
        (a, h)
        for a in accounts
        for h in _matching_holdings(journal, a.id, parsed)
    ]
    if len(held) == 1:  # 2) the typed/inferred code resolves uniquely
        account, holding = held[0]
        return account, account.name, [], holding
    if len(held) > 1:
        return (None, "",
                [f"{parsed.raw_symbol} 匹配到多个持仓，请指明账户和完整代码"],
                None)

    if parsed.event_type == "sell":
        return (None, "", [f"当前未持有 {parsed.raw_symbol}，不能卖出"], None)

    if len(accounts) == 1:  # 3) only one account exists
        return accounts[0], accounts[0].name, [], None

    return None, "", ["请指明账户（消息未写明，且该标的当前无持仓）"], None


def _ack(parsed: ParsedTrade, label: str) -> str:
    action = _EVENT_ZH.get(parsed.event_type, parsed.event_type)
    px = f" @ {parsed.price:g}" if parsed.price is not None else ""
    acct = f" · {label}" if label else ""
    return (f"📝 已识别记账：{action} {parsed.symbol} {parsed.qty:g}股{px}{acct}\n"
            f"请核对下方卡片并确认 ✅ / 拒绝 ❌")


def make_trade_recorder(runtime: Any) -> Callable[[str], Optional[tuple]]:
    """Build the finance-bot's DM trade recorder: ``text -> (draft, label, ack)``
    or None when the text isn't a trade record (falls through to analysis)."""

    def record(text: str) -> Optional[tuple]:
        parsed = parse_trade(text)
        if parsed is None:
            return None
        journal = runtime.portfolio
        drafts = runtime.portfolio_drafts
        if journal is None or drafts is None:
            return None
        account, label, acc_amb, holding = resolve_account(journal, parsed, text)
        if holding is not None:
            parsed.symbol = holding.symbol
            market = holding.market.value if holding.market is not None else "CN"
            currency = holding.currency
            if parsed.event_type == "sell" and parsed.qty > holding.qty:
                acc_amb.append(
                    f"卖出数量 {parsed.qty:g} 超过当前持仓 {holding.qty:g}"
                )
        else:
            market, currency = _market_currency(parsed.symbol)
        draft = drafts.create_draft(
            account_id=account.id if account is not None else None,
            event_type=parsed.event_type,
            symbol=parsed.symbol,
            market=market,
            currency=currency,
            qty=parsed.qty,
            price=parsed.price,
            occurred_at=runtime.clock(),
            original_text=text[:2000],
            ambiguities=[*parsed.ambiguities, *acc_amb],
            created_by="hermes",
            created_surface="telegram",
        )
        return draft, label, _ack(parsed, label)

    return record
