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


def _finance_post(path: str, body: dict, *, params: dict | None = None,
                  surface: str = "system") -> tuple[bool, dict | list]:
    """POST ``path`` on the Finance service (used ONLY to create DRAFTS — never
    to confirm/place). ``X-Finance-Surface: system`` marks these as Hermes-
    drafted, which the service refuses to let system/LLM finalize (boundary #4).
    Never raises; failures become an error dict."""
    url = f"{FINANCE_SERVICE_URL}{path}"
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            resp = client.post(url, json=body, params=params or {},
                               headers={"X-Finance-Surface": surface})
    except Exception as exc:  # noqa: BLE001 — surfaced as a tool error
        return False, {"error": f"finance service unreachable at "
                                f"{FINANCE_SERVICE_URL}: {exc}"}
    if resp.status_code not in (200, 201):
        detail: object = resp.text
        try:
            body_json = resp.json()
            detail = body_json.get("detail", body_json) if isinstance(body_json, dict) else body_json
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
        "View the PAPER TRADING SIMULATION account (the system's own trading loop): "
        "equity, cash, open positions, working orders, breaker state, cumulative stats. "
        "This is NOT the user's real holdings — for '我的持仓/portfolio/持仓/盈亏' use "
        "portfolio_valuation / portfolio_holdings instead. READ-ONLY. mode='paper' (default)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["paper", "live"], "description": "'paper' (default) or 'live'."},
        },
        "required": [],
    },
}


# ------------------------------------------------------- portfolio (P0.9, draft-only)
# Read the user's REAL holdings and DRAFT portfolio events from a conversation
# ("today I bought 3 NVDA at 208.5"). These create a DRAFT only — a human must
# confirm it on an authenticated surface (Desktop/Web/Telegram) before any
# append-only event is recorded. There is deliberately NO confirm/update tool
# (Loop.md P0.9 boundary #4/#8): free-form conversation can never mutate holdings.


def _handle_portfolio_accounts(args: dict, **kw) -> str:
    return _respond(*_finance_get("/v1/portfolio/accounts"))


def _handle_portfolio_holdings(args: dict, **kw) -> str:
    acct = str(args.get("account_id", "")).strip()
    if not acct:
        return tool_error("Missing required parameter: account_id "
                          "(use portfolio_accounts to list account ids)")
    return _respond(*_finance_get(f"/v1/portfolio/accounts/{acct}/holdings"))


def _handle_draft_portfolio_trade(args: dict, **kw) -> str:
    acct = str(args.get("account_id", "")).strip()
    et = str(args.get("event_type", "buy")).strip().lower()
    if not acct:
        return tool_error("Missing required parameter: account_id")
    body: dict = {
        "account_id": acct,
        "event_type": et,
        "created_by": "hermes",
        "original_text": str(args.get("original_text", ""))[:2000],
    }
    for key in ("symbol", "market", "currency", "occurred_at", "note", "external_id"):
        v = args.get(key)
        if v not in (None, ""):
            body[key] = v
    for key in ("qty", "price", "commission", "amount"):
        v = args.get(key)
        if v is not None:
            try:
                body[key] = float(v)
            except (TypeError, ValueError):
                return tool_error(f"Invalid numeric value for {key}: {v!r}")
    ambig = args.get("ambiguities")
    if isinstance(ambig, list) and ambig:
        body["ambiguities"] = [str(a)[:200] for a in ambig]
    return _respond(*_finance_post("/v1/portfolio/drafts", body, surface="system"))


def _handle_portfolio_valuation(args: dict, **kw) -> str:
    acct = str(args.get("account_id", "")).strip()
    if acct:
        return _respond(*_finance_get(f"/v1/portfolio/accounts/{acct}/valuation"))
    return _respond(*_finance_get("/v1/portfolio/valuation"))


def _handle_draft_close_position(args: dict, **kw) -> str:
    acct = str(args.get("account_id", "")).strip()
    sym = _need_symbol(args)
    if not acct or sym is None:
        return tool_error("Both account_id and symbol are required")
    return _respond(*_finance_post(
        f"/v1/portfolio/accounts/{acct}/close-draft", {}, params={"symbol": sym},
        surface="system"))


