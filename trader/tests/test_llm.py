"""Tests for swing_trader.llm — fail-safe LLM analysis layer (Loop.md §3/§5.3)."""

import pytest

from swing_trader.llm import LLMAnalyst, LLMSettings, llm_settings_from_env
from swing_trader.schemas import Direction

SETTINGS = LLMSettings(base_url="https://x", model="test-model", api_key="k")


def analyst(reply):
    if isinstance(reply, Exception):
        def complete(_s, _sys, _p):
            raise reply
    else:
        def complete(_s, _sys, _p):
            return reply
    return LLMAnalyst(SETTINGS, complete=complete)


def test_valid_json_becomes_signal():
    sig = analyst('{"direction": "long", "confidence": 0.66, "thesis": "trend up"}') \
        .analyze("NVDA", {"rsi": 60}, ["NVDA beats"], regime="risk_on")
    assert sig is not None
    assert sig.direction is Direction.LONG
    assert sig.confidence == pytest.approx(0.66)
    assert sig.source_agent == "llm:test-model"


def test_json_wrapped_in_prose_is_extracted():
    sig = analyst('Sure! {"direction": "neutral", "confidence": 0.5, "thesis": "mixed"} hope this helps') \
        .analyze("MU", {}, [])
    assert sig is not None and sig.direction is Direction.NEUTRAL


def test_confidence_capped_at_0_8():
    sig = analyst('{"direction": "long", "confidence": 0.99, "thesis": "moon"}') \
        .analyze("NVDA", {}, [])
    assert sig.confidence == pytest.approx(0.8)


@pytest.mark.parametrize("bad", [
    "not json at all",
    '{"direction": "yolo", "confidence": 0.5}',
    '{"confidence": 0.5}',
    RuntimeError("timeout"),
])
def test_any_failure_returns_none(bad):
    assert analyst(bad).analyze("NVDA", {}, []) is None


def test_settings_from_env_provider_chain():
    assert llm_settings_from_env({}) is None
    s = llm_settings_from_env({"DEEPSEEK_API_KEY": "d"})
    assert s.model == "deepseek-v4-flash" and "deepseek" in s.base_url
    s = llm_settings_from_env({"GLM_API_KEY": "g"})
    assert s.model == "glm5-turbo"
    s = llm_settings_from_env({
        "FINANCE_LLM_PROVIDER": "glm", "GLM_API_KEY": "g",
        "DEEPSEEK_API_KEY": "d",
    })
    assert s.model == "glm5-turbo"  # explicit provider wins


def test_search_role_pins_cheap_model():
    # Default role="search" (the search/summary subagent) stays on the CHEAP
    # flash model to save token cost — it IGNORES FINANCE_LLM_MODEL so a pricier
    # decision model configured for another role does not raise its bill.
    s = llm_settings_from_env({
        "DEEPSEEK_API_KEY": "d", "FINANCE_LLM_MODEL": "deepseek-v4",
    })
    assert s.model == "deepseek-v4-flash"  # search role: FINANCE_LLM_MODEL ignored
    # FINANCE_LLM_SEARCH_MODEL overrides the search-tier model explicitly.
    s = llm_settings_from_env({
        "DEEPSEEK_API_KEY": "d", "FINANCE_LLM_SEARCH_MODEL": "deepseek-lite",
    })
    assert s.model == "deepseek-lite"
    # A non-search role uses FINANCE_LLM_MODEL (the decision/general tier).
    s = llm_settings_from_env(
        {"DEEPSEEK_API_KEY": "d", "FINANCE_LLM_MODEL": "deepseek-v4"},
        role="decision",
    )
    assert s.model == "deepseek-v4"


def test_key_never_in_signal():
    sig = analyst('{"direction": "long", "confidence": 0.6, "thesis": "t"}') \
        .analyze("NVDA", {}, [])
    assert "k" != sig.thesis and SETTINGS.api_key not in repr(sig)
