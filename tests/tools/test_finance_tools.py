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
        # Phase 0.9 portfolio: read + DRAFT-ONLY (no confirm/place tool).
        "portfolio_accounts", "portfolio_holdings",
        "draft_portfolio_trade", "draft_close_position",
    }
    # HARD guardrail (Loop.md §3/§8, P0.9 boundary #4/#8): the toolset exposes NO
    # write/order/approve capability AND no portfolio-CONFIRM/mutate tool — the
    # LLM may only draft; a human confirms on an authenticated surface.
    forbidden = {"place_order", "approve", "approve_candidate", "cancel_order",
                 "submit_order", "execute", "confirm_portfolio_event",
                 "confirm_draft", "update_position", "commit_portfolio"}
    assert not (names & forbidden)
    for n in names:
        entry = registry.get_entry(n)
        assert entry is not None and entry.toolset == "finance"
        assert entry.check_fn is ft._check_finance_available


# ------------------------------------------------------- portfolio (P0.9)


@pytest.fixture
def capture_post(monkeypatch):
    """Record POSTs (draft creation) so no network is hit."""
    calls = []

    def fake_post(path, body, *, params=None, surface="system"):
        calls.append({"path": path, "body": body, "params": dict(params or {}),
                      "surface": surface})
        return True, {"ok": path, "status": "draft"}

    monkeypatch.setattr(ft, "_finance_post", fake_post)
    return calls


def test_portfolio_accounts_and_holdings(capture):
    ft._handle_portfolio_accounts({})
    assert capture[-1][0] == "/v1/portfolio/accounts"
    assert "Missing" in ft._handle_portfolio_holdings({})
    ft._handle_portfolio_holdings({"account_id": "acc1"})
    assert capture[-1][0] == "/v1/portfolio/accounts/acc1/holdings"


def test_draft_portfolio_trade_posts_draft_as_system(capture_post):
    out = ft._handle_draft_portfolio_trade({
        "account_id": "acc1", "event_type": "buy", "symbol": "NVDA",
        "qty": 3, "price": 208.5, "commission": 1,
        "original_text": "bought 3 NVDA at 208.5",
    })
    assert json.loads(out)["status"] == "draft"
    call = capture_post[-1]
    assert call["path"] == "/v1/portfolio/drafts"
    assert call["surface"] == "system"  # never a human surface → cannot self-confirm
    assert call["body"]["created_by"] == "hermes"
    assert call["body"]["qty"] == 3.0 and call["body"]["price"] == 208.5


def test_draft_trade_requires_account(capture_post):
    assert "account_id" in ft._handle_draft_portfolio_trade({"symbol": "NVDA"})


def test_draft_trade_rejects_bad_number(capture_post):
    assert "Invalid" in ft._handle_draft_portfolio_trade(
        {"account_id": "a", "qty": "lots"})


def test_draft_close_position(capture_post):
    ft._handle_draft_close_position({"account_id": "acc1", "symbol": "NVDA"})
    call = capture_post[-1]
    assert call["path"] == "/v1/portfolio/accounts/acc1/close-draft"
    assert call["params"] == {"symbol": "NVDA"} and call["surface"] == "system"
    assert "required" in ft._handle_draft_close_position({"account_id": "acc1"})


def test_no_confirm_tool_exists():
    """The draft-only tools must NOT include any way to finalize a draft."""
    names = set(registry.get_tool_names_for_toolset("finance"))
    assert not any("confirm" in n or "commit" in n for n in names)
