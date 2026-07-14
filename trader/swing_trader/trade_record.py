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


def _holds(journal: Any, account_id: str, symbol: str) -> bool:
    try:
        return any(
            h.symbol == symbol.upper()
            for h in journal.holdings(account_id).holdings
        )
    except Exception:  # a holdings read must not break recording
        return False


def resolve_account(journal: Any, parsed: ParsedTrade, text: str):
    """Pick the account for a parsed trade. Returns ``(account_or_None, label,
    ambiguities)``. Order: explicit name hint → held in exactly one account →
    the only account → give up (flag it, draft stays INCOMPLETE)."""
    accounts = journal.list_accounts()
    if not accounts:
        return None, "", ["尚无账户，请先在门户创建账户"]

    lowered = text.lower()
    for a in accounts:  # 1) explicit account nickname in the message
        if any(kw and kw.lower() in lowered for kw in _name_keywords(a.name)):
            return a, a.name, []

    holders = [a for a in accounts if _holds(journal, a.id, parsed.symbol)]
    if len(holders) == 1:  # 2) the symbol is held in exactly one account
        return holders[0], holders[0].name, []
    if len(holders) > 1:
        return None, "", [f"{parsed.symbol} 在多个账户持有，请指明账户"]

    if len(accounts) == 1:  # 3) only one account exists
        return accounts[0], accounts[0].name, []

    return None, "", ["请指明账户（消息未写明，且该标的当前无持仓）"]


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
        account, label, acc_amb = resolve_account(journal, parsed, text)
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
