"""Tests for the finance-bot's natural-language trade parser (trade_parse.py).

Tuned to the user's real phrasings; the confirm card is the safety net, so the
parser aims for the common cases and flags anything it's unsure about.
"""

from __future__ import annotations

from swing_trader.trade_parse import looks_like_trade, parse_trade


def test_buy_with_chengjiaojia():
    p = parse_trade("513310 成交价5.762，200股")
    assert p is not None
    assert p.event_type == "buy" and p.symbol == "513310.SS"
    assert p.qty == 200 and p.price == 5.762


def test_buy_with_chengben():
    p = parse_trade("159518 成本 1.1332，1200 股")
    assert p.event_type == "buy" and p.symbol == "159518.SZ"  # 1xxxxx -> Shenzhen
    assert p.raw_symbol == "159518"
    assert p.qty == 1200 and p.price == 1.1332


def test_bare_fund_code_is_retained_for_portfolio_resolution():
    p = parse_trade("蚂蚁财富，017470，以成交价3.500卖了400份")
    assert p.symbol == "017470.SZ"  # fallback for a genuinely new instrument
    assert p.raw_symbol == "017470"  # existing portfolio may canonically be bare


def test_sell_with_at_price():
    p = parse_trade("卖了 159813 200股 @1.893")
    assert p.event_type == "sell" and p.symbol == "159813.SZ"
    assert p.qty == 200 and p.price == 1.893
    assert not p.ambiguities  # explicit sell + explicit price → nothing unsure


def test_sell_keyword_after_qty():
    p = parse_trade("159813 @1.893 卖 200 股")
    assert p.event_type == "sell" and p.symbol == "159813.SZ" and p.qty == 200


def test_us_ticker_with_unit_price():
    p = parse_trade("买入 NVDA 100股 单价 204")
    assert p.event_type == "buy" and p.symbol == "NVDA"
    assert p.qty == 100 and p.price == 204


def test_shanghai_vs_shenzhen_suffix_inference():
    assert parse_trade("买 600519 100股 @1500").symbol == "600519.SS"  # 6 -> SH
    assert parse_trade("买 000001 100股 @12").symbol == "000001.SZ"    # 0 -> SZ
    assert parse_trade("买 588200 300股 @3.18").symbol == "588200.SS"  # 5 -> SH


def test_explicit_exchange_suffix_kept():
    assert parse_trade("卖 159813.SZ 100股 @1.9").symbol == "159813.SZ"


def test_bare_direction_defaults_to_buy_and_flags_it():
    p = parse_trade("159518 1200股 成交价1.1332")
    assert p.event_type == "buy"
    assert any("方向默认" in a for a in p.ambiguities)


def test_missing_price_is_flagged():
    p = parse_trade("卖了 159813 200股")
    assert p.price is None and any("成交价" in a for a in p.ambiguities)


def test_analysis_messages_are_not_trades():
    for t in ["分析一下 NVDA", "看看 159813 走势", "600519 现在多少钱", "帮我查 GOLD"]:
        assert parse_trade(t) is None, t
        assert looks_like_trade(t) is False, t


def test_looks_like_trade_gate():
    assert looks_like_trade("卖了 159813 200股 @1.893") is True
    assert looks_like_trade("159813 怎么样") is False  # no share quantity
