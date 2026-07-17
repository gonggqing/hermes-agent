"""HK statutory + HKEX fee model (Loop.md §5.7 / rollout doc, offline)."""

from decimal import Decimal

import pytest

from swing_trader.hk_fees import DEFAULT_HK_FEES, compute_hk_fees


class TestComputeHKFees:
    def test_reference_100k_consideration(self):
        f = compute_hk_fees(100_000)
        assert f.stamp_duty == Decimal("100")      # 0.10%, whole HK$
        assert f.sfc_levy == Decimal("2.70")       # 0.0027%
        assert f.afrc_levy == Decimal("0.15")      # 0.00015%
        assert f.trading_fee == Decimal("5.65")    # 0.00565%
        assert f.ccass_fee == Decimal("2.00")      # 0.0020%, above the HK$2 min
        assert f.trading_tariff == Decimal("0.50")
        assert f.total == Decimal("111.00")
        assert f.schedule_version == "hk-2026.07"

    def test_stamp_duty_rounds_up_to_whole_dollar(self):
        # 100_050 * 0.001 = 100.05 -> ceil to 101
        assert compute_hk_fees(100_050).stamp_duty == Decimal("101")

    def test_ccass_min_floor(self):
        # 5_000 * 0.00002 = 0.10 -> floored at the HK$2 minimum
        assert compute_hk_fees(5_000).ccass_fee == Decimal("2.00")

    def test_ccass_max_cap(self):
        # 10_000_000 * 0.00002 = 200 -> capped at HK$100
        assert compute_hk_fees(10_000_000).ccass_fee == Decimal("100.00")

    def test_total_is_sum_of_components(self):
        f = compute_hk_fees(37_450)
        assert f.total == (
            f.stamp_duty + f.sfc_levy + f.afrc_levy
            + f.trading_fee + f.ccass_fee + f.trading_tariff
        )

    def test_nonpositive_consideration_raises(self):
        with pytest.raises(ValueError, match="positive"):
            compute_hk_fees(0)

    def test_accepts_decimal_input(self):
        assert compute_hk_fees(Decimal("100000")).total == Decimal("111.00")

    def test_schedule_is_overridable(self):
        cheap = DEFAULT_HK_FEES.__class__(
            **{**DEFAULT_HK_FEES.__dict__, "version": "test-0",
               "trading_tariff": Decimal("0")}
        )
        assert compute_hk_fees(100_000, schedule=cheap).trading_tariff == Decimal("0")
        assert compute_hk_fees(100_000, schedule=cheap).schedule_version == "test-0"
