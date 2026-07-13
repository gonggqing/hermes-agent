"""Finance tools for the general Hermes agent (Loop.md Phase 0.75 thrust B).

Thin, READ / ANALYSIS-ONLY wrappers over the local Finance service (FastAPI on
``HERMES_FINANCE_SERVICE_URL``, default ``http://127.0.0.1:9319``) so the
everyday Hermes conversational agent can, on demand, fetch a symbol's current
price and K-line, run a multi-agent analysis, read the daily research brief,
search the research knowledge store, and view the paper account.

AUTHORITY (Loop.md §3 / §8 — HARD): there is deliberately **no** order-placement
or candidate-approval tool here. Order authority stays service-bound to the
ExecutionEngine and approval stays a human-only action from an authenticated
surface. These tools cannot move money; they only read/analyze. They also never
import trader internals — they call the versioned service API, keeping the
Finance domain isolated behind its service boundary (Loop.md §8).

Availability mirrors the Home Assistant pattern: the toolset auto-enables when
``HERMES_FINANCE_SERVICE_URL`` is set (the gateway container sets it), and each
tool's ``check_fn`` hides its schema from the model when it is not.
"""

from __future__ import annotations

import json
import os

import httpx

from tools.registry import registry, tool_error

__all__ = ["FINANCE_SERVICE_URL"]

#: Base URL of the local Finance service. Mirrors hermes_cli/finance_proxy.py.
FINANCE_SERVICE_URL = os.environ.get(
    "HERMES_FINANCE_SERVICE_URL", "http://127.0.0.1:9319"
).rstrip("/")

_TIMEOUT = 15.0


def _check_finance_available() -> bool:
    """Tool is only offered to the model when the Finance service is configured."""
    return bool(os.getenv("HERMES_FINANCE_SERVICE_URL"))


def _finance_get(path: str, params: dict | None = None) -> tuple[bool, dict | list]:
    """GET ``path`` on the Finance service. Returns ``(ok, payload_or_error)``.

    Never raises: connection failures and non-200s become an error dict so the
    handler can surface them via :func:`tool_error`.
    """
    url = f"{FINANCE_SERVICE_URL}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.get(url, params=params or {})
    except Exception as exc:  # noqa: BLE001 — surfaced as a tool error
        return False, {"error": f"finance service unreachable at "
                                f"{FINANCE_SERVICE_URL}: {exc}"}
    if resp.status_code != 200:
        detail: object = resp.text
        try:
            body = resp.json()
            detail = body.get("detail", body) if isinstance(body, dict) else body
        except Exception:  # noqa: BLE001
            pass
        return False, {"error": f"finance service HTTP {resp.status_code}: {detail}",
                       "status": resp.status_code}
    try:
        return True, resp.json()
    except Exception as exc:  # noqa: BLE001
        return False, {"error": f"invalid JSON from finance service: {exc}"}


def _respond(ok: bool, payload: dict | list) -> str:
    if ok:
        return json.dumps(payload, ensure_ascii=False)
    return tool_error(payload.get("error", "finance request failed"),
                      **{k: v for k, v in payload.items() if k != "error"})


def _need_symbol(args: dict) -> str | None:
    sym = str(args.get("symbol", "")).strip()
    return sym or None


# --------------------------------------------------------------------- handlers


def _handle_get_quote(args: dict, **kw) -> str:
    sym = _need_symbol(args)
    if sym is None:
        return tool_error("Missing required parameter: symbol")
    return _respond(*_finance_get("/v1/quote", {"symbol": sym}))


def _handle_get_kline(args: dict, **kw) -> str:
    sym = _need_symbol(args)
    if sym is None:
        return tool_error("Missing required parameter: symbol")
    params = {"symbol": sym, "timeframe": str(args.get("timeframe", "1d"))}
    limit = args.get("limit")
    if limit is not None:
        try:
            params["limit"] = max(1, min(500, int(limit)))
        except (TypeError, ValueError):
            return tool_error(f"Invalid limit: {limit!r}")
    return _respond(*_finance_get("/v1/bars", params))


def _handle_analyze_symbol(args: dict, **kw) -> str:
    sym = _need_symbol(args)
    if sym is None:
        return tool_error("Missing required parameter: symbol")
    return _respond(*_finance_get("/v1/analyze", {"symbol": sym}))


def _handle_research_brief(args: dict, **kw) -> str:
    market = str(args.get("market", "us")).strip().lower()
    if market not in ("us", "cn"):
        return tool_error("market must be 'us' or 'cn'")
    params = {"market": "cn"} if market == "cn" else {}
    return _respond(*_finance_get("/v1/research/brief", params))


