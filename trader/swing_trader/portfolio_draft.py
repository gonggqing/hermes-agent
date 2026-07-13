"""PortfolioDraftService — the human-confirmation gate for portfolio events.

Free-form conversation can NEVER mutate holdings (Loop.md §7 P0.9, boundary
#4). Hermes parses a user statement into a :class:`PortfolioDraft`; only an
authenticated HUMAN confirmation on Desktop / Web / Telegram turns a draft into
an append-only :class:`PortfolioEvent`. This service is the single writer of
that transition and mirrors the trading-side :class:`ConfirmationService`:

- the LLM/system may create and edit drafts but MUST NOT finalize one — a
  ``system``/``llm`` actor or the ``system`` surface is refused (and audited);
- a draft with missing/ambiguous fields cannot confirm (clarify, never guess);
- idempotent confirm (replay-safe), version/stale checks, and EVERY attempt —
  applied or refused — is written to the append-only portfolio audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Callable, Optional

from swing_trader.confirmation import Surface
from swing_trader.log import get_logger
from swing_trader.portfolio import (
    SYSTEM_ACTORS,
    DraftStatus,
    EventSource,
    EventType,
    MarketScope,
    PortfolioDraft,
    PortfolioEvent,
    draft_missing_fields,
    utcnow,
)
from swing_trader.portfolio_journal import PortfolioAuditEvent, PortfolioJournal

logger = get_logger(__name__)

__all__ = ["DraftResult", "DraftResultCode", "PortfolioDraftService"]

#: Draft fields a human may edit before confirming (never actor/version/status).
EDITABLE_FIELDS: frozenset[str] = frozenset({
    "account_id", "symbol", "market", "currency", "qty", "price",
    "commission", "amount", "occurred_at", "settlement_date", "note",
    "event_type", "external_id",
})


class DraftResultCode(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"
    INCOMPLETE = "incomplete"  # missing/ambiguous fields — clarify first
    NOT_HUMAN = "not_human"  # system/LLM cannot finalize (boundary #4)
    UNKNOWN_DRAFT = "unknown_draft"
    TERMINAL = "terminal"  # already confirmed/rejected/expired
    VERSION_CONFLICT = "version_conflict"
    INVALID_EDIT = "invalid_edit"


@dataclass
class DraftResult:
    ok: bool
    code: DraftResultCode
    message: str
    draft: Optional[PortfolioDraft] = None
    event: Optional[PortfolioEvent] = None
    version: int = 0


class PortfolioDraftService:
    def __init__(
        self,
        journal: PortfolioJournal,
        *,
        clock: Callable[[], datetime] = utcnow,
        draft_ttl_hours: float = 48.0,
    ) -> None:
        self._journal = journal
        self._clock = clock
        self._ttl = timedelta(hours=draft_ttl_hours)

    # -------------------------------------------------------- create/read

    def create_draft(
        self,
        *,
        account_id: Optional[str] = None,
        event_type: EventType | str = EventType.BUY,
        symbol: Optional[str] = None,
        market: Optional[MarketScope | str] = None,
        currency: Optional[str] = None,
        qty: Optional[float] = None,
        price: Optional[float] = None,
        commission: Optional[float] = None,
        amount: Optional[float] = None,
        occurred_at: Optional[datetime] = None,
        source: EventSource | str = EventSource.MANUAL,
        external_id: Optional[str] = None,
        note: str = "",
        original_text: str = "",
        ambiguities: Optional[list[str]] = None,
        created_by: str = "hermes",
        created_surface: str = "system",
    ) -> PortfolioDraft:
        """Build + persist a draft, flagging what must be clarified. Never
        guesses — unresolved fields land in ``missing``/``ambiguities``."""
        now = self._clock()
        draft = PortfolioDraft(
            account_id=account_id,
            event_type=EventType(event_type),
            symbol=symbol,
            market=MarketScope(market) if market is not None else None,
            currency=currency,
            qty=qty,
            price=price,
            commission=commission,
            amount=amount,
            occurred_at=occurred_at,
            source=EventSource(source),
            external_id=external_id,
            note=note,
            original_text=original_text,
            ambiguities=list(ambiguities or []),
            created_by=created_by,
            created_surface=created_surface,
            created_at=now,
            updated_at=now,
        )
        self._recompute_gaps(draft)
        self._journal.save_draft(draft)
        self._audit(draft, action="draft", actor=created_by, surface=created_surface,
                    applied=True, detail="drafted")
        return draft

    def propose_close(
        self,
        *,
        account_id: str,
        symbol: str,
        occurred_at: Optional[datetime] = None,
        original_text: str = "",
        created_by: str = "hermes",
        created_surface: str = "system",
    ) -> PortfolioDraft:
        """Draft a full exit ("cleared X") — proposes the SELL qty from CURRENT
        holdings (still requires confirmation). If nothing is held, flags an
        ambiguity rather than guessing. The draft is a proposed FILL; an
        unfilled order does not reduce holdings (Loop.md P0.9 conversation)."""
        held = next(
            (h for h in self._journal.holdings(account_id).holdings if h.symbol == symbol.upper()),
            None,
        )
        ambiguities = ["confirm this is a completed trade (a placed-but-unfilled "
                       "order does not change holdings)"]
        qty = None
        market = None
        currency = None
        if held is None:
            ambiguities.append(f"no open position in {symbol.upper()} to close")
        else:
            qty = held.qty
            market = held.market
            currency = held.currency
        return self.create_draft(
            account_id=account_id, event_type=EventType.SELL, symbol=symbol,
            market=market, currency=currency, qty=qty,
            occurred_at=occurred_at or self._clock(),
            original_text=original_text, ambiguities=ambiguities,
            created_by=created_by, created_surface=created_surface,
        )

    def get_draft(self, draft_id: str) -> Optional[PortfolioDraft]:
        return self._journal.get_draft(draft_id)

    def list_drafts(self, account_id=None, status=None) -> list[PortfolioDraft]:
        return self._journal.list_drafts(account_id, status)

    # -------------------------------------------------------------- edit

    def edit_draft(
        self, draft_id: str, *, actor: str, surface: str, edits: dict,
        expected_version: Optional[int] = None,
    ) -> DraftResult:
        draft = self._journal.get_draft(draft_id)
        if draft is None:
            return DraftResult(False, DraftResultCode.UNKNOWN_DRAFT, "unknown draft")
        if draft.status is not DraftStatus.DRAFT:
            return self._refuse(draft, DraftResultCode.TERMINAL, actor, surface,
                                action="edit", detail=f"status {draft.status.value}")
        bad = set(edits) - EDITABLE_FIELDS
        if bad:
            return self._refuse(draft, DraftResultCode.INVALID_EDIT, actor, surface,
                                action="edit", detail=f"non-editable: {sorted(bad)}")
        if expected_version is not None and expected_version != draft.version:
            return self._refuse(draft, DraftResultCode.VERSION_CONFLICT, actor, surface,
                                action="edit", detail=f"expected v{expected_version}")
        try:
            # Full re-validation (model_copy(update=) skips validators): an
            # invalid edit value must be rejected, not silently stored.
            updated = PortfolioDraft.model_validate({
                **draft.model_dump(), **edits,
                "version": draft.version + 1, "updated_at": self._clock(),
            })
        except Exception as exc:  # noqa: BLE001 — invalid edit value
            return self._refuse(draft, DraftResultCode.INVALID_EDIT, actor, surface,
                                action="edit", detail=str(exc)[:160])
        self._recompute_gaps(updated)
        self._journal.save_draft(updated)
        self._audit(updated, action="edit", actor=actor, surface=surface, applied=True)
        return DraftResult(True, DraftResultCode.APPLIED, "edited", draft=updated,
                           version=updated.version)

    def reject_draft(self, draft_id: str, *, actor: str, surface: str,
                     idempotency_key: str = "") -> DraftResult:
        draft = self._journal.get_draft(draft_id)
        if draft is None:
            return DraftResult(False, DraftResultCode.UNKNOWN_DRAFT, "unknown draft")
        if draft.status is not DraftStatus.DRAFT:
            return self._refuse(draft, DraftResultCode.TERMINAL, actor, surface,
                                action="reject", detail=f"status {draft.status.value}")
        updated = draft.model_copy(update={
            "status": DraftStatus.REJECTED, "updated_at": self._clock(),
        })
        self._journal.save_draft(updated)
        self._audit(updated, action="reject", actor=actor, surface=surface,
                    applied=True, idempotency_key=idempotency_key)
        return DraftResult(True, DraftResultCode.APPLIED, "rejected", draft=updated,
                           version=updated.version)

    # ----------------------------------------------------------- confirm

    def confirm_draft(
        self, draft_id: str, *, actor: str, surface: str, idempotency_key: str,
        expected_version: Optional[int] = None, now: Optional[datetime] = None,
    ) -> DraftResult:
        """Authenticated-human confirmation → append-only event. The ONLY path
        that mutates holdings. Refuses system/LLM finalization (boundary #4)."""
        now = now or self._clock()

        # (0) Only an authenticated human may finalize (boundary #4).
        if surface == Surface.SYSTEM.value or actor.strip().lower() in SYSTEM_ACTORS:
            draft = self._journal.get_draft(draft_id)
            return self._refuse(draft, DraftResultCode.NOT_HUMAN, actor, surface,
                                action="confirm",
                                detail="only an authenticated human may confirm",
                                idempotency_key=idempotency_key)

        # (1) Idempotency replay (survives restart via the audit trail).
        for e in self._journal.get_audit(idempotency_key=idempotency_key):
            if e.applied and e.action == "confirm":
                existing = self._journal.get_draft(e.draft_id)
                event = self._journal.get_event(e.event_id) if e.event_id else None
                return DraftResult(True, DraftResultCode.REPLAYED, "already confirmed",
                                   draft=existing, event=event,
                                   version=existing.version if existing else 0)

        draft = self._journal.get_draft(draft_id)
        if draft is None:
            return DraftResult(False, DraftResultCode.UNKNOWN_DRAFT, "unknown draft")
        if draft.status is not DraftStatus.DRAFT:
            return self._refuse(draft, DraftResultCode.TERMINAL, actor, surface,
                                action="confirm", detail=f"status {draft.status.value}",
                                idempotency_key=idempotency_key)
        if expected_version is not None and expected_version != draft.version:
            return self._refuse(draft, DraftResultCode.VERSION_CONFLICT, actor, surface,
                                action="confirm", detail=f"expected v{expected_version}",
                                idempotency_key=idempotency_key)

        self._recompute_gaps(draft)
        if draft.needs_clarification:
            self._journal.save_draft(draft)
            return self._refuse(
                draft, DraftResultCode.INCOMPLETE, actor, surface, action="confirm",
                detail="; ".join(draft.missing + draft.ambiguities),
                idempotency_key=idempotency_key)

        event = PortfolioEvent(
            account_id=draft.account_id,
            event_type=draft.event_type,
            symbol=draft.symbol,
            market=draft.market,
            currency=draft.currency,
            qty=draft.qty if draft.qty is not None else 0.0,
            price=draft.price,
            commission=draft.commission,
            amount=draft.amount,
            occurred_at=draft.occurred_at,
            settlement_date=draft.settlement_date,
            source=draft.source,
            external_id=draft.external_id,
            idempotency_key=idempotency_key,
            actor=actor,
            surface=surface,
            note=draft.note,
            created_at=now,
        )
        stored, _created = self._journal.append_event(event)
        confirmed = draft.model_copy(update={
            "status": DraftStatus.CONFIRMED, "confirmed_by": actor,
            "confirmed_at": now, "updated_at": now,
        })
        self._journal.save_draft(confirmed)
        self._audit(confirmed, action="confirm", actor=actor, surface=surface,
                    applied=True, idempotency_key=idempotency_key, event_id=stored.id)
        return DraftResult(True, DraftResultCode.APPLIED, "confirmed", draft=confirmed,
                           event=stored, version=confirmed.version)

    def expire_drafts(self, now: Optional[datetime] = None) -> int:
        now = now or self._clock()
        n = 0
        for draft in self._journal.list_drafts(status=DraftStatus.DRAFT):
            if now - draft.created_at >= self._ttl:
                expired = draft.model_copy(update={
                    "status": DraftStatus.EXPIRED, "updated_at": now,
                })
                self._journal.save_draft(expired)
                self._audit(expired, action="expire", actor="system", surface="system",
                            applied=True, detail="draft ttl elapsed")
                n += 1
        return n

    # -------------------------------------------------------------- internals

    def _recompute_gaps(self, draft: PortfolioDraft) -> None:
        missing = draft_missing_fields(draft)
        # keep only caller ambiguities not tied to hard-missing fields
        ambig = list(draft.ambiguities)
        if draft.account_id is not None and self._journal.get_account(draft.account_id) is None:
            missing.append("account")
            ambig.append(f"account {draft.account_id} not found")
        # de-dup while preserving order
        draft.missing = list(dict.fromkeys(missing))
        draft.ambiguities = list(dict.fromkeys(ambig))

    def _audit(self, draft: Optional[PortfolioDraft], *, action: str, actor: str,
               surface: str, applied: bool, detail: str = "",
               idempotency_key: str = "", event_id: str = "") -> None:
        self._journal.record_audit(PortfolioAuditEvent(
            ts=self._clock(),
            action=action if applied else f"refused:{action}",
            actor=actor,
            surface=surface,
            account_id=(draft.account_id or "") if draft else "",
            draft_id=draft.id if draft else "",
            event_id=event_id,
            version=draft.version if draft else 1,
            idempotency_key=idempotency_key,
            applied=applied,
            detail=detail,
        ))

    def _refuse(self, draft: Optional[PortfolioDraft], code: DraftResultCode,
                actor: str, surface: str, *, action: str, detail: str,
                idempotency_key: str = "") -> DraftResult:
        self._audit(draft, action=action, actor=actor, surface=surface,
                    applied=False, detail=f"{code.value}: {detail}",
                    idempotency_key=idempotency_key)
        return DraftResult(False, code, detail, draft=draft,
                           version=draft.version if draft else 0)
