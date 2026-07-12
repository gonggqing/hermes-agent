"""Confirmation service — ONE server-authoritative candidate state machine
shared by Desktop, Web, and Telegram (Loop.md §5.6, §3 authority guardrails).

Authority model (Loop.md §3):

- An LLM may research, propose, and query finance state, but it may NEVER
  transition a candidate to APPROVED. ``act()`` is invoked only by surface
  adapters relaying an authenticated HUMAN action; the actor identity,
  surface (``desktop|web|telegram``), candidate version, idempotency key,
  and timestamp of every attempt — applied OR refused — are appended to the
  immutable ledger audit trail.
- Idempotency: the same (candidate_id, idempotency_key) is applied at most
  once; replays return the recorded outcome without re-transitioning, so two
  surfaces (or a retried HTTP request) can never double-approve → the
  ExecutionEngine can never double-place.
- Every EDIT is re-validated: first structurally (pydantic CandidateOrder
  invariants — protection cannot be stripped), then through the caller-
  provided ``revalidate`` hook (RiskEngine), before it can count as
  human-approved. ExecutionEngine re-validates once more before broker
  submission (Loop.md §5.6/§5.7).
- The 11:30→12:30 ET window is enforced SERVER-SIDE with zoneinfo (DST
  correct); after cutoff every action is refused and pending candidates
  expire. Surfaces render state; they hold none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from swing_trader.ledger import AuditEvent, Ledger
from swing_trader.log import get_logger
from swing_trader.schemas import CandidateOrder, CandidateStatus, Mode

logger = get_logger(__name__)

__all__ = [
    "ActResult",
    "ConfirmationService",
    "ResultCode",
    "Surface",
]

# Candidate fields a human may edit from any surface. Everything else —
# especially symbol/side/order_type — requires rejecting and re-proposing.
EDITABLE_FIELDS: frozenset[str] = frozenset({"qty", "limit", "sl", "tp", "stop"})

SYSTEM_ACTOR = "system"


class Surface(str, Enum):
    DESKTOP = "desktop"
    WEB = "web"
    TELEGRAM = "telegram"
    SYSTEM = "system"


class ResultCode(str, Enum):
    APPLIED = "applied"
    REPLAYED = "replayed"  # idempotent replay of an earlier action
    WINDOW_CLOSED = "window_closed"
    TERMINAL = "terminal"  # candidate already settled
    VERSION_CONFLICT = "version_conflict"  # stale card (edited elsewhere)
    UNKNOWN_CANDIDATE = "unknown_candidate"
    INVALID_EDIT = "invalid_edit"
    INVALID_ACTION = "invalid_action"


@dataclass
class ActResult:
    ok: bool
    code: ResultCode
    message: str = ""
    candidate: Optional[CandidateOrder] = None
    version: int = 0


@dataclass
class _Entry:
    candidate: CandidateOrder
    version: int = 1


@dataclass
class FinalizedDecisions:
    approved: list[CandidateOrder] = field(default_factory=list)
    edited: list[CandidateOrder] = field(default_factory=list)
    rejected: list[CandidateOrder] = field(default_factory=list)
    expired: list[CandidateOrder] = field(default_factory=list)

    @property
    def human_approved(self) -> list[CandidateOrder]:
        """APPROVED + EDITED both count as human-approved (Loop.md §4)."""
        return [*self.approved, *self.edited]


class ConfirmationService:
    """The single writer for candidate confirmation state (Loop.md §5.6)."""

    def __init__(
        self,
        ledger: Ledger,
        mode: Mode = Mode.PAPER,
        push_time_et: time = time(11, 30),
        cutoff_et: time = time(12, 30),
        market_tz: str = "America/New_York",
        revalidate: Optional[Callable[[CandidateOrder], tuple[bool, str]]] = None,
    ) -> None:
        self._ledger = ledger
        self._mode = mode
        self._push_time = push_time_et
        self._cutoff = cutoff_et
        self._tz = ZoneInfo(market_tz)
        self._revalidate = revalidate
        self._entries: dict[str, _Entry] = {}
        self._results: dict[tuple[str, str], ActResult] = {}
        self._final = FinalizedDecisions()

    # ------------------------------------------------------------- window

    def in_window(self, now_utc: datetime) -> bool:
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        now_t = now_utc.astimezone(self._tz).time()
        return self._push_time <= now_t < self._cutoff

    def past_cutoff(self, now_utc: datetime) -> bool:
        if now_utc.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        return now_utc.astimezone(self._tz).time() >= self._cutoff

    # ------------------------------------------------------------- publish

    def publish(
        self, candidates: list[CandidateOrder], now_utc: datetime
    ) -> list[CandidateOrder]:
        """Register RISK_APPROVED candidates for human confirmation.

        Refuses outside the 11:30→12:30 ET window (Loop.md §4). Publishing
        is a SYSTEM action (the loop), not an approval.
        """
        if not self.in_window(now_utc):
            logger.warning("publish refused outside confirmation window")
            return []
        published: list[CandidateOrder] = []
        for cand in candidates:
            if cand.status is not CandidateStatus.RISK_APPROVED:
                logger.warning(
                    "refusing to publish non-risk-approved candidate",
                    extra={"symbol": cand.symbol, "status": cand.status.value},
                )
                continue
            pushed = cand.model_copy(update={"status": CandidateStatus.PUSHED})
            self._entries[pushed.id] = _Entry(candidate=pushed, version=1)
            self._ledger.update_candidate(pushed.id, CandidateStatus.PUSHED)
            self._audit(
                now_utc, pushed.id, "publish", SYSTEM_ACTOR, Surface.SYSTEM,
                version=1, prev=cand.status, new=CandidateStatus.PUSHED,
            )
            published.append(pushed)
        return published

    # ------------------------------------------------------------- queries

    def pending(self) -> list[tuple[CandidateOrder, int]]:
        return [(e.candidate, e.version) for e in self._entries.values()
                if e.candidate.status is CandidateStatus.PUSHED]

    def get(self, candidate_id: str) -> Optional[tuple[CandidateOrder, int]]:
        entry = self._entries.get(candidate_id)
        if entry is None:
            return None
        return (entry.candidate, entry.version)

    def finalized(self) -> FinalizedDecisions:
        return self._final

    # ------------------------------------------------------------- actions

    def act(
        self,
        candidate_id: str,
        action: str,
        actor: str,
        surface: Surface | str,
        idempotency_key: str,
        now_utc: datetime,
        edits: Optional[dict] = None,
        expected_version: Optional[int] = None,
    ) -> ActResult:
        """Apply one authenticated HUMAN action (approve | edit | reject).

        Every attempt — applied or refused — lands in the audit trail.
        """
        surface = Surface(surface)
        if surface is Surface.SYSTEM:
            # Defense in depth: the system may publish/expire, never approve.
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.INVALID_ACTION,
                "surface 'system' cannot perform human actions (Loop.md §3)",
            )

        # Idempotent replay (in-memory first, ledger check for restarts).
        key = (candidate_id, idempotency_key)
        if key in self._results:
            prior = self._results[key]
            return ActResult(ok=prior.ok, code=ResultCode.REPLAYED,
                             message=f"replay of earlier {action}",
                             candidate=prior.candidate, version=prior.version)
        if idempotency_key and self._ledger.get_audit(
            mode=self._mode, candidate_id=candidate_id,
            idempotency_key=idempotency_key,
        ):
            entry = self._entries.get(candidate_id)
            return ActResult(
                ok=True, code=ResultCode.REPLAYED,
                message="replay (found in audit trail)",
                candidate=entry.candidate if entry else None,
                version=entry.version if entry else 0,
            )

        entry = self._entries.get(candidate_id)
        if entry is None:
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.UNKNOWN_CANDIDATE, "unknown candidate id",
            )
        if entry.candidate.status is not CandidateStatus.PUSHED:
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.TERMINAL,
                f"already settled: {entry.candidate.status.value}",
                version=entry.version,
            )
        if not self.in_window(now_utc):
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.WINDOW_CLOSED,
                "confirmation window is closed (11:30-12:30 ET)",
                version=entry.version,
            )
        if expected_version is not None and expected_version != entry.version:
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.VERSION_CONFLICT,
                f"stale card: version {expected_version} != {entry.version}",
                version=entry.version,
            )

        if action == "approve":
            result = self._transition(
                now_utc, entry, CandidateStatus.APPROVED, action, actor,
                surface, idempotency_key,
            )
        elif action == "reject":
            result = self._transition(
                now_utc, entry, CandidateStatus.REJECTED, action, actor,
                surface, idempotency_key,
            )
        elif action == "edit":
            result = self._edit(
                now_utc, entry, edits or {}, actor, surface, idempotency_key
            )
        else:
            return self._refuse(
                now_utc, candidate_id, action, actor, surface, idempotency_key,
                ResultCode.INVALID_ACTION, f"unknown action {action!r}",
                version=entry.version,
            )

        if result.ok:
            self._results[key] = result
        return result

    def expire(self, now_utc: datetime) -> list[CandidateOrder]:
        """Server-side expiry: after cutoff, every pending candidate expires."""
        if not self.past_cutoff(now_utc):
            return []
        expired: list[CandidateOrder] = []
        for entry in self._entries.values():
            if entry.candidate.status is not CandidateStatus.PUSHED:
                continue
            prev = entry.candidate.status
            entry.candidate = entry.candidate.model_copy(
                update={"status": CandidateStatus.EXPIRED}
            )
            self._ledger.update_candidate(
                entry.candidate.id, CandidateStatus.EXPIRED,
                risk_note="expired: confirmation window passed",
            )
            self._audit(
                now_utc, entry.candidate.id, "expire", SYSTEM_ACTOR,
                Surface.SYSTEM, version=entry.version,
                prev=prev, new=CandidateStatus.EXPIRED,
            )
            self._final.expired.append(entry.candidate)
            expired.append(entry.candidate)
        return expired

    # ------------------------------------------------------------ internals

    def _transition(
        self,
        now_utc: datetime,
        entry: _Entry,
        new_status: CandidateStatus,
        action: str,
        actor: str,
        surface: Surface,
        idempotency_key: str,
    ) -> ActResult:
        prev = entry.candidate.status
        entry.candidate = entry.candidate.model_copy(update={"status": new_status})
        self._ledger.update_candidate(entry.candidate.id, new_status)
        self._audit(
            now_utc, entry.candidate.id, action, actor, surface,
            version=entry.version, prev=prev, new=new_status,
            key=idempotency_key,
        )
        bucket = {
            CandidateStatus.APPROVED: self._final.approved,
            CandidateStatus.EDITED: self._final.edited,
            CandidateStatus.REJECTED: self._final.rejected,
        }[new_status]
        bucket.append(entry.candidate)
        logger.info(
            "candidate settled",
            extra={
                "candidate_id": entry.candidate.id,
                "symbol": entry.candidate.symbol,
                "action": action,
                "actor": actor,
                "surface": surface.value,
            },
        )
        return ActResult(ok=True, code=ResultCode.APPLIED,
                         message=f"{action} applied",
                         candidate=entry.candidate, version=entry.version)

    def _edit(
        self,
        now_utc: datetime,
        entry: _Entry,
        edits: dict,
        actor: str,
        surface: Surface,
        idempotency_key: str,
    ) -> ActResult:
        if not edits:
            return self._refuse(
                now_utc, entry.candidate.id, "edit", actor, surface,
                idempotency_key, ResultCode.INVALID_EDIT, "no edits supplied",
                version=entry.version,
            )
        illegal = set(edits) - EDITABLE_FIELDS
        if illegal:
            return self._refuse(
                now_utc, entry.candidate.id, "edit", actor, surface,
                idempotency_key, ResultCode.INVALID_EDIT,
                f"non-editable fields: {sorted(illegal)}",
                version=entry.version,
            )
        # Structural re-validation: full pydantic pass, so an edit can never
        # strip protection or zero the quantity (Loop.md §4 invariants).
        try:
            edited = CandidateOrder.model_validate(
                {**entry.candidate.model_dump(), **edits,
                 "status": CandidateStatus.EDITED.value}
            )
        except Exception as exc:  # pydantic.ValidationError
            return self._refuse(
                now_utc, entry.candidate.id, "edit", actor, surface,
                idempotency_key, ResultCode.INVALID_EDIT,
                f"validation failed: {exc}", version=entry.version,
            )
        # Risk re-validation (Loop.md §5.6: every edit re-validated).
        if self._revalidate is not None:
            ok, reason = self._revalidate(edited)
            if not ok:
                return self._refuse(
                    now_utc, entry.candidate.id, "edit", actor, surface,
                    idempotency_key, ResultCode.INVALID_EDIT,
                    f"risk re-validation failed: {reason}",
                    version=entry.version,
                )
        prev = entry.candidate.status
        entry.candidate = edited
        entry.version += 1
        self._ledger.update_candidate(
            edited.id, CandidateStatus.EDITED,
            risk_note=f"human edit via {surface.value}: {edits}",
        )
        self._audit(
            now_utc, edited.id, "edit", actor, surface,
            version=entry.version, prev=prev, new=CandidateStatus.EDITED,
            key=idempotency_key, detail=str(sorted(edits.items())),
        )
        self._final.edited.append(edited)
        return ActResult(ok=True, code=ResultCode.APPLIED,
                         message="edit applied (counts as human approval)",
                         candidate=edited, version=entry.version)

    def _refuse(
        self,
        now_utc: datetime,
        candidate_id: str,
        action: str,
        actor: str,
        surface: Surface,
        idempotency_key: str,
        code: ResultCode,
        message: str,
        version: int = 0,
    ) -> ActResult:
        entry = self._entries.get(candidate_id)
        self._audit(
            now_utc, candidate_id, action, actor, surface,
            version=version, key=idempotency_key, applied=False,
            detail=f"{code.value}: {message}",
            prev=entry.candidate.status if entry else None,
            new=entry.candidate.status if entry else None,
        )
        logger.warning(
            "confirmation action refused",
            extra={"candidate_id": candidate_id, "action": action,
                   "actor": actor, "surface": surface.value,
                   "code": code.value},
        )
        return ActResult(ok=False, code=code, message=message,
                         candidate=entry.candidate if entry else None,
                         version=version)

    def _audit(
        self,
        now_utc: datetime,
        candidate_id: str,
        action: str,
        actor: str,
        surface: Surface,
        version: int,
        prev: Optional[CandidateStatus] = None,
        new: Optional[CandidateStatus] = None,
        key: str = "",
        applied: bool = True,
        detail: str = "",
    ) -> None:
        self._ledger.record_audit(AuditEvent(
            ts=now_utc,
            mode=self._mode.value,
            candidate_id=candidate_id,
            action=action,
            actor=actor,
            surface=surface.value,
            version=version,
            idempotency_key=key,
            prev_status=prev.value if prev else "",
            new_status=new.value if new else "",
            applied=applied,
            detail=detail,
        ))
