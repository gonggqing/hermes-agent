"""Finance toolset for the general Hermes agent (Loop.md Phase 0.75-B2).

Read/analysis-only thin wrappers over the Finance service. Tests validate
handler param handling, error surfacing, availability gating, and that NO
write/order tool is registered — all without network (``_finance_get`` mocked).
"""

import json

import pytest

import tools.finance_tools as ft
from tools.registry import registry


@pytest.fixture
def capture(monkeypatch):
    """Replace the HTTP call with a recorder returning a canned OK payload."""
    calls = []

    def fake_get(path, params=None):
        calls.append((path, dict(params or {})))
        return True, {"ok": path, "params": params or {}}

    monkeypatch.setattr(ft, "_finance_get", fake_get)
    return calls


# ---------------------------------------------------------------- availability

def test_check_available_gates_on_env(monkeypatch):
    monkeypatch.delenv("HERMES_FINANCE_SERVICE_URL", raising=False)
    assert ft._check_finance_available() is False
    monkeypatch.setenv("HERMES_FINANCE_SERVICE_URL", "http://127.0.0.1:9319")
    assert ft._check_finance_available() is True


# ------------------------------------------------------------------- handlers

def test_get_quote_requires_symbol_and_calls_endpoint(capture):
    assert json.loads(ft._handle_get_quote({}))["error"].startswith("Missing")
    ft._handle_get_quote({"symbol": "nvda"})
    assert capture == [("/v1/quote", {"symbol": "nvda"})]


def test_get_kline_clamps_limit_and_passes_timeframe(capture):
    ft._handle_get_kline({"symbol": "0700.HK", "timeframe": "1wk", "limit": 9999})
    path, params = capture[0]
    assert path == "/v1/bars"
    assert params["symbol"] == "0700.HK" and params["timeframe"] == "1wk"
    assert params["limit"] == 500  # clamped to max
    assert "error" in json.loads(ft._handle_get_kline({"symbol": "X", "limit": "abc"}))


def test_analyze_symbol_calls_analyze(capture):
    ft._handle_analyze_symbol({"symbol": "TSM"})
    assert capture == [("/v1/analyze", {"symbol": "TSM"})]


def test_research_brief_market_routing(capture):
    ft._handle_research_brief({})  # default us -> no market param
    ft._handle_research_brief({"market": "cn"})
    assert capture[0] == ("/v1/research/brief", {})
    assert capture[1] == ("/v1/research/brief", {"market": "cn"})
    assert "error" in json.loads(ft._handle_research_brief({"market": "jp"}))


def test_search_research_requires_query(capture):
    assert "error" in json.loads(ft._handle_search_research({"query": "a"}))  # too short
    ft._handle_search_research({"query": "ai capex", "k": 3})
    assert capture == [("/v1/knowledge/search", {"q": "ai capex", "k": 3})]


def test_account_view_mode_optional(capture):
    ft._handle_account_view({})
    ft._handle_account_view({"mode": "paper"})
    assert capture[0] == ("/v1/account", {})
    assert capture[1] == ("/v1/account", {"mode": "paper"})


def test_error_from_service_is_surfaced(monkeypatch):
    monkeypatch.setattr(
        ft, "_finance_get",
        lambda path, params=None: (False, {"error": "finance service HTTP 503: idle",
                                           "status": 503}),
    )
    out = json.loads(ft._handle_get_quote({"symbol": "NVDA"}))
    assert out["error"].startswith("finance service HTTP 503") and out["status"] == 503


# --------------------------------------------------------------- registration

def test_finance_toolset_registered_and_read_only():
    names = set(registry.get_tool_names_for_toolset("finance"))
    assert names == {
        "get_quote", "get_kline", "analyze_symbol",
        "research_brief", "search_research", "account_view",
    }
    # HARD guardrail (Loop.md §3/§8): the toolset exposes NO write/order/approve
    # capability to the general agent.
    forbidden = {"place_order", "approve", "approve_candidate", "cancel_order",
                 "submit_order", "execute"}
    assert not (names & forbidden)
    for n in names:
        entry = registry.get_entry(n)
        assert entry is not None and entry.toolset == "finance"
        assert entry.check_fn is ft._check_finance_available
