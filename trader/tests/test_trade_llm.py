"""High-speed LLM trade extraction is structured and fail-closed."""

import json

import pytest

from swing_trader.llm import LLMSettings
from swing_trader.trade_llm import LLMTradeExtractor


SETTINGS = LLMSettings(base_url="https://x", model="fast-test", api_key="secret")


def _extractor(payload):
    def complete(_settings, _system, prompt):
        assert json.loads(prompt)["message"]
        return json.dumps(payload, ensure_ascii=False)

    return LLMTradeExtractor(SETTINGS, complete=complete)


@pytest.mark.parametrize("text", [
    "015894 以2.1025成交价卖出 204.56份",
    "015894 以2.1025净值卖出 204.56份",
    "015894 以2.1025的价格卖出 204.56份",
])
def test_chinese_price_order_from_structured_reply(text):
    ext = _extractor({
        "is_trade": True,
        "action": "sell",
        "instrument_query": "015894",
        "suggested_symbol": "015894",
        "quantity": 204.56,
        "price": 2.1025,
        "price_kind": "nav",
        "account_hint": "蚂蚁财富",
    }).extract(text)

    assert ext is not None
    assert ext.action == "sell" and ext.instrument_query == "015894"
    assert ext.quantity == pytest.approx(204.56)
    assert ext.price == pytest.approx(2.1025)


def test_name_only_instrument_keeps_name_and_search_hint():
    ext = _extractor({
        "is_trade": True,
        "action": "buy",
        "instrument_query": "英伟达",
        "suggested_symbol": "NVDA",
        "quantity": 5,
        "price": 180.25,
        "price_kind": "trade",
        "account_hint": "IBKR",
    }).extract("IBKR 以180.25美元买入英伟达5股")

    assert ext.instrument_query == "英伟达"
    assert ext.suggested_symbol == "NVDA" and ext.account_hint == "IBKR"


@pytest.mark.parametrize("payload", [
    {"is_trade": False},
    {"is_trade": True, "action": "hold", "instrument_query": "NVDA",
     "quantity": 1},
    {"is_trade": True, "action": "buy", "instrument_query": "NVDA",
     "quantity": -1},
])
def test_non_trade_or_invalid_output_creates_no_extraction(payload):
    assert _extractor(payload).extract("text") is None


def test_network_failure_returns_none_and_never_leaks_key():
    calls = []

    def fail(*_args):
        calls.append(1)
        raise RuntimeError("timeout")

    assert LLMTradeExtractor(
        SETTINGS, complete=fail, attempts=2, sleep=lambda _s: None
    ).extract("买入 NVDA 1股") is None
    assert len(calls) == 2


def test_transient_failure_retries_then_succeeds():
    calls = []

    def flaky(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("first call")
        return (
            '{"is_trade":true,"action":"buy","instrument_query":"NVDA",'
            '"suggested_symbol":"NVDA","quantity":1,"price":180,'
            '"price_kind":"trade","account_hint":"IBKR"}'
        )

    ext = LLMTradeExtractor(
        SETTINGS, complete=flaky, attempts=2, sleep=lambda _s: None
    ).extract("IBKR 买入 NVDA 1股 @180")
    assert ext is not None and ext.suggested_symbol == "NVDA"
    assert len(calls) == 2
