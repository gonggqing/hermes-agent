"""HKEX intraday hours — lunch break + auctions (Loop.md §5.7, offline)."""

from datetime import datetime, timezone

import pytest

from swing_trader.hk_market_hours import (
    HKPhase,
    hk_phase,
    is_hk_continuous_trading,
    is_hk_lunch_break,
    is_hk_order_acceptable,
)
from swing_trader.scheduler import HONG_KONG

# 2026-07-15 is a Wednesday and not an HK holiday.
WED = (2026, 7, 15)


def hkt(hh: int, mm: int, day=WED) -> datetime:
    return datetime(*day, hh, mm, tzinfo=HONG_KONG).astimezone(timezone.utc)


class TestPhase:
    @pytest.mark.parametrize(
        "hh,mm,phase",
        [
            (8, 59, HKPhase.CLOSED),
            (9, 0, HKPhase.PRE_AUCTION),
            (9, 29, HKPhase.PRE_AUCTION),
            (9, 30, HKPhase.MORNING),
            (11, 59, HKPhase.MORNING),
            (12, 0, HKPhase.LUNCH),
            (12, 30, HKPhase.LUNCH),
            (13, 0, HKPhase.AFTERNOON),
            (15, 59, HKPhase.AFTERNOON),
            (16, 0, HKPhase.CLOSING_AUCTION),
            (16, 9, HKPhase.CLOSING_AUCTION),
            (16, 10, HKPhase.CLOSED),
            (23, 0, HKPhase.CLOSED),
        ],
    )
    def test_boundaries(self, hh, mm, phase):
        assert hk_phase(hkt(hh, mm)) is phase

    def test_weekend_is_closed(self):
        assert hk_phase(hkt(10, 0, day=(2026, 7, 18))) is HKPhase.CLOSED  # Sat

    def test_hk_holiday_is_closed(self):
        # 2026-07-01 is the HK SAR Establishment Day holiday.
        assert hk_phase(hkt(10, 0, day=(2026, 7, 1))) is HKPhase.CLOSED

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            hk_phase(datetime(2026, 7, 15, 10, 0))


class TestHelpers:
    def test_continuous_trading(self):
        assert is_hk_continuous_trading(hkt(10, 0)) is True   # morning
        assert is_hk_continuous_trading(hkt(14, 0)) is True   # afternoon
        assert is_hk_continuous_trading(hkt(12, 30)) is False  # lunch
        assert is_hk_continuous_trading(hkt(9, 15)) is False   # pre-auction
        assert is_hk_continuous_trading(hkt(16, 5)) is False   # closing auction

    def test_lunch_break(self):
        assert is_hk_lunch_break(hkt(12, 30)) is True
        assert is_hk_lunch_break(hkt(11, 0)) is False

    def test_order_acceptable_includes_auctions_not_lunch(self):
        assert is_hk_order_acceptable(hkt(9, 10)) is True    # pre-auction
        assert is_hk_order_acceptable(hkt(10, 0)) is True    # morning
        assert is_hk_order_acceptable(hkt(14, 0)) is True    # afternoon
        assert is_hk_order_acceptable(hkt(16, 5)) is True    # closing auction
        assert is_hk_order_acceptable(hkt(12, 30)) is False  # lunch
        assert is_hk_order_acceptable(hkt(8, 0)) is False    # closed