def _handle_search_research(args: dict, **kw) -> str:
    query = str(args.get("query", "")).strip()
    if len(query) < 2:
        return tool_error("Missing/short required parameter: query (>= 2 chars)")
    params: dict = {"q": query}
    k = args.get("k")
    if k is not None:
        try:
            params["k"] = max(1, min(25, int(k)))
        except (TypeError, ValueError):
            return tool_error(f"Invalid k: {k!r}")
    return _respond(*_finance_get("/v1/knowledge/search", params))


def _handle_account_view(args: dict, **kw) -> str:
    params = {}
    mode = str(args.get("mode", "")).strip().lower()
    if mode in ("paper", "live"):
        params["mode"] = mode
    return _respond(*_finance_get("/v1/account", params))


# ---------------------------------------------------------------------- schemas

_GET_QUOTE_SCHEMA = {
    "name": "get_quote",
    "description": (
        "Get the latest (possibly ~15-min delayed) market price for one stock/ETF "
        "symbol — current price feedback. Works for US tickers (e.g. NVDA, TSM) and "
        "Hong Kong / China tickers (e.g. 0700.HK, 600519.SS). Research only, not for "
        "execution timing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, e.g. 'NVDA', '0700.HK', '600519.SS'."},
        },
        "required": ["symbol"],
    },
}

_GET_KLINE_SCHEMA = {
    "name": "get_kline",
    "description": (
        "Get K-line / candlestick OHLCV bars for one symbol (for charting or trend "
        "analysis). Returns a list of {ts, open, high, low, close, volume}."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker, e.g. 'NVDA', '0700.HK'."},
            "timeframe": {"type": "string", "description": "Bar size (default '1d'; e.g. '1d', '1h', '1wk')."},
            "limit": {"type": "integer", "description": "Number of most-recent bars (1-500, default 120)."},
        },
        "required": ["symbol"],
    },
}

_ANALYZE_SYMBOL_SCHEMA = {
    "name": "analyze_symbol",
    "description": (
        "Run a one-shot multi-agent analysis of one symbol: technical + fundamental + "
        "sentiment sub-agents synthesized by a bull/bear debate into a direction + "
        "confidence verdict, with the current price and recent news. READ-ONLY research "
        "— it forms a thesis, it does NOT place or approve any order."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {"type": "string", "description": "Ticker to analyze, e.g. 'NVDA', '0981.HK'."},
        },
        "required": ["symbol"],
    },
}

_RESEARCH_BRIEF_SCHEMA = {
    "name": "research_brief",
    "description": (
        "Get today's Investment Research brief: market regime, risk/freshness warnings, "
        "watchlist movers, themes, news digest, and analysis signals. market='us' (default) "
        "for the US evening desk or market='cn' for the China/HK morning research desk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "market": {"type": "string", "enum": ["us", "cn"], "description": "'us' (default) or 'cn'."},
        },
        "required": [],
    },
}

_SEARCH_RESEARCH_SCHEMA = {
    "name": "search_research",
    "description": (
        "Semantic search over the local finance research knowledge store (archived news / "
        "research documents). Returns source-linked results with provenance. Fails closed if "
        "the vector index is unavailable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query (>= 2 chars)."},
            "k": {"type": "integer", "description": "Max results (1-25, default 5)."},
        },
        "required": ["query"],
    },
}

_ACCOUNT_VIEW_SCHEMA = {
    "name": "account_view",
    "description": (
        "View the paper trading account: equity, cash, open positions, working orders, "
        "breaker state, and cumulative stats. READ-ONLY. mode='paper' (default) or 'live'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["paper", "live"], "description": "'paper' (default) or 'live'."},
        },
        "required": [],
    },
}


# ------------------------------------------------------------------- registration

for _name, _schema, _handler in (
    ("get_quote", _GET_QUOTE_SCHEMA, _handle_get_quote),
    ("get_kline", _GET_KLINE_SCHEMA, _handle_get_kline),
    ("analyze_symbol", _ANALYZE_SYMBOL_SCHEMA, _handle_analyze_symbol),
    ("research_brief", _RESEARCH_BRIEF_SCHEMA, _handle_research_brief),
    ("search_research", _SEARCH_RESEARCH_SCHEMA, _handle_search_research),
    ("account_view", _ACCOUNT_VIEW_SCHEMA, _handle_account_view),
):
    registry.register(
        name=_name,
        toolset="finance",
        schema=_schema,
        handler=_handler,
        check_fn=_check_finance_available,
        emoji="📈",
    )
