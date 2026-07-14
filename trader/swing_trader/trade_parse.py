"""Parse a natural-language trade-record message into a proposed draft.

The finance-bot is the SOLE portfolio-write surface (user decision 2026-07-14):
the user DMs/@mentions it with a trade in plain language, it parses the message
into a PROPOSED :class:`PortfolioDraft`, and the user confirms on the card. The
parse is best-effort and NEVER final — the confirm card is the human safety net,
so an imperfect parse is corrected/rejected there, never silently recorded
(Loop.md P0.9 boundary #4). Deterministic (no LLM) so it is testable and has no
latency; ``ambiguities`` carry anything the parse was unsure about onto the card.

Handles the user's actual phrasings, e.g.:
    "513310 成交价5.762，200股"        -> BUY 513310.SS 200 @ 5.762
    "159518 成本 1.1332，1200 股"       -> BUY 159518.SZ 1200 @ 1.1332
    "卖了 159813 200股 @1.893"          -> SELL 159813.SZ 200 @ 1.893
    "买入 NVDA 100股 单价 204"          -> BUY NVDA 100 @ 204
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["ParsedTrade", "looks_like_trade", "parse_trade"]

# Direction keywords. Bare "code price qty" with no keyword defaults to BUY
# (recording a new/added position is the common case) and flags an ambiguity.
_SELL = ("卖出", "卖掉", "卖了", "卖", "清仓", "减仓", "sell", "sold")
_BUY = ("买入", "买了", "买进", "买", "加仓", "建仓", "buy", "bought")

# Symbols: A-share/ETF 6-digit (optional .SS/.SZ), HK 4-5 digit .HK, or a US
# ticker (2-5 uppercase letters, optional .XX). Numeric codes win; US tickers
# are matched conservatively (uppercase, stopword-filtered) to avoid grabbing
# ordinary words — the required share-quantity gate keeps false positives rare.
_SYM_CN = re.compile(r"(?<!\d)(\d{6})(\.(?:SS|SZ))?(?!\d)")
_SYM_HK = re.compile(r"(?<!\d)(\d{4,5}\.HK)(?!\d)", re.IGNORECASE)
_SYM_US = re.compile(r"\b([A-Z]{2,5})(\.[A-Z]{2,4})?\b")
_US_STOPWORDS = {
    "SELL", "SOLD", "BUY", "SHARE", "SHARES", "AT", "THE", "AND", "FOR",
    "ETF", "USD", "CNY", "HKD", "PX", "AVG", "COST",
}

# Quantity: a number immediately before 股 / 份 / shares.
_QTY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:股|份|shares?|手)", re.IGNORECASE)
# Price: a number after @ / 成交价 / 成本(价) / 单价 / 价格 / 成交 / price / 价.
_PRICE = re.compile(
    r"(?:@|成交价|成本价?|单价|价格|成交|买价|卖价|price)\s*[:：]?\s*(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


@dataclass
class ParsedTrade:
    """A best-effort structured trade from a free-text message (not final)."""

    event_type: str                 # "buy" | "sell"
    symbol: str                     # normalized (uppercase, exchange-suffixed)
    qty: Optional[float]
    price: Optional[float]
    ambiguities: list = field(default_factory=list)


def _infer_cn_suffix(code: str) -> str:
    """A-share exchange from the leading digit (standard convention): 0/1/2/3 →
    Shenzhen (.SZ), 5/6/9 → Shanghai (.SS). Covers 159xxx→SZ, 51/58xxxx→SS."""
    return ".SZ" if code[0] in "0123" else ".SS"


def _find_symbol(text: str) -> Optional[str]:
    m = _SYM_HK.search(text)
    if m:
        return m.group(1).upper()
    m = _SYM_CN.search(text)
    if m:
        code, suffix = m.group(1), m.group(2)
        return f"{code}{(suffix or _infer_cn_suffix(code)).upper()}"
    for m in _SYM_US.finditer(text):
        tok = m.group(1).upper()
        if tok not in _US_STOPWORDS:
            return f"{tok}{(m.group(2) or '').upper()}"
    return None


def looks_like_trade(text: str) -> bool:
    """Cheap intent gate: a symbol AND a share quantity. An analysis request
    ('分析 NVDA', '看看 159813') has no 股/份, so it won't match."""
    return bool(text) and _QTY.search(text) is not None and _find_symbol(text) is not None


def parse_trade(text: str) -> Optional[ParsedTrade]:
    """Parse a record message, or None if it isn't a trade record.

    Requires a symbol AND a share quantity (the gate); price is optional. The
    direction is taken from an explicit keyword, defaulting to BUY (flagged)."""
    if not text:
        return None
    symbol = _find_symbol(text)
    qm = _QTY.search(text)
    if symbol is None or qm is None:
        return None

    qty = float(qm.group(1))
    pm = _PRICE.search(text)
    price = float(pm.group(1)) if pm else None

    low = text.lower()
    is_sell = any(w in low for w in _SELL)
    is_buy = any(w in low for w in _BUY)
    event_type = "sell" if is_sell else "buy"

    ambiguities: list[str] = []
    if not is_sell and not is_buy:
        ambiguities.append("方向默认按「买入」（消息未写明买/卖，请在卡片核对）")
    if price is None:
        ambiguities.append("未识别成交价（价格留空，可在门户补）")
    return ParsedTrade(
        event_type=event_type, symbol=symbol, qty=qty, price=price,
        ambiguities=ambiguities,
    )
