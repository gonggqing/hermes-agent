"""HK board-lot rules — SEHK order quantity microstructure
(docs/finance-hk-ibkr-rollout.md: "Enforce board lots ... before approval and
again before submission. Do not silently round quantity ... after human
approval; show any normalized order on the confirmation card").

SEHK trades in **board lots**: each security has a lot size (e.g. 100, 500,
1,000 shares) and an order quantity must be a whole multiple of it. Unlike the
price-spread table (a single exchange-wide rule — see ``sehk_rules``), the lot
size is **per instrument** and is authoritative from the IBKR contract details;
it is real financial data and must never be guessed (Loop.md §3).

So this module holds only the lot **arithmetic** plus a lookup table that is
*injected* with lot sizes the caller obtained authoritatively (IBKR contract
details, or an operator-supplied config). An unknown symbol returns ``None`` —
the caller then applies its own policy (fail closed, or an explicit, clearly
labelled whole-share paper fallback), which this module never decides for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

__all__ = [
    "BoardLotTable",
    "is_valid_lot",
    "round_down_to_lot",
    "lot_violation",
]


def is_valid_lot(qty: float, lot: int) -> bool:
    """True when ``qty`` is a positive whole multiple of the ``lot`` size."""
    if lot <= 0:
        raise ValueError(f"lot size must be positive; got {lot}")
    if qty <= 0:
        return False
    # qty may arrive as a float; treat near-integers as integers, then test
    # exact divisibility so 300 % 100 == 0 while 350 % 100 != 0.
    rounded = round(qty)
    if abs(qty - rounded) > 1e-9:
        return False
    return rounded % lot == 0


def round_down_to_lot(qty: float, lot: int) -> int:
    """Largest whole-lot quantity not exceeding ``qty`` (0 if below one lot).

    Rounding DOWN is the conservative normalization for a buy: it never buys
    more than intended. Used BEFORE approval; after approval an off-lot order is
    refused, not silently rounded.
    """
    if lot <= 0:
        raise ValueError(f"lot size must be positive; got {lot}")
    if qty <= 0:
        return 0
    return (int(qty) // lot) * lot


def lot_violation(qty: float, lot: int) -> Optional[str]:
    """A human-readable reason if ``qty`` is not a valid board-lot multiple,
    else ``None``. Used by the fail-closed pre-submission guard."""
    if not is_valid_lot(qty, lot):
        return f"quantity {qty:g} is not a whole multiple of the {lot}-share board lot"
    return None


@dataclass
class BoardLotTable:
    """Symbol → board-lot lookup, populated only from authoritative sources.

    Nothing is seeded: an entry exists only because the caller ``set`` it from
    IBKR contract details or an operator config. ``get`` returns ``None`` for an
    unknown symbol so the caller can fail closed rather than assume a lot size.
    """

    _lots: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_config(cls, mapping: dict[str, int]) -> "BoardLotTable":
        """Build from an operator-supplied ``{symbol: lot}`` map (authoritative
        input; the operator owns its correctness)."""
        table = cls()
        for symbol, lot in mapping.items():
            table.set(symbol, lot)
        return table

    def set(self, symbol: str, lot: int) -> None:
        if lot <= 0:
            raise ValueError(f"lot size for {symbol} must be positive; got {lot}")
        self._lots[symbol.upper()] = lot

    def get(self, symbol: str) -> Optional[int]:
        return self._lots.get(symbol.upper())

    def known(self, symbol: str) -> bool:
        return symbol.upper() in self._lots
