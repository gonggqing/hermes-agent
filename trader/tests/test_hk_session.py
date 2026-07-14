"""Phase 0.95 CN/HK independence contracts."""

from datetime import date

from swing_trader.cn_watchlist import build_mainland_watchlist
from swing_trader.hk_watchlist import HK_INDEX_SYMBOLS, build_hk_watchlist
from swing_trader.scheduler import CN_SCHEDULE, HK_SCHEDULE, HONG_KONG, SHANGHAI, is_trading_day


def test_cn_and_hk_universes_are_disjoint():
    cn = set(build_mainland_watchlist().symbols)
    hk = set(build_hk_watchlist().symbols)
    assert cn and hk and cn.isdisjoint(hk)
    assert all(symbol.endswith((".SS", ".SZ")) for symbol in cn)
    assert all(symbol.endswith(".HK") for symbol in hk)


def test_overrides_cannot_cross_contaminate_markets():
    assert build_mainland_watchlist("0700.HK,688981.SS").symbols == ["688981.SS"]
    assert build_hk_watchlist("0700.HK,688981.SS").symbols == ["0700.HK"]


def test_cn_and_hk_have_independent_calendar_and_identity():
    assert CN_SCHEDULE.market_id == "CN" and CN_SCHEDULE.tz == SHANGHAI
    assert HK_SCHEDULE.market_id == "HK" and HK_SCHEDULE.tz == HONG_KONG
    # Good Friday: HK closed, mainland open.
    assert is_trading_day(date(2026, 4, 3), CN_SCHEDULE)
    assert not is_trading_day(date(2026, 4, 3), HK_SCHEDULE)
    assert HK_INDEX_SYMBOLS == ("^HSI", "^HSCE")