_PORTFOLIO_ACCOUNTS_SCHEMA = {
    "name": "portfolio_accounts",
    "description": (
        "List the user's REAL portfolio accounts (US/HK/CN) with id, name, market, "
        "currency and provider. READ-ONLY. Use this to get an account_id before "
        "reading holdings or drafting a trade."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_PORTFOLIO_HOLDINGS_SCHEMA = {
    "name": "portfolio_holdings",
    "description": (
        "View current holdings + cash for one real portfolio account, derived from "
        "its append-only events. Cost basis may be null when unknown (never guessed). "
        "READ-ONLY."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Account id (see portfolio_accounts)."},
        },
        "required": ["account_id"],
    },
}

_DRAFT_TRADE_SCHEMA = {
    "name": "draft_portfolio_trade",
    "description": (
        "Turn a user statement about a REAL trade or holding (e.g. 'today I bought 3 "
        "NVDA at 208.5, $1 fee') into a portfolio-event DRAFT for the user to review "
        "and confirm. This does NOT change any holdings — it only proposes a draft; "
        "the user must confirm it on Desktop/Web/Telegram. If the account, symbol, "
        "quantity, price, currency or whether the order actually FILLED is unclear, do "
        "NOT guess — ask the user, or pass what is uncertain in 'ambiguities'. Unknown "
        "price is allowed (cost basis stays unknown)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Target account id (see portfolio_accounts)."},
            "event_type": {"type": "string",
                           "enum": ["buy", "sell", "opening_balance", "dividend",
                                    "fee", "cash_transfer", "split"],
                           "description": "Kind of event (default buy)."},
            "symbol": {"type": "string", "description": "Instrument symbol (e.g. NVDA, 0700.HK, 600519.SS)."},
            "market": {"type": "string", "enum": ["US", "HK", "CN"]},
            "currency": {"type": "string", "description": "ISO currency (USD/HKD/CNY)."},
            "qty": {"type": "number", "description": "Share quantity (>0) for buy/sell."},
            "price": {"type": "number", "description": "Execution price per share; omit if unknown."},
            "commission": {"type": "number", "description": "Commission/fees, if known."},
            "amount": {"type": "number", "description": "Cash amount for dividend/fee/cash_transfer."},
            "occurred_at": {"type": "string", "description": "ISO timestamp of the trade, if known."},
            "original_text": {"type": "string", "description": "The user's original statement (for the audit trail)."},
            "ambiguities": {"type": "array", "items": {"type": "string"},
                            "description": "Anything unclear that the user must clarify before confirming."},
        },
        "required": ["account_id"],
    },
}

_PORTFOLIO_VALUATION_SCHEMA = {
    "name": "portfolio_valuation",
    "description": (
        "The user's REAL portfolio holdings WITH market value + unrealized P&L "
        "(现价/市值/盈亏/盈亏%). This is their ACTUAL money across their real "
        "accounts (e.g. 蚂蚁财富/平安证券) — NOT the paper trading account "
        "(use account_view for that). Pass account_id for one account, omit for "
        "all accounts aggregated with per-currency totals. Prices come from live "
        "quotes or imported/manual marks; some 场外基金 may be unpriced (price/"
        "market_value/pnl = null — say '未知', never 0). READ-ONLY. Prefer this "
        "over portfolio_holdings when the user wants value or 盈亏."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "One account id (omit for all accounts)."},
        },
        "required": [],
    },
}

_DRAFT_CLOSE_SCHEMA = {
    "name": "draft_close_position",
    "description": (
        "Draft a FULL exit of a holding ('I cleared my NVDA') — proposes selling the "
        "current quantity from that account for the user to confirm. Does NOT change "
        "holdings; the user must confirm. A placed-but-unfilled order does not reduce "
        "holdings — if unsure whether it actually filled, ask the user instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_id": {"type": "string", "description": "Account id (see portfolio_accounts)."},
            "symbol": {"type": "string", "description": "Symbol to close."},
        },
        "required": ["account_id", "symbol"],
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
    # Phase 0.9 portfolio — read + DRAFT-ONLY (no confirm/place; human confirms).
    ("portfolio_accounts", _PORTFOLIO_ACCOUNTS_SCHEMA, _handle_portfolio_accounts),
    ("portfolio_holdings", _PORTFOLIO_HOLDINGS_SCHEMA, _handle_portfolio_holdings),
    ("portfolio_valuation", _PORTFOLIO_VALUATION_SCHEMA, _handle_portfolio_valuation),
    ("draft_portfolio_trade", _DRAFT_TRADE_SCHEMA, _handle_draft_portfolio_trade),
    ("draft_close_position", _DRAFT_CLOSE_SCHEMA, _handle_draft_close_position),
):
    registry.register(
        name=_name,
        toolset="finance",
        schema=_schema,
        handler=_handler,
        check_fn=_check_finance_available,
        emoji="📈",
    )
