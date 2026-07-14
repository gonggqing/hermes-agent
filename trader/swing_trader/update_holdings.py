"""Parse a DM 'update-holdings' message into a structured field edit.

The finance bot lets the user fix a holding from Telegram. Fields split by risk:
  - name  → cosmetic display override (immediate)
  - mark  → manual current price for valuation (immediate; reuses the marks store)
  - cost / qty / account → FINANCIAL corrections → must go through a correction
    draft → confirm card (append-only re-statement; boundary #4)

This module only PARSES (text → (field, symbol, value)); the handler decides
immediate-vs-card. Normal buy-more averaging is NOT here — that's a trade record
(trade_parse), where weighted-average cost updates automatically.
"""

from __future__ import annotations

import re
from typing import Optional

from swing_trader.name_override import parse_rename
from swing_trader.trade_parse import find_symbol

__all__ = ["market_currency", "parse_update"]

_MARK_KW = re.compile(r"(现价|标记价|标记|市价)")
_COST_KW = re.compile(r"成本")
_QTY_KW = re.compile(r"(数量|股数|持仓数)")  # NOT "股" alone — that's a trade
_ACCT_KW = re.compile(r"(移到|移入|换到|转到|换账户|账户改成|账户换成|挪到)")
_NUM = re.compile(r"(\d+(?:\.\d+)?)")
_SYM_TOKEN = re.compile(r"\d{6}(?:\.(?:SS|SZ))?|\d{4,5}\.HK|[A-Za-z]{2,5}(?:\.[A-Za-z]{2,4})?")


def market_currency(symbol: str) -> tuple[str, str]:
    """Instrument market + trade currency from the symbol suffix."""
    s = symbol.upper()
    if s.endswith(".HK"):
        return "HK", "HKD"
    if s.endswith(".SS") or s.endswith(".SZ"):
        return "CN", "CNY"
    return "US", "USD"


def _num_after(text: str, end: int) -> Optional[str]:
    m = _NUM.search(text[end:])
    return m.group(1) if m else None


def parse_update(text: str) -> Optional[tuple[str, str, str]]:
    """(field, normalized symbol, value) or None if not an update-holdings edit.

    field ∈ {name, mark, cost, qty, account}. Only one field per message; order
    of precedence: name → account → mark → cost → qty."""
    if not text:
        return None

    r = parse_rename(text)  # name (改名/重命名/…)
    if r is not None:
        return ("name", r[0], r[1])

    sym = find_symbol(text)
    if sym is None:
        return None

    am = _ACCT_KW.search(text)
    if am is not None:
        # account name = text after the keyword, minus any symbol token
        acct = _SYM_TOKEN.sub("", text[am.end():]).strip(" \t:：到的")
        if acct:
            return ("account", sym, acct)

    mm = _MARK_KW.search(text)
    if mm is not None:
        v = _num_after(text, mm.end()) or _num_after(text, 0)
        if v:
            return ("mark", sym, v)

    cm = _COST_KW.search(text)
    if cm is not None:
        v = _num_after(text, cm.end())
        if v:
            return ("cost", sym, v)

    qm = _QTY_KW.search(text)
    if qm is not None:
        v = _num_after(text, qm.end())
        if v:
            return ("qty", sym, v)

    return None
