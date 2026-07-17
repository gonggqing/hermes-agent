"""SEHK price-spread (tick) rules — deterministic, offline (Loop.md §5.7).

The spread table is a fixed published HKEX schedule; these tests pin the band
boundaries, the anchored grid, and the direction-aware rounding used to
normalize a limit/stop for the confirmation card.
"""

from decimal import Decimal

import pytest

from swing_trader.sehk_rules import (
    SEHKPriceError,
    is_on_tick,
    round_to_tick,
    sehk_tick_size,
)


class TestTickSize:
    @pytest.mark.parametrize(
        "price,tick",
        [
            ("0.01", "0.001"),
            ("0.25", "0.001"),   # upper bound of band 1 is inclusive
            ("0.2501", "0.005"),
            ("0.50", "0.005"),
            ("0.5001", "0.010"),
            ("10.00", "0.010"),
            ("10.001", "0.020"),
            ("20.00", "0.020"),  # boundary -> coarser band starts ABOVE 20
            ("20.01", "0.050"),
            ("100.00", "0.050"),
            ("100.01", "0.100"),
            ("200.00", "0.100"),
            ("500.00", "0.200"),
            ("1000.00", "0.500"),
            ("2000.00", "1.000"),
            ("5000.00", "2.000"),
            ("9995.00", "5.000"),
        ],
    )
    def test_band_boundaries(self, price, tick):
        assert sehk_tick_size(price) == Decimal(tick)

    def test_accepts_float_input(self):
        assert sehk_tick_size(15.0) == Decimal("0.020")

    @pytest.mark.parametrize("price", ["0.009", "0", "9995.01", "10000", "-1"])
    def test_out_of_range_raises(self, price):
        with pytest.raises(SEHKPriceError):
            sehk_tick_size(price)


class TestIsOnTick:
    @pytest.mark.parametrize("price", ["20.05", "20.10", "0.011", "600.5", "9995.00"])
    def test_on_grid(self, price):
        assert is_on_tick(price) is True

    @pytest.mark.parametrize("price", ["20.03", "20.055", "0.0125", "1000.25"])
    def test_off_grid(self, price):
        assert is_on_tick(price) is False

    def test_out_of_range_is_not_on_tick(self):
        assert is_on_tick("10000") is False  # returns False, does not raise


class TestRoundToTick:
    def test_nearest(self):
        assert round_to_tick("20.03") == Decimal("20.05")
        assert round_to_tick("20.02") == Decimal("20.00")

    def test_down_is_conservative_for_a_buy_limit(self):
        # a BUY limit rounded DOWN never pays more than intended
        assert round_to_tick("20.049", direction="down") == Decimal("20.00")

    def test_up_is_conservative_for_a_protective_stop(self):
        # a protective SELL stop rounded UP never sits below intent
        # 15.001 is in the 10-20 band (tick 0.02) -> ceil to 15.02
        assert round_to_tick("15.001", direction="up") == Decimal("15.02")

    def test_result_is_always_on_grid(self):
        for raw in ("0.0123", "0.2537", "137.77", "2734.9"):
            assert is_on_tick(round_to_tick(raw)) is True

    def test_already_on_grid_is_unchanged(self):
        assert round_to_tick("20.05") == Decimal("20.05")

    def test_clean_boundary_representation(self):
        # 20.00 must read as a clean quote, not 2E+1
        assert str(round_to_tick("20.004", direction="down")) == "20.00"

    def test_unknown_direction_raises(self):
        with pytest.raises(ValueError, match="direction"):
            round_to_tick("20.05", direction="sideways")

    def test_out_of_range_raises(self):
        with pytest.raises(SEHKPriceError):
            round_to_tick("0.001")
