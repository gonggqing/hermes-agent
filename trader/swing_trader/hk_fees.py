"""Hong Kong securities trading fees — statutory + exchange charges
(docs/finance-hk-ibkr-rollout.md: "Model commission, stamp duty, trading fee,
transaction levy and platform charges as versioned fee rules. Paper estimates
and broker-reported fills are stored separately so reconciliation can explain
differences").

Scope: the FIXED, published statutory and HKEX charges that apply to every SEHK
securities trade, both buy and sell. **Broker commission is deliberately out of
scope** — it is broker-negotiated (not a published rate), so it stays with the
broker layer (``PaperBroker.commission_per_order`` / real IBKR fills) and must
never be fabricated here (Loop.md §3: never invent financial data).

The rates below are a VERSIONED snapshot with an as-of date and sources; they
change by regulation, so the schedule is overridable and every computed fill
records which schedule version produced it. Paper estimates from this module and
broker-reported fills are reconciled separately, never conflated.

Rate snapshot (``hk-2026.07``), consideration = price × quantity in HKD:
- Stamp duty: 0.10% each side, rounded UP to the next whole HK$ (Revenue
  (Stamp Duty) — 0.1% since 2023-11-17).
- SFC transaction levy: 0.0027% each side.
- AFRC transaction levy: 0.00015% each side.
- HKEX trading fee: 0.00565% each side.
- HKEX trading tariff: HK$0.50 per trade (nominal; broker may absorb).
- CCASS settlement fee: 0.0020% each side, min HK$2, max HK$100.

Pure arithmetic in ``Decimal`` so rounding (stamp-duty ceil, CCASS min/max) is
exact and reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

__all__ = [
    "HKFeeSchedule",
    "HKFeeBreakdown",
    "DEFAULT_HK_FEES",
    "compute_hk_fees",
]


def _d(v: str) -> Decimal:
    return Decimal(v)


@dataclass(frozen=True)
class HKFeeSchedule:
    """A versioned snapshot of HK statutory + HKEX securities charges.

    Rates are fractions of consideration (e.g. 0.001 = 0.10%). ``version`` and
    ``as_of`` identify the snapshot so a stored paper estimate is traceable to
    the rule set that produced it. Broker commission is NOT part of this
    schedule (see module docstring).
    """

    version: str
    as_of: str
    stamp_duty_rate: Decimal
    sfc_levy_rate: Decimal
    afrc_levy_rate: Decimal
    trading_fee_rate: Decimal
    ccass_rate: Decimal
    ccass_min: Decimal
    ccass_max: Decimal
    trading_tariff: Decimal


#: Published snapshot as of 2026-07 (HK$; sources in the module docstring).
DEFAULT_HK_FEES = HKFeeSchedule(
    version="hk-2026.07",
    as_of="2026-07-01",
    stamp_duty_rate=_d("0.001"),      # 0.10% each side, ceil to next HK$
    sfc_levy_rate=_d("0.000027"),     # 0.0027%
    afrc_levy_rate=_d("0.0000015"),   # 0.00015%
    trading_fee_rate=_d("0.0000565"),  # 0.00565%
    ccass_rate=_d("0.00002"),         # 0.0020%
    ccass_min=_d("2"),
    ccass_max=_d("100"),
    trading_tariff=_d("0.50"),
)


@dataclass(frozen=True)
class HKFeeBreakdown:
    """Per-component fee amounts (HK$) for one SEHK fill, plus the total.

    ``total`` is what the paper broker deducts on top of any broker commission;
    ``schedule_version`` tags the estimate for later reconciliation against the
    broker-reported charges.
    """

    stamp_duty: Decimal
    sfc_levy: Decimal
    afrc_levy: Decimal
    trading_fee: Decimal
    ccass_fee: Decimal
    trading_tariff: Decimal
    total: Decimal
    schedule_version: str


def _round2(value: Decimal) -> Decimal:
    """Round to the HK$0.01 the charge is levied/reported in."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def compute_hk_fees(
    consideration: float | Decimal,
    *,
    schedule: HKFeeSchedule = DEFAULT_HK_FEES,
) -> HKFeeBreakdown:
    """Statutory + HKEX fees for one SEHK fill of ``consideration`` HK$.

    ``consideration`` = fill price × quantity (must be > 0). Fees are symmetric
    across buy and sell for these components, so no side argument is needed.
    Stamp duty is rounded UP to the next whole HK$; CCASS is clamped to its
    HK$2–100 band; the remaining ad-valorem fees and the flat tariff are summed
    at HK$0.01 precision.
    """
    value = Decimal(str(consideration)) if not isinstance(
        consideration, Decimal
    ) else consideration
    if value <= 0:
        raise ValueError(f"consideration must be positive; got {value}")

    stamp = (value * schedule.stamp_duty_rate).quantize(
        Decimal("1"), rounding=ROUND_CEILING
    )
    sfc = _round2(value * schedule.sfc_levy_rate)
    afrc = _round2(value * schedule.afrc_levy_rate)
    trading = _round2(value * schedule.trading_fee_rate)
    ccass_raw = value * schedule.ccass_rate
    ccass = _round2(min(max(ccass_raw, schedule.ccass_min), schedule.ccass_max))
    tariff = _round2(schedule.trading_tariff)
    total = stamp + sfc + afrc + trading + ccass + tariff
    return HKFeeBreakdown(
        stamp_duty=stamp,
        sfc_levy=sfc,
        afrc_levy=afrc,
        trading_fee=trading,
        ccass_fee=ccass,
        trading_tariff=tariff,
        total=total,
        schedule_version=schedule.version,
    )
