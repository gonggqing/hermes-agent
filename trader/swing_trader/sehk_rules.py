"""SEHK price-spread (minimum tick) rules — HK order microstructure (Loop.md
§5.7 / docs/finance-hk-ibkr-rollout.md).

The Hong Kong Stock Exchange enforces a published, price-banded **minimum
spread** (tick size) for Main Board securities: a limit/stop price is only
valid when it lies on the tick grid of its price band. An order priced off the
grid is rejected by the exchange, so the price must be validated — and any
normalization shown to the human — BEFORE approval and again before submission
(the rollout doc: "Do not silently round quantity or price after human
approval; show any normalized order on the confirmation card").

Scope of THIS module: the deterministic, exchange-wide spread table only. It is
a fixed published rule, not per-stock data. **Board lots are per-instrument and
are NOT modelled here** — they are authoritative from IBKR contract details and
must never be guessed (Loop.md §3: never fabricate financial data).

Arithmetic uses ``Decimal`` throughout: binary floats cannot represent ticks
like 0.001/0.005/0.020 exactly, so a float implementation would mis-classify
on-grid prices as off-grid.
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple

__all__ = [
    "SEHK_SPREAD_TABLE",
    "SEHKPriceError",
    "sehk_tick_size",
    "is_on_tick",
    "round_to_tick",
]


class SEHKPriceError(ValueError):
    """A price is outside the SEHK-quotable range (0.01–9,995.00 HKD)."""


class _Band(NamedTuple):
    #: band applies to prices in (lower, upper], except the first band which is
    #: [0.01, 0.25]; ``lower`` is exclusive, ``upper`` inclusive.
    lower: Decimal
    upper: Decimal
    tick: Decimal


def _d(value: str) -> Decimal:
    return Decimal(value)


#: HKEX Main Board securities minimum spread table. Prices in HKD.
#: Source: HKEX trading rules spread table (long-standing published schedule).
SEHK_SPREAD_TABLE: tuple[_Band, ...] = (
    _Band(_d("0.01"), _d("0.25"), _d("0.001")),
    _Band(_d("0.25"), _d("0.50"), _d("0.005")),
    _Band(_d("0.50"), _d("10.00"), _d("0.01")),
    _Band(_d("10.00"), _d("20.00"), _d("0.02")),
    _Band(_d("20.00"), _d("100.00"), _d("0.05")),
    _Band(_d("100.00"), _d("200.00"), _d("0.1")),
    _Band(_d("200.00"), _d("500.00"), _d("0.2")),
    _Band(_d("500.00"), _d("1000.00"), _d("0.5")),
    _Band(_d("1000.00"), _d("2000.00"), _d("1")),
    _Band(_d("2000.00"), _d("5000.00"), _d("2")),
    _Band(_d("5000.00"), _d("9995.00"), _d("5")),
)

_MIN_PRICE = SEHK_SPREAD_TABLE[0].lower
_MAX_PRICE = SEHK_SPREAD_TABLE[-1].upper


def _as_decimal(price: float | str | Decimal) -> Decimal:
    # str(float) gives the shortest round-tripping decimal, avoiding binary
    # dust (e.g. Decimal(0.1) == 0.1000000000000000055…); callers pass prices
    # that are conceptually decimal quotes, so this is the intended reading.
    if isinstance(price, Decimal):
        return price
    return Decimal(str(price))


def sehk_tick_size(price: float | str | Decimal) -> Decimal:
    """Return the minimum tick (spread) for ``price`` per the SEHK band table.

    Raises ``SEHKPriceError`` if the price is outside the quotable range.
    """
    p = _as_decimal(price)
    if p < _MIN_PRICE or p > _MAX_PRICE:
        raise SEHKPriceError(
            f"price {p} outside SEHK quotable range "
            f"[{_MIN_PRICE}, {_MAX_PRICE}] HKD"
        )
    for band in SEHK_SPREAD_TABLE:
        # first band is inclusive at its lower bound; all others exclusive.
        if band.lower == _MIN_PRICE:
            if band.lower <= p <= band.upper:
                return band.tick
        elif band.lower < p <= band.upper:
            return band.tick
    raise SEHKPriceError(f"price {p} not covered by SEHK spread table")  # pragma: no cover


def is_on_tick(price: float | str | Decimal) -> bool:
    """True when ``price`` lies exactly on its band's tick grid.

    The grid is anchored at 0 (e.g. in the 20–100 band with 0.05 tick, 20.05
    and 20.10 are valid; 20.03 is not). Out-of-range prices are not on any
    grid and return False rather than raising, so validation callers can treat
    "off tick" and "out of range" uniformly if they wish.
    """
    p = _as_decimal(price)
    try:
        tick = sehk_tick_size(p)
    except SEHKPriceError:
        return False
    return (p % tick) == 0


def round_to_tick(
    price: float | str | Decimal, *, direction: str = "nearest"
) -> Decimal:
    """Snap ``price`` onto its band's tick grid.

    ``direction``: ``"nearest"`` (default), ``"down"`` (floor — conservative
    for a BUY limit, never pays more) or ``"up"`` (ceil — conservative for a
    protective SELL stop, never sits below intent). The returned price is
    guaranteed on-grid. Raises ``SEHKPriceError`` if out of range.

    A band boundary is handled by the tick AT the raw price; rounding never
    crosses into a neighbouring band's coarser/finer grid because every band
    upper bound is itself a multiple of both adjacent ticks.
    """
    p = _as_decimal(price)
    tick = sehk_tick_size(p)
    quotient = p / tick
    if direction == "down":
        stepped = quotient.to_integral_value(rounding="ROUND_FLOOR")
    elif direction == "up":
        stepped = quotient.to_integral_value(rounding="ROUND_CEILING")
    elif direction == "nearest":
        stepped = quotient.to_integral_value(rounding="ROUND_HALF_UP")
    else:
        raise ValueError(f"unknown direction {direction!r}")
    # quantize to the tick's own scale so the result reads as a clean quote
    # (20.00, not 2E+1) and carries the band's decimal places.
    return (stepped * tick).quantize(tick)
