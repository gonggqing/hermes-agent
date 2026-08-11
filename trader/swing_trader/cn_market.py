"""Mainland A-share paper-execution rules.

This module deliberately implements only exchange rules that can be applied
deterministically from the data carried by an :class:`Order`:

* SSE/SZSE session phases (including the midday break);
* 100-share/份 buy lots and integer sell quantities;
* 0.01 CNY stock ticks and 0.001 CNY listed-fund ticks;
* T+1 identification for the equity stocks/ETFs traded by this service.

Price-limit bands and security-specific exceptions need an authoritative
previous close plus instrument status.  They are therefore not guessed here;
the paper service must describe them as unverified until that data is wired.

Rules source: SSE Trading Rules (2026 revision), effective 2026-07-06,
sections 2.4.2, 3.1.4, 3.3.8 and 3.3.11.
https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/
"""

from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from enum import Enum
from typing import Mapping, Optional

from swing_trader.scheduler import CN_SCHEDULE, SHANGHAI, is_trading_day
from swing_trader.schemas import Side

__all__ = [
    "A_SHARE_BUY_LOT",
    "ASharePhase",
    "cn_phase",
    "cn_tick_size",
    "is_cn_order_acceptable",
    "is_cn_symbol",
    "is_listed_fund",
    "normalize_cn_buy_qty",
    "off_grid_cn_prices",
    "quantity_violation",
    "round_cn_price",
]

A_SHARE_BUY_LOT = 100
_EPS = Decimal("0.000000001")


class ASharePhase(str, Enum):
    CLOSED = "CLOSED"
    OPENING_AUCTION = "OPENING_AUCTION"
    MORNING = "MORNING"
    LUNCH = "LUNCH"
    AFTERNOON = "AFTERNOON"
    CLOSING_AUCTION = "CLOSING_AUCTION"


def is_cn_symbol(symbol: str) -> bool:
    return symbol.strip().upper().endswith((".SS", ".SZ"))


def _code(symbol: str) -> str:
    return symbol.strip().upper().split(".", 1)[0]


def is_listed_fund(symbol: str) -> bool:
    """Best-effort exchange-code classification for listed funds/ETFs.

    The production CN universe only admits exchange-listed instruments.  SSE
    listed funds use the 5xxxxx range; SZSE listed funds primarily use 15xxxx
    and 16xxxx.  Unknown ranges are treated as stocks (the safer, coarser
    0.01 tick) and can still be rejected by a later authoritative instrument
    lookup.
    """

    code = _code(symbol)
    upper = symbol.strip().upper()
    return (upper.endswith(".SS") and code.startswith("5")) or (
        upper.endswith(".SZ") and code.startswith(("15", "16"))
    )


def cn_tick_size(symbol: str) -> Decimal:
    if not is_cn_symbol(symbol):
        raise ValueError(f"not a mainland listed symbol: {symbol}")
    return Decimal("0.001") if is_listed_fund(symbol) else Decimal("0.01")


def round_cn_price(price: float, symbol: str, *, direction: str) -> float:
    """Snap a price before approval; never use this after human approval."""

    value = Decimal(str(price))
    if value <= 0:
        raise ValueError("price must be positive")
    tick = cn_tick_size(symbol)
    rounding = ROUND_FLOOR if direction == "down" else ROUND_CEILING
    units = (value / tick).to_integral_value(rounding=rounding)
    return float(units * tick)


def off_grid_cn_prices(
    symbol: str, prices: Mapping[str, Optional[float]]
) -> dict[str, str]:
    tick = cn_tick_size(symbol)
    bad: dict[str, str] = {}
    for label, raw in prices.items():
        if raw is None:
            continue
        value = Decimal(str(raw))
        units = value / tick
        if abs(units - units.to_integral_value()) > _EPS:
            bad[label] = f"{label} {raw:g} is off the mainland {tick} tick grid"
    return bad


def normalize_cn_buy_qty(qty: float) -> int:
    if qty <= 0:
        return 0
    return (int(qty) // A_SHARE_BUY_LOT) * A_SHARE_BUY_LOT


def quantity_violation(
    qty: float, side: Side, *, held_qty: Optional[float] = None
) -> Optional[str]:
    rounded = round(qty)
    if qty <= 0 or abs(qty - rounded) > 1e-9:
        return f"quantity {qty:g} must be a positive whole number of shares/units"
    if side is Side.BUY and rounded % A_SHARE_BUY_LOT:
        return f"BUY quantity {qty:g} is not a {A_SHARE_BUY_LOT}-share board lot"
    if side is Side.SELL and held_qty is not None:
        # A residual odd lot may be sold only in one shot.  Whole-lot sells are
        # always fine; a non-lot sell must exactly flatten the position.
        if rounded % A_SHARE_BUY_LOT and abs(qty - held_qty) > 1e-9:
            return "an odd-lot SELL must dispose of the entire remaining position"
    return None


def _require_aware(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")


def cn_phase(now_utc: datetime) -> ASharePhase:
    _require_aware(now_utc)
    local = now_utc.astimezone(SHANGHAI)
    if not is_trading_day(local.date(), CN_SCHEDULE):
        return ASharePhase.CLOSED
    value = local.time()
    if value < time(9, 15):
        return ASharePhase.CLOSED
    if value < time(9, 25):
        return ASharePhase.OPENING_AUCTION
    if value < time(9, 30):
        return ASharePhase.CLOSED
    if value < time(11, 30):
        return ASharePhase.MORNING
    if value < time(13, 0):
        return ASharePhase.LUNCH
    if value < time(14, 57):
        return ASharePhase.AFTERNOON
    if value < time(15, 0):
        return ASharePhase.CLOSING_AUCTION
    return ASharePhase.CLOSED


def is_cn_order_acceptable(now_utc: datetime) -> bool:
    return cn_phase(now_utc) in {
        ASharePhase.OPENING_AUCTION,
        ASharePhase.MORNING,
        ASharePhase.AFTERNOON,
        ASharePhase.CLOSING_AUCTION,
    }
