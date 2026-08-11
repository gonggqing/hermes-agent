"""Tests for swing_trader.llm — fail-safe LLM analysis layer (Loop.md §3/§5.3)."""

import pytest

from swing_trader.llm import (
    LLMAnalyst,
    LLMSettings,
    hermes_primary_model,
    http_complete,
    llm_settings_from_env,
)
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


def test_decision_role_uses_hermes_primary_model(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "model:\n  default: MiniMax-M3\n  provider: minimax-cn\n",
        encoding="utf-8",
    )
    env = {
        "HERMES_HOME": str(tmp_path),
        "FINANCE_LLM_PROVIDER": "minimax",
        "MINIMAX_CN_API_KEY": "secret",
    }
    assert hermes_primary_model(env) == "MiniMax-M3"
    settings = llm_settings_from_env(env, role="decision")
    assert settings is not None and settings.model == "MiniMax-M3"


def test_explicit_finance_decision_model_overrides_hermes_config(tmp_path):
    (tmp_path / "config.yaml").write_text("model: primary-model\n", encoding="utf-8")
    settings = llm_settings_from_env(
        {
            "HERMES_HOME": str(tmp_path),
            "DEEPSEEK_API_KEY": "secret",
            "FINANCE_LLM_MODEL": "explicit-finance-model",
        },
        role="decision",
    )
    assert settings is not None and settings.model == "explicit-finance-model"


def test_key_never_in_signal():
    sig = analyst('{"direction": "long", "confidence": 0.6, "thesis": "t"}') \
        .analyze("NVDA", {}, [])
    assert "k" != sig.thesis and SETTINGS.api_key not in repr(sig)


def test_http_complete_splits_minimax_reasoning_only(monkeypatch):
    payloads: list[dict] = []

    class _Response:
        def raise_for_status(self):
            return None

        def iter_lines(self, *, decode_unicode):
            assert decode_unicode is True
            return iter([
                'data: {"choices":[{"delta":{"content":"{\\"ok\\":"}}]}',
                'data: {"choices":[{"delta":{"content":"true}"}}]}',
                "data: [DONE]",
            ])

        def json(self):
            return {"choices": [{"message": {"content": '{"ok":true}'}}]}

    def post(_url, *, headers, json, timeout, stream):
        assert headers["Authorization"] == "Bearer secret"
        assert timeout == 20.0
        assert stream is (json.get("stream") is True)
        payloads.append(json)
        return _Response()

    monkeypatch.setattr("requests.post", post)

    minimax = LLMSettings(
        base_url="https://api.minimaxi.com/v1",
        model="MiniMax-M3",
        api_key="secret",
    )
    other = LLMSettings(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        api_key="secret",
    )
    assert http_complete(minimax, "system", "prompt") == '{"ok":true}'
    assert http_complete(other, "system", "prompt") == '{"ok":true}'
    assert payloads[0]["reasoning_split"] is True
    assert payloads[0]["stream"] is True
    assert "reasoning_split" not in payloads[1]
    assert "stream" not in payloads[1]


def test_http_complete_recovers_provider_reasoning_fields(monkeypatch):
    streams = iter([
        [
            'data: {"choices":[{"delta":{"reasoning_content":"{\\"action\\":\\""}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"keep\\"}"}}]}',
        ],
        [
            'data: {"choices":[{"delta":{"reasoning_details":[{"type":"text","text":"{\\"action\\":\\"keep\\"}"}]}}]}',
        ],
    ])

    class _Response:
        def raise_for_status(self):
            return None

        def iter_lines(self, *, decode_unicode):
            assert decode_unicode is True
            return iter(next(streams))

    monkeypatch.setattr("requests.post", lambda *_args, **_kwargs: _Response())
    settings = LLMSettings("https://api.minimaxi.com/v1", "MiniMax-M3", "secret")

    assert http_complete(settings, "system", "prompt") == '{"action":"keep"}'
    assert http_complete(settings, "system", "prompt") == '{"action":"keep"}'
