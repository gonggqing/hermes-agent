"""Finance service HTTP API (Loop.md §5.6, §5.9, §8) — versioned `/v1`.

This FastAPI app runs INSIDE the trading-loop process, so there is exactly
ONE server-authoritative :class:`~swing_trader.confirmation.ConfirmationService`
shared by every surface: the Hermes dashboard mounts a thin reverse proxy at
``/api/finance/* -> http://127.0.0.1:9319/v1/*`` (inheriting dashboard auth),
the Desktop app reaches the same proxy through its own backend, and the
Telegram adapter calls the service in-process. No surface holds state.

Authority (Loop.md §3): every write endpoint maps to a HUMAN action relayed
by an authenticated surface. There is deliberately NO endpoint that places
orders — only :class:`~swing_trader.execution.ExecutionEngine` (driven by
the daily loop after cutoff) submits to the broker.

The app degrades gracefully when the daily loop is idle (evenings, weekends):
read endpoints fall back to the ledger (last snapshot, recorded orders), and
the pending queue is simply empty outside the confirmation window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from pydantic import BaseModel, Field

from swing_trader import watchlist as watchlist_mod
from swing_trader.confirmation import ConfirmationService, ResultCode, Surface
from swing_trader.interfaces import BrokerInterface
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.portfolio_draft import DraftResultCode
from swing_trader.reporter import build_account_view
from swing_trader.schemas import CandidateStatus, Mode

logger = get_logger(__name__)

__all__ = ["FinanceRuntime", "create_app", "DEFAULT_SERVICE_PORT"]

DEFAULT_SERVICE_PORT = 9319

API_VERSION = "v1"

#: Honest provenance/delay note on every on-demand quote/bar (Loop.md §5.9,
#: §8 data policy): free Yahoo Finance data can lag real time and must never be
#: treated as an execution-grade tick.
MARKET_DATA_NOTE = (
    "prices/bars via Yahoo Finance (yfinance) — may be delayed (~15 min for "
    "many symbols); for research/analysis only, not execution timing"
)


@dataclass
class FinanceRuntime:
    """Shared state the daily loop keeps current and the API reads.

    ``confirmation`` is swapped in each morning by the loop and cleared after
    execution; ``market``/``reports`` are plain dicts the loop updates.
    """

    ledger: Ledger
    mode: Mode = Mode.PAPER
    broker: Optional[BrokerInterface] = None
    confirmation: Optional[ConfirmationService] = None
    market: dict = field(default_factory=dict)  # latest MarketSnapshot dump
    market_cn: dict = field(default_factory=dict)  # latest CN MarketSnapshot dump
    latest_reports: dict = field(default_factory=dict)  # kind -> text
    latest_brief: dict = field(default_factory=dict)  # US ResearchBrief dump
    latest_brief_cn: dict = field(default_factory=dict)  # CN ResearchBrief dump
    knowledge: Any = None  # FinanceKnowledge (Phase 0.5)
    knowledge_index: Any = None  # KnowledgeIndex | None (fail-closed)
    # Phase 0.75 (thrust B): on-demand analysis for the conversational agent.
    feed: Any = None  # DataFeed — real-time-ish quotes + K-line bars
    fundamentals: Any = None  # FundamentalsProvider — on-demand fundamentals
    llm_analyst: Any = None  # optional LLMAnalyst voice for /v1/analyze
    # Phase 0.8 (resilience): last HealthStatus the loop assessed at decide time.
    health: Any = None  # swing_trader.health.HealthStatus | None
    # Phase 0.9 (portfolio): instrument type-ahead + the append-only journal.
    instrument_search: Any = None  # CachedInstrumentSearch | None
    portfolio: Any = None  # swing_trader.portfolio_journal.PortfolioJournal | None
    portfolio_drafts: Any = None  # swing_trader.portfolio_draft.PortfolioDraftService
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


class ActionRequest(BaseModel):
    action: str = Field(pattern="^(approve|reject|edit)$")
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: Optional[int] = None
    edits: Optional[dict] = None
    # Fallback for clients whose transport cannot set custom headers (the
    # Desktop IPC bridge forwards only method/body). Header wins when set.
    surface: Optional[str] = None


_RESULT_HTTP: dict[ResultCode, int] = {
    ResultCode.APPLIED: 200,
    ResultCode.REPLAYED: 200,
    ResultCode.WINDOW_CLOSED: 403,
    ResultCode.TERMINAL: 409,
    ResultCode.VERSION_CONFLICT: 409,
    ResultCode.UNKNOWN_CANDIDATE: 404,
    ResultCode.INVALID_EDIT: 422,
    ResultCode.INVALID_ACTION: 422,
}

_DRAFT_HTTP: dict[DraftResultCode, int] = {
    DraftResultCode.APPLIED: 200,
    DraftResultCode.REPLAYED: 200,
    DraftResultCode.INCOMPLETE: 422,
    DraftResultCode.NOT_HUMAN: 403,
    DraftResultCode.UNKNOWN_DRAFT: 404,
    DraftResultCode.TERMINAL: 409,
    DraftResultCode.VERSION_CONFLICT: 409,
    DraftResultCode.INVALID_EDIT: 422,
}


# ------------------------------------------------------- Portfolio (P0.9)


class PortfolioAccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    market_scope: str
    base_currency: str = Field(min_length=1, max_length=8)
    provider: str = "manual"
    account_type: str = "cash"
    include_in_risk: bool = True
    note: str = ""
    actor: str = Field(min_length=1, max_length=200)


class PortfolioAccountUpdate(BaseModel):
    name: Optional[str] = None
    include_in_risk: Optional[bool] = None
    note: Optional[str] = None
    account_type: Optional[str] = None
    actor: str = Field(min_length=1, max_length=200)


class PortfolioDraftCreate(BaseModel):
    account_id: Optional[str] = None
    event_type: str = "buy"
    symbol: Optional[str] = None
    market: Optional[str] = None
    currency: Optional[str] = None
    qty: Optional[float] = None
    price: Optional[float] = None
    commission: Optional[float] = None
    amount: Optional[float] = None
    occurred_at: Optional[datetime] = None
    source: str = "manual"
    external_id: Optional[str] = None
    reverses_event_id: Optional[str] = None
    note: str = ""
    original_text: str = ""
    ambiguities: Optional[list[str]] = None
    created_by: str = "hermes"
    surface: Optional[str] = None


class PortfolioDraftAction(BaseModel):
    action: str = Field(pattern="^(edit|reject|confirm)$")
    actor: str = Field(min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    expected_version: Optional[int] = None
    edits: Optional[dict] = None
    surface: Optional[str] = None


class PortfolioImportRequest(BaseModel):
    csv: str = Field(min_length=1, max_length=2_000_000)
    actor: Optional[str] = Field(default=None, max_length=200)


def create_app(runtime: FinanceRuntime):
    """Build the FastAPI app (fastapi imported lazily — `service` extra)."""
    from fastapi import FastAPI, Header, HTTPException, Query
    from fastapi.responses import JSONResponse

    app = FastAPI(
        title="Hermes Finance Service",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
    )

    def _mode(mode: Optional[str]) -> Mode:
        try:
            return Mode(mode) if mode else runtime.mode
        except ValueError:
            raise HTTPException(422, f"unknown mode {mode!r}")

    # ------------------------------------------------------------- reads

    @app.get(f"/{API_VERSION}/health")
    def health() -> dict:
        now = runtime.clock()
        breaker = "UNKNOWN"
        if runtime.broker is not None:
            breaker = runtime.broker.get_account().breaker_state.value
        else:
            snaps = runtime.ledger.get_snapshots(runtime.mode)
            if snaps:
                breaker = snaps[-1].breaker_state.value
        out = {
            "status": "ok",
            "mode": runtime.mode.value,
            "loop_attached": runtime.broker is not None,
            "breaker": breaker,
            "ts": now.isoformat(),
        }
        # Phase 0.8: surface the loop's last health assessment (dead-man's
        # switch + freshness/reconciliation checks) so the Finance tab and the
        # reporter bot can show *why* new entries may be halted. Read-only.
        h = runtime.health
        if h is not None:
            out["health"] = {
                "level": h.level.value,
                "entries_allowed": h.entries_allowed,
                "as_of": h.as_of.isoformat(),
                "warnings": list(h.warnings),
                "checks": [
                    {"name": c.name, "level": c.level.value, "detail": c.detail}
                    for c in h.checks
                ],
            }
        return out

    @app.get(f"/{API_VERSION}/account")
    def account(mode: Optional[str] = Query(default=None)) -> dict:
        m = _mode(mode)
        if runtime.broker is not None and m is runtime.mode:
            view = build_account_view(runtime.broker, runtime.ledger, m)
            return view.model_dump(mode="json")
        # Ledger-only fallback (loop idle / other mode): last snapshot + rows.
        snaps = runtime.ledger.get_snapshots(m)
        stats = runtime.ledger.stats(m)
        return {
            "mode": m.value,
            "snapshot": snaps[-1].model_dump(mode="json") if snaps else None,
            "stats": stats.__dict__,
            "source": "ledger",
        }

    @app.get(f"/{API_VERSION}/orders")
    def orders(
        mode: Optional[str] = Query(default=None),
        active_only: bool = Query(default=False),
    ) -> list[dict]:
        rows = runtime.ledger.get_orders(mode=_mode(mode), active_only=active_only)
        return [o.model_dump(mode="json") for o in rows]

    @app.get(f"/{API_VERSION}/fills")
    def fills(mode: Optional[str] = Query(default=None)) -> list[dict]:
        return [f.model_dump(mode="json")
                for f in runtime.ledger.get_fills(_mode(mode))]

    @app.get(f"/{API_VERSION}/trades")
    def trades(
        mode: Optional[str] = Query(default=None),
        open_only: bool = Query(default=False),
    ) -> list[dict]:
        rows = runtime.ledger.get_trades(_mode(mode), open_only=open_only)
        return [t.__dict__ | {
            "mode": t.mode.value,
            "entry_ts": t.entry_ts.isoformat(),
            "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
        } for t in rows]

    @app.get(f"/{API_VERSION}/stats")
    def stats(mode: Optional[str] = Query(default=None)) -> dict:
        return runtime.ledger.stats(_mode(mode)).__dict__

    @app.get(f"/{API_VERSION}/snapshots")
    def snapshots(
        mode: Optional[str] = Query(default=None),
        limit: int = Query(default=90, ge=1, le=1000),
    ) -> list[dict]:
        rows = runtime.ledger.get_snapshots(_mode(mode))
        return [s.model_dump(mode="json") for s in rows[-limit:]]

    @app.get(f"/{API_VERSION}/market")
    def market() -> dict:
        return runtime.market or {"status": "no snapshot yet"}

    @app.get(f"/{API_VERSION}/watchlist")
    def get_watchlist() -> list[dict]:
        return [i.model_dump(mode="json") for i in watchlist_mod.UNIVERSE]

    @app.get(f"/{API_VERSION}/reports/latest")
    def latest_reports() -> dict:
        return runtime.latest_reports

    @app.get(f"/{API_VERSION}/research/brief")
    def research_brief(market: Optional[str] = Query(default=None)) -> dict:
        """Investment Research brief (Loop.md Phase 0.5). ``market=cn`` returns
        the China morning research brief; anything else returns the US brief.
        Falls back to an on-demand DEGRADED brief (explicit freshness warnings)
        when the relevant session has not produced one yet."""
        from swing_trader.brief import build_research_brief

        if market == "cn":
            if runtime.latest_brief_cn:
                return runtime.latest_brief_cn
            from zoneinfo import ZoneInfo

            brief = build_research_brief(
                runtime.ledger, runtime.mode, now=runtime.clock(),
                signals=[], candidates=[], include_account=False,
                trading_tz=ZoneInfo("Asia/Shanghai"),
                extra_uncertainty=[
                    "China research session has not run yet today — "
                    "showing an empty degraded brief"
                ],
            )
            return brief.model_dump(mode="json")

        if runtime.latest_brief:
            return runtime.latest_brief
        brief = build_research_brief(
            runtime.ledger, runtime.mode, now=runtime.clock()
        )
        return brief.model_dump(mode="json")

    # --------------------------------------------- on-demand market analysis
    # Phase 0.75 thrust B: READ/ANALYSIS-ONLY endpoints for the conversational
    # Hermes agent (and a K-line UI). There is deliberately no order/approve
    # capability here — order authority stays with ExecutionEngine (Loop.md §3).

    @app.get(f"/{API_VERSION}/quote")
    def quote(symbol: str = Query(min_length=1, max_length=24)) -> dict:
        """Latest (delayed) quote for one symbol — current price feedback."""
        if runtime.feed is None:
            raise HTTPException(503, "data feed not available (loop idle)")
        from swing_trader.datafeed import DataFeedError

        try:
            q = runtime.feed.get_quote(symbol)
        except DataFeedError as exc:
            raise HTTPException(404, f"no quote for {symbol!r}: {exc}")
        return {
            "symbol": q.symbol,
            "last": q.last,
            "bid": q.bid,
            "ask": q.ask,
            "volume": q.volume,
            "as_of": q.ts.isoformat(),
            "note": MARKET_DATA_NOTE,
        }

    @app.get(f"/{API_VERSION}/bars")
    def bars(
        symbol: str = Query(min_length=1, max_length=24),
        timeframe: str = Query(default="1d"),
        limit: int = Query(default=120, ge=1, le=500),
    ) -> dict:
        """K-line / candlestick OHLCV bars for one symbol (charting)."""
        if runtime.feed is None:
            raise HTTPException(503, "data feed not available (loop idle)")
        from swing_trader.datafeed import DataFeedError

        try:
            rows = runtime.feed.get_bars(symbol, timeframe, limit)
        except (DataFeedError, ValueError) as exc:
            raise HTTPException(404, f"no bars for {symbol!r}: {exc}")
        return {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "bars": [
                {
                    "ts": b.ts.isoformat(),
                    "open": b.open,
                    "high": b.high,
                    "low": b.low,
                    "close": b.close,
                    "volume": b.volume,
                }
                for b in rows
            ],
            "as_of": runtime.clock().isoformat(),
            "note": MARKET_DATA_NOTE,
        }

    @app.get(f"/{API_VERSION}/analyze")
    def analyze(symbol: str = Query(min_length=1, max_length=24)) -> dict:
        """One-shot multi-agent analysis of one symbol for the chat agent:
        technical + fundamental + sentiment sub-agents synthesized by the
        bull/bear debate agent (+ the optional LLM voice). READ-ONLY — it
        forms a thesis, never a candidate/order (Loop.md §3). Shares
        swing_trader.on_demand.analyze_symbol with the finance bot."""
        if runtime.feed is None:
            raise HTTPException(503, "data feed not available (loop idle)")
        from swing_trader.datafeed import DataFeedError
        from swing_trader.on_demand import analyze_symbol

        try:
            result = analyze_symbol(
                runtime.feed, symbol,
                fundamentals=runtime.fundamentals,
                llm_analyst=runtime.llm_analyst,
                knowledge=runtime.knowledge,
                knowledge_index=runtime.knowledge_index,
                now=runtime.clock(),
            )
        except (DataFeedError, ValueError) as exc:
            raise HTTPException(404, f"no data for {symbol!r}: {exc}")
        result["as_of"] = runtime.clock().isoformat()
        result["note"] = MARKET_DATA_NOTE
        return result

    @app.get(f"/{API_VERSION}/instruments/search")
    def instruments_search(
        q: str = Query(min_length=1, max_length=64),
        market: Optional[str] = Query(default=None),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> dict:
        """Type-ahead instrument search for the Portfolio symbol field (P0.9).
        Read-only; ``degraded`` flags a source failure (never a silent empty)."""
        if runtime.instrument_search is None:
            raise HTTPException(503, "instrument search not available")
        mkt = None
        if market is not None:
            from swing_trader.portfolio import MarketScope

            try:
                mkt = MarketScope(market.upper())
            except ValueError:
                raise HTTPException(422, f"unknown market {market!r}")
        res = runtime.instrument_search.search(q, market=mkt, limit=limit)
        return {
            "query": q,
            "degraded": res.degraded,
            "source": res.source,
            "matches": [m.model_dump(mode="json") for m in res.matches],
        }

    @app.get(f"/{API_VERSION}/knowledge/search")
    def knowledge_search(
        q: str = Query(min_length=2, max_length=300),
        k: int = Query(default=5, ge=1, le=25),
    ) -> list[dict]:
        """Source-linked semantic research search (Loop.md §5.10: results
        always carry provenance; vector down => fail closed with 503)."""
        if runtime.knowledge is None:
            raise HTTPException(503, "knowledge store not configured")
        from swing_trader.knowledge import KnowledgeUnavailable
        from swing_trader.knowledge_pipeline import search_knowledge

        try:
            return search_knowledge(
                runtime.knowledge, runtime.knowledge_index, q, k=k
            )
        except KnowledgeUnavailable as exc:
            raise HTTPException(503, f"knowledge index unavailable: {exc}")

    @app.get(f"/{API_VERSION}/candidates")
    def candidates(
        mode: Optional[str] = Query(default=None),
        status: Optional[str] = Query(default=None),
    ) -> list[dict]:
        try:
            st = CandidateStatus(status) if status else None
        except ValueError:
            raise HTTPException(422, f"unknown status {status!r}")
        rows = runtime.ledger.get_candidates(mode=_mode(mode), status=st)
        return [c.model_dump(mode="json") for c in rows]

    @app.get(f"/{API_VERSION}/candidates/pending")
    def pending() -> list[dict]:
        svc = runtime.confirmation
        if svc is None:
            return []
        now = runtime.clock()
        return [
            {"candidate": c.model_dump(mode="json"), "version": v,
             "window_open": svc.in_window(now)}
            for c, v in svc.pending()
        ]

    @app.get(f"/{API_VERSION}/audit")
    def audit(
        candidate_id: Optional[str] = Query(default=None),
        mode: Optional[str] = Query(default=None),
    ) -> list[dict]:
        rows = runtime.ledger.get_audit(mode=_mode(mode), candidate_id=candidate_id)
        return [e.__dict__ | {"ts": e.ts.isoformat()} for e in rows]

    # ------------------------------------------------------------- writes

    @app.post(f"/{API_VERSION}/candidates/{{candidate_id}}/action")
    def act(
        candidate_id: str,
        body: ActionRequest,
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        svc = runtime.confirmation
        if svc is None:
            raise HTTPException(
                503, "confirmation service not active (no candidates published today)"
            )
        raw_surface = x_finance_surface or body.surface or "web"
        try:
            surface = Surface(raw_surface)
        except ValueError:
            raise HTTPException(422, f"unknown surface {raw_surface!r}")
        if surface is Surface.SYSTEM:
            raise HTTPException(403, "system surface cannot act (Loop.md §3)")
        result = svc.act(
            candidate_id,
            body.action,
            actor=body.actor,
            surface=surface,
            idempotency_key=body.idempotency_key,
            now_utc=runtime.clock(),
            edits=body.edits,
            expected_version=body.expected_version,
        )
        payload = {
            "ok": result.ok,
            "code": result.code.value,
            "message": result.message,
            "version": result.version,
            "candidate": result.candidate.model_dump(mode="json")
            if result.candidate else None,
        }
        return JSONResponse(payload, status_code=_RESULT_HTTP[result.code])

    # ------------------------------------------------- Portfolio (P0.9)

    def _need_portfolio():
        if runtime.portfolio is None:
            raise HTTPException(503, "portfolio journal not available")
        return runtime.portfolio

    def _need_drafts():
        if runtime.portfolio_drafts is None:
            raise HTTPException(503, "portfolio draft service not available")
        return runtime.portfolio_drafts

    def _resolve_surface(header: Optional[str], body_surface: Optional[str],
                         default: str = "web") -> str:
        raw = header or body_surface or default
        try:
            return Surface(raw).value
        except ValueError:
            raise HTTPException(422, f"unknown surface {raw!r}")

    def _holdings_payload(h) -> dict:
        return {
            "account_id": h.account_id,
            "as_of": h.as_of.isoformat() if h.as_of else None,
            "n_events": h.n_events,
            "holdings": [
                {"symbol": p.symbol,
                 "market": p.market.value if p.market else None,
                 "currency": p.currency, "qty": p.qty,
                 "avg_cost": p.avg_cost, "cost_basis_known": p.cost_basis_known}
                for p in h.holdings
            ],
            "cash": [
                {"currency": c.currency, "amount": c.amount, "known": c.known}
                for c in h.cash
            ],
        }

    def _audit_payload(e) -> dict:
        return {
            "ts": e.ts.isoformat(), "action": e.action, "actor": e.actor,
            "surface": e.surface, "account_id": e.account_id, "draft_id": e.draft_id,
            "event_id": e.event_id, "version": e.version,
            "idempotency_key": e.idempotency_key, "applied": e.applied,
            "detail": e.detail,
        }

    @app.get(f"/{API_VERSION}/portfolio/accounts")
    def portfolio_accounts() -> list[dict]:
        return [a.model_dump(mode="json") for a in _need_portfolio().list_accounts()]

    @app.get(f"/{API_VERSION}/portfolio/accounts/{{account_id}}")
    def portfolio_account(account_id: str) -> dict:
        a = _need_portfolio().get_account(account_id)
        if a is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        return a.model_dump(mode="json")

    @app.post(f"/{API_VERSION}/portfolio/accounts")
    def portfolio_account_create(
        body: PortfolioAccountCreate,
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        from swing_trader.portfolio import MarketScope
        from swing_trader.portfolio_journal import PortfolioAuditEvent

        pf = _need_portfolio()
        surface = _resolve_surface(x_finance_surface, None)
        try:
            MarketScope(body.market_scope.upper())
        except ValueError:
            raise HTTPException(422, f"unknown market {body.market_scope!r}")
        try:
            account = pf.create_account(
                name=body.name, market_scope=body.market_scope.upper(),
                base_currency=body.base_currency, provider=body.provider,
                account_type=body.account_type, include_in_risk=body.include_in_risk,
                note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        pf.record_audit(PortfolioAuditEvent(
            ts=runtime.clock(), action="account_create", actor=body.actor,
            surface=surface, account_id=account.id, detail=account.name))
        return JSONResponse(account.model_dump(mode="json"), status_code=201)

    @app.post(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/update")
    def portfolio_account_update(
        account_id: str, body: PortfolioAccountUpdate,
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        from swing_trader.portfolio_journal import PortfolioAuditEvent

        pf = _need_portfolio()
        surface = _resolve_surface(x_finance_surface, None)
        try:
            account = pf.update_account(
                account_id, name=body.name, include_in_risk=body.include_in_risk,
                note=body.note, account_type=body.account_type, now=runtime.clock())
        except ValueError as exc:
            raise HTTPException(404, str(exc))
        pf.record_audit(PortfolioAuditEvent(
            ts=runtime.clock(), action="account_update", actor=body.actor,
            surface=surface, account_id=account_id, detail="config updated"))
        return account.model_dump(mode="json")

    @app.get(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/holdings")
    def portfolio_holdings(account_id: str) -> dict:
        pf = _need_portfolio()
        if pf.get_account(account_id) is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        return _holdings_payload(pf.holdings(account_id))

    @app.get(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/events")
    def portfolio_events(account_id: str,
                         symbol: Optional[str] = Query(default=None)) -> list[dict]:
        return [e.model_dump(mode="json")
                for e in _need_portfolio().get_events(account_id, symbol)]

    @app.get(f"/{API_VERSION}/portfolio/audit")
    def portfolio_audit(account_id: Optional[str] = Query(default=None),
                        draft_id: Optional[str] = Query(default=None)) -> list[dict]:
        return [_audit_payload(e)
                for e in _need_portfolio().get_audit(account_id, draft_id)]

    @app.get(f"/{API_VERSION}/portfolio/drafts")
    def portfolio_drafts_list(account_id: Optional[str] = Query(default=None),
                              status: Optional[str] = Query(default=None)) -> list[dict]:
        return [d.model_dump(mode="json")
                for d in _need_drafts().list_drafts(account_id, status)]

    @app.get(f"/{API_VERSION}/portfolio/drafts/{{draft_id}}")
    def portfolio_draft_get(draft_id: str) -> dict:
        d = _need_drafts().get_draft(draft_id)
        if d is None:
            raise HTTPException(404, f"unknown draft {draft_id!r}")
        return d.model_dump(mode="json")

    @app.post(f"/{API_VERSION}/portfolio/drafts")
    def portfolio_draft_create(
        body: PortfolioDraftCreate,
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        svc = _need_drafts()
        surface = _resolve_surface(x_finance_surface, body.surface, default="system")
        try:
            draft = svc.create_draft(
                account_id=body.account_id, event_type=body.event_type,
                symbol=body.symbol, market=body.market, currency=body.currency,
                qty=body.qty, price=body.price, commission=body.commission,
                amount=body.amount, occurred_at=body.occurred_at, source=body.source,
                external_id=body.external_id, reverses_event_id=body.reverses_event_id,
                note=body.note, original_text=body.original_text,
                ambiguities=body.ambiguities,
                created_by=body.created_by, created_surface=surface)
        except Exception as exc:  # noqa: BLE001 — surface bad draft input as 422
            raise HTTPException(422, f"invalid draft: {str(exc)[:200]}")
        return JSONResponse(draft.model_dump(mode="json"), status_code=201)

    @app.post(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/close-draft")
    def portfolio_close_draft(
        account_id: str, symbol: str = Query(min_length=1, max_length=24),
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        svc = _need_drafts()
        surface = _resolve_surface(x_finance_surface, None, default="system")
        if _need_portfolio().get_account(account_id) is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        draft = svc.propose_close(account_id=account_id, symbol=symbol,
                                  created_surface=surface)
        return JSONResponse(draft.model_dump(mode="json"), status_code=201)

    @app.post(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/correct-draft")
    def portfolio_correct_draft(
        account_id: str, event_id: str = Query(min_length=1),
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        """Draft the UNDO of a prior event (append-only 'delete' via a
        compensating CORRECTION). Still requires human confirmation."""
        svc = _need_drafts()
        surface = _resolve_surface(x_finance_surface, None, default="system")
        if _need_portfolio().get_account(account_id) is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        draft = svc.propose_correction(account_id=account_id, event_id=event_id,
                                       created_surface=surface)
        if draft is None:
            raise HTTPException(404, f"unknown event {event_id!r} in this account")
        return JSONResponse(draft.model_dump(mode="json"), status_code=201)

    @app.post(f"/{API_VERSION}/portfolio/drafts/{{draft_id}}/action")
    def portfolio_draft_action(
        draft_id: str, body: PortfolioDraftAction,
        x_finance_surface: Optional[str] = Header(default=None),
    ):
        svc = _need_drafts()
        surface = _resolve_surface(x_finance_surface, body.surface)
        if body.action == "confirm":
            result = svc.confirm_draft(
                draft_id, actor=body.actor, surface=surface,
                idempotency_key=body.idempotency_key,
                expected_version=body.expected_version, now=runtime.clock())
        elif body.action == "edit":
            result = svc.edit_draft(
                draft_id, actor=body.actor, surface=surface,
                edits=body.edits or {}, expected_version=body.expected_version)
        else:  # reject
            result = svc.reject_draft(
                draft_id, actor=body.actor, surface=surface,
                idempotency_key=body.idempotency_key)
        payload = {
            "ok": result.ok, "code": result.code.value, "message": result.message,
            "version": result.version,
            "draft": result.draft.model_dump(mode="json") if result.draft else None,
            "event": result.event.model_dump(mode="json") if result.event else None,
        }
        return JSONResponse(payload, status_code=_DRAFT_HTTP[result.code])

    @app.get(f"/{API_VERSION}/portfolio/aggregate")
    def portfolio_aggregate(include_in_risk_only: bool = Query(default=False)) -> dict:
        pf = _need_portfolio()
        agg = pf.aggregate(include_in_risk_only=include_in_risk_only)
        return {
            "accounts": agg.accounts,
            "as_of": agg.as_of.isoformat() if agg.as_of else None,
            "holdings": [
                {"symbol": h.symbol,
                 "market": h.market.value if h.market else None,
                 "currency": h.currency, "qty": h.qty, "avg_cost": h.avg_cost,
                 "cost_basis_known": h.cost_basis_known, "accounts": h.accounts}
                for h in agg.holdings
            ],
            "cash": [
                {"currency": c.currency, "amount": c.amount, "known": c.known}
                for c in agg.cash
            ],
        }

    @app.get(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/reconcile")
    def portfolio_reconcile(account_id: str) -> dict:
        from swing_trader.portfolio_reconcile import reconcile_portfolio_account

        pf = _need_portfolio()
        account = pf.get_account(account_id)
        if account is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        # Phase 0.9: no IBKR snapshot per portfolio account yet → the account's
        # own record is authoritative (manual/imported); wired once IBKR lands.
        res = reconcile_portfolio_account(account, pf.holdings(account_id),
                                          broker_positions=None, now=runtime.clock())
        return {
            "account_id": res.account_id, "ok": res.ok, "authority": res.authority,
            "summary": res.summary(), "note": res.note,
            "as_of": res.as_of.isoformat() if res.as_of else None,
            "drifts": [
                {"symbol": d.symbol, "portfolio_qty": d.portfolio_qty,
                 "broker_qty": d.broker_qty}
                for d in res.drifts
            ],
        }

    @app.post(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/import/preview")
    def portfolio_import_preview(account_id: str, body: PortfolioImportRequest) -> dict:
        from swing_trader.portfolio_csv import parse_csv

        pf = _need_portfolio()
        if pf.get_account(account_id) is None:
            raise HTTPException(404, f"unknown account {account_id!r}")
        pv = parse_csv(body.csv, account_id, pf)
        return {
            "header_error": pv.header_error,
            "n_valid": pv.n_valid, "n_invalid": pv.n_invalid,
            "n_duplicate": pv.n_duplicate, "committable": pv.committable,
            "rows": [
                {"line": r.line, "duplicate": r.duplicate, "errors": r.errors,
                 "ok": r.ok,
                 "event_type": r.fields.get("event_type").value if r.fields.get("event_type") else None,
                 "symbol": r.fields.get("symbol"),
                 "qty": r.fields.get("qty"), "price": r.fields.get("price"),
                 "amount": r.fields.get("amount")}
                for r in pv.rows
            ],
        }

    @app.post(f"/{API_VERSION}/portfolio/accounts/{{account_id}}/import/commit")
    def portfolio_import_commit(
        account_id: str, body: PortfolioImportRequest,
        x_finance_surface: Optional[str] = Header(default=None),
    ) -> dict:
        from swing_trader.portfolio_csv import commit_csv

        _need_portfolio()
        surface = _resolve_surface(x_finance_surface, None)
        if not body.actor:
            raise HTTPException(422, "actor is required to commit an import")
        try:
            res = commit_csv(runtime.portfolio, account_id, body.csv,
                             actor=body.actor, surface=surface)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return {
            "n_committed": res.n_committed, "n_duplicate": res.n_duplicate,
            "n_skipped": res.n_skipped, "event_ids": res.event_ids,
        }

    return app
