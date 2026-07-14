"""Tests for the DM update-holdings parser (update_holdings.py)."""

from __future__ import annotations

from swing_trader.update_holdings import market_currency, parse_update


def test_name():
    assert parse_update("159518 改名 标普油气ETF嘉实") == (
        "name", "159518.SZ", "标普油气ETF嘉实")


def test_mark():
    assert parse_update("159518 现价 1.15") == ("mark", "159518.SZ", "1.15")
    assert parse_update("518880 标记价 8.5") == ("mark", "518880.SS", "8.5")


def test_cost():
    assert parse_update("159518 成本改成 1.20") == ("cost", "159518.SZ", "1.20")
    assert parse_update("159518 成本 1.20") == ("cost", "159518.SZ", "1.20")


def test_qty():
    assert parse_update("159518 数量改成 1000") == ("qty", "159518.SZ", "1000")
    # a plain trade ("…200股") must NOT be read as a qty edit (no 数量 keyword)
    assert parse_update("卖了 159813 200股 @1.893") is None


def test_account():
    f, sym, val = parse_update("159518 移到 蚂蚁财富")
    assert (f, sym) == ("account", "159518.SZ") and "蚂蚁财富" in val


def test_not_an_update():
    assert parse_update("159518 现在多少钱") is None
    assert parse_update("分析 NVDA") is None


def test_market_currency():
    assert market_currency("159518.SZ") == ("CN", "CNY")
    assert market_currency("0700.HK") == ("HK", "HKD")
    assert market_currency("NVDA") == ("US", "USD")
