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
from swing_trader.reporter import build_account_view
from swing_trader.schemas import CandidateStatus, Mode

logger = get_logger(__name__)

__all__ = ["FinanceRuntime", "create_app", "DEFAULT_SERVICE_PORT"]

DEFAULT_SERVICE_PORT = 9319

API_VERSION = "v1"


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
    latest_reports: dict = field(default_factory=dict)  # kind -> text
    latest_brief: dict = field(default_factory=dict)  # ResearchBrief dump
    knowledge: Any = None  # FinanceKnowledge (Phase 0.5)
    knowledge_index: Any = None  # KnowledgeIndex | None (fail-closed)
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
        return {
            "status": "ok",
            "mode": runtime.mode.value,
            "loop_attached": runtime.broker is not None,
            "breaker": breaker,
            "ts": now.isoformat(),
        }

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
    def research_brief() -> dict:
        """Investment Research brief (Loop.md Phase 0.5). Falls back to an
        on-demand DEGRADED brief (ledger-only, explicit freshness warnings)
        when the daily loop has not produced one yet."""
        if runtime.latest_brief:
            return runtime.latest_brief
        from swing_trader.brief import build_research_brief

        brief = build_research_brief(
            runtime.ledger, runtime.mode, now=runtime.clock()
        )
        return brief.model_dump(mode="json")

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

    return app
