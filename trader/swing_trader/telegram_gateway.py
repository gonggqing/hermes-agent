"""Telegram confirmation gateway (Loop.md §5.6, §4).

Renders concise candidate-order cards, collects approve / edit / reject
responses, enforces the 11:30->12:30 ET confirmation window, and expires
stale candidates. APPROVED and EDITED both count as human-approved
(Loop.md §4: user approves / edits / rejects within the window).

DESIGN DECISION (Loop.md §8 allows substitution with justification): we
deliberately use the plain Telegram Bot HTTP API via the ``requests``
library (already installed as a yfinance dependency) instead of
``python-telegram-bot``. We need exactly three endpoints (``sendMessage``,
``getUpdates``, ``answerCallbackQuery``) inside a fixed one-hour daily
window; PTB is an async application framework — overkill for that — and a
thin injectable :class:`TelegramTransport` keeps tests trivially offline
(Loop.md §3: every external dependency mockable, tests never hit the
network).

Secrets policy (Loop.md §3): the bot token is embedded in every request
URL, so the URL is itself a secret — it is NEVER logged; log records carry
only the API method name.

Window semantics (Loop.md §4, §9): pushes happen only inside
[push_time_et, cutoff_et) in the market timezone (zoneinfo-based, so DST
is handled); after the cutoff every callback is refused ("window closed")
and :meth:`ConfirmationGateway.expire_stale` moves all still-pending
candidates to EXPIRED — nothing is ever executed without an explicit
in-window human confirmation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any, Optional, Protocol
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError

from swing_trader.log import get_logger
from swing_trader.schemas import CandidateOrder, CandidateStatus

logger = get_logger(__name__)

__all__ = [
    "ConfirmationGateway",
    "FinalizedCandidates",
    "GatewayError",
    "HttpTransport",
    "TelegramTransport",
    "build_keyboard",
    "render_card",
]

#: Telegram caps callback_data at 64 bytes; a 16-char id prefix + compact
#: JSON keeps us comfortably under (verified in tests).
CALLBACK_ID_LEN = 16

#: Card rationale is truncated to this many characters.
RATIONALE_MAX_CHARS = 300

#: Fields the user may change via a text-reply edit (Loop.md §4: edits stay
#: inside the candidate; side/symbol/order-type changes require a fresh
#: candidate through the risk engine).
EDITABLE_FIELDS: tuple[str, ...] = ("qty", "limit", "sl", "tp")

EDIT_INSTRUCTIONS = 'Reply with edits like "qty=N limit=X sl=Y tp=Z" (any subset).'

DEFAULT_BASE_URL = "https://api.telegram.org/bot{token}/{method}"

_EDIT_PAIR_RE = re.compile(r"([A-Za-z_]+)\s*=\s*([^\s,;]+)")


class GatewayError(Exception):
    """Raised on Telegram transport/API failure or gateway misuse."""


# --------------------------------------------------------------------------- transport


class TelegramTransport(Protocol):
    """Minimal Telegram surface the gateway needs (injectable, mockable)."""

    def send_message(
        self, chat_id: str, text: str, reply_markup: dict | None = None
    ) -> int:
        """Send a message; return the Telegram message id."""
        ...

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        """Fetch pending updates (long-poll up to ``timeout`` seconds)."""
        ...

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        """Acknowledge an inline-button press (optionally with a toast)."""
        ...

    def get_me(self) -> dict:
        """Return the bot's own identity (getMe): ``{id, username, ...}``."""
        ...


class HttpTransport:
    """Plain Telegram Bot HTTP API transport over an injectable session.

    The session only needs ``post(url, json=..., timeout=...) -> response``
    with ``response.status_code`` and ``response.json()`` — a mocked object
    in tests, a ``requests.Session`` in production (imported lazily so the
    module import never depends on it).

    SECURITY (Loop.md §3): the request URL embeds the bot token. It is never
    logged and never included in exception messages; errors carry only the
    API method name plus Telegram's own description.
    """

    def __init__(
        self,
        token: SecretStr | str,
        base_url: str = DEFAULT_BASE_URL,
        session: Any | None = None,
        request_timeout: float = 15.0,
    ) -> None:
        self._token = token if isinstance(token, SecretStr) else SecretStr(token)
        self._base_url = base_url
        if session is None:  # pragma: no cover - exercised via injected fakes
            import requests

            session = requests.Session()
        self._session = session
        self._request_timeout = request_timeout

    def _call(
        self, method: str, payload: dict[str, Any], extra_timeout: float = 0.0
    ) -> Any:
        # NOTE: `url` contains the bot token — never log it (Loop.md §3).
        url = self._base_url.format(
            token=self._token.get_secret_value(), method=method
        )
        try:
            resp = self._session.post(
                url, json=payload, timeout=self._request_timeout + extra_timeout
            )
        except Exception as exc:
            # `from None`: requests exceptions embed the (secret) URL.
            logger.error(
                "telegram transport error",
                extra={"tg_method": method, "error_type": type(exc).__name__},
            )
            raise GatewayError(
                f"telegram {method}: transport error ({type(exc).__name__})"
            ) from None
        status = getattr(resp, "status_code", None)
        if status != 200:
            logger.error(
                "telegram HTTP error", extra={"tg_method": method, "status": status}
            )
            raise GatewayError(f"telegram {method}: HTTP {status}")
        try:
            body = resp.json()
        except ValueError:
            logger.error("telegram non-JSON response", extra={"tg_method": method})
            raise GatewayError(f"telegram {method}: non-JSON response") from None
        if not isinstance(body, dict) or body.get("ok") is not True:
            description = ""
            if isinstance(body, dict):
                description = str(body.get("description", ""))
            logger.error(
                "telegram API not ok",
                extra={"tg_method": method, "description": description},
            )
            raise GatewayError(f"telegram {method}: not ok: {description}")
        logger.debug("telegram call ok", extra={"tg_method": method})
        return body.get("result")

    def send_message(
        self, chat_id: str, text: str, reply_markup: dict | None = None
    ) -> int:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = self._call("sendMessage", payload)
        return int(result["message_id"])

    def get_updates(self, offset: int | None = None, timeout: int = 0) -> list[dict]:
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload, extra_timeout=float(timeout))
        return list(result or [])

    def answer_callback(self, callback_query_id: str, text: str = "") -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._call("answerCallbackQuery", payload)

    def get_me(self) -> dict:
        return self._call("getMe", {}) or {}


# --------------------------------------------------------------------------- cards


def _fmt_px(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:g}"


def render_card(c: CandidateOrder) -> str:
    """Concise plain-text confirmation card (Loop.md §5.6)."""
    rationale = c.rationale
    if len(rationale) > RATIONALE_MAX_CHARS:
        rationale = rationale[:RATIONALE_MAX_CHARS] + "..."
    lines = [
        f"{c.symbol} {c.side.value} {c.qty:g} {c.order_type.value} ({c.tif.value})",
        (
            f"limit={_fmt_px(c.limit)} stop={_fmt_px(c.stop)} "
            f"tp={_fmt_px(c.tp)} sl={_fmt_px(c.sl)}"
        ),
        f"confidence: {round(c.confidence * 100)}%",
        f"rationale: {rationale}",
    ]
    if c.risk_note:
        lines.append(f"risk: {c.risk_note}")
    return "\n".join(lines)


def _callback_data(c: CandidateOrder, action: str) -> str:
    """Compact JSON callback payload; must stay <= 64 bytes (Telegram cap)."""
    return json.dumps(
        {"id": c.id[:CALLBACK_ID_LEN], "a": action}, separators=(",", ":")
    )


def build_keyboard(c: CandidateOrder) -> dict:
    """One-row inline keyboard: Approve / Edit / Reject (Loop.md §5.6)."""
    return {
        "inline_keyboard": [
            [
                {"text": "Approve", "callback_data": _callback_data(c, "ok")},
                {"text": "Edit", "callback_data": _callback_data(c, "edit")},
                {"text": "Reject", "callback_data": _callback_data(c, "no")},
            ]
        ]
    }


def _parse_edit(text: str) -> tuple[dict[str, float], list[str]]:
    """Parse ``qty=N limit=X sl=Y tp=Z`` pairs (any subset) from a reply.

    Returns (changes, errors); any error refuses the WHOLE edit so a
    partially-understood instruction is never silently half-applied.
    """
    changes: dict[str, float] = {}
    errors: list[str] = []
    for match in _EDIT_PAIR_RE.finditer(text):
        key = match.group(1).lower()
        raw = match.group(2)
        if key not in EDITABLE_FIELDS:
            errors.append(f"unknown field '{key}'")
            continue
        try:
            changes[key] = float(raw)
        except ValueError:
            errors.append(f"bad number for '{key}': {raw!r}")
    return changes, errors


# --------------------------------------------------------------------------- gateway


@dataclass
class _Pending:
    candidate: CandidateOrder
    message_id: int
    awaiting_edit: bool = False


@dataclass(frozen=True)
class FinalizedCandidates:
    """Snapshot of every candidate the human (or the clock) has settled."""

    approved: list[CandidateOrder]
    edited: list[CandidateOrder]
    rejected: list[CandidateOrder]
    expired: list[CandidateOrder]

    @property
    def human_approved(self) -> list[CandidateOrder]:
        """APPROVED + EDITED both count as human-approved (Loop.md §4)."""
        return [*self.approved, *self.edited]


class ConfirmationGateway:
    """Push cards, collect responses, enforce the ET window (Loop.md §5.6, §4).

    All ``now_utc`` arguments must be timezone-aware; they are converted to
    the market timezone with ``zoneinfo`` so DST transitions are correct
    (Loop.md §9). Nothing here places orders — approved/edited candidates
    are handed to the execution module, which re-validates before send
    (Loop.md §5.7).
    """

    def __init__(
        self,
        transport: TelegramTransport,
        chat_id: str,
        push_time_et: time = time(11, 30),
        cutoff_et: time = time(12, 30),
        market_tz: str = "America/New_York",
    ) -> None:
        if cutoff_et <= push_time_et:
            raise GatewayError(
                "cutoff_et must be after push_time_et (Loop.md §4: 11:30 -> 12:30 ET)"
            )
        self._transport = transport
        self._chat_id = chat_id
        self._push_time_et = push_time_et
        self._cutoff_et = cutoff_et
        self._tz = ZoneInfo(market_tz)
        self._pending: dict[str, _Pending] = {}
        self._offset: Optional[int] = None
        self._approved: list[CandidateOrder] = []
        self._edited: list[CandidateOrder] = []
        self._rejected: list[CandidateOrder] = []
        self._expired: list[CandidateOrder] = []

    # ------------------------------------------------------------------ window

    def _now_et(self, now_utc: datetime) -> datetime:
        if now_utc.tzinfo is None:
            raise GatewayError("now_utc must be timezone-aware UTC (Loop.md §3 tests)")
        return now_utc.astimezone(self._tz)

    def in_window(self, now_utc: datetime) -> bool:
        """True iff push_time_et <= now (in market tz) < cutoff_et."""
        now_t = self._now_et(now_utc).time()
        return self._push_time_et <= now_t < self._cutoff_et

    # ------------------------------------------------------------------ push

    def push(
        self, candidates: list[CandidateOrder], now_utc: datetime
    ) -> list[CandidateOrder]:
        """Send one card per RISK_APPROVED candidate; refuse outside the window."""
        if not self.in_window(now_utc):
            logger.warning(
                "push refused: outside confirmation window (Loop.md §4)",
                extra={"n_candidates": len(candidates)},
            )
            return []
        pushed: list[CandidateOrder] = []
        for c in candidates:
            if c.status is not CandidateStatus.RISK_APPROVED:
                logger.info(
                    "skipping candidate: not risk-approved",
                    extra={"candidate_id": c.id, "status": c.status.value},
                )
                continue
            key = c.id[:CALLBACK_ID_LEN]
            if key in self._pending:
                logger.warning(
                    "skipping candidate: id already pending",
                    extra={"candidate_id": c.id},
                )
                continue
            message_id = self._transport.send_message(
                self._chat_id, render_card(c), reply_markup=build_keyboard(c)
            )
            pushed_c = c.model_copy(update={"status": CandidateStatus.PUSHED})
            self._pending[key] = _Pending(candidate=pushed_c, message_id=message_id)
            pushed.append(pushed_c)
            logger.info(
                "candidate pushed",
                extra={"candidate_id": c.id, "symbol": c.symbol},
            )
        return pushed

    # ------------------------------------------------------------------ poll

    def poll_responses(self, now_utc: datetime) -> list[CandidateOrder]:
        """Process pending Telegram updates; return candidates finalized now.

        Every processed update advances the getUpdates offset, whatever its
        outcome, so nothing is re-processed on the next poll.
        """
        finalized: list[CandidateOrder] = []
        updates = self._transport.get_updates(offset=self._offset, timeout=0)
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = update_id + 1
                if self._offset is None or next_offset > self._offset:
                    self._offset = next_offset
            done: Optional[CandidateOrder] = None
            if "callback_query" in update:
                done = self._handle_callback(update["callback_query"], now_utc)
            elif "message" in update:
                done = self._handle_message(update["message"], now_utc)
            else:
                logger.debug("ignoring unrecognized update type")
            if done is not None:
                finalized.append(done)
        return finalized

    def _handle_callback(
        self, cq: dict, now_utc: datetime
    ) -> Optional[CandidateOrder]:
        cq_id = str(cq.get("id", ""))
        try:
            data = json.loads(cq.get("data") or "")
            key = data["id"]
            action = data["a"]
        except (json.JSONDecodeError, TypeError, KeyError):
            logger.warning("malformed callback data ignored")
            self._transport.answer_callback(
                cq_id, "Sorry, I could not read that action."
            )
            return None
        if not isinstance(key, str) or not isinstance(action, str):
            logger.warning("malformed callback data ignored")
            self._transport.answer_callback(
                cq_id, "Sorry, I could not read that action."
            )
            return None
        entry = self._pending.get(key)
        if entry is None:
            logger.warning("callback for unknown candidate ignored")
            self._transport.answer_callback(
                cq_id, "Unknown or already-finalized candidate."
            )
            return None
        if not self.in_window(now_utc):
            # Loop.md §4: after 12:30 ET the decision stands as EXPIRED —
            # refuse every action so nothing is approved after cutoff.
            logger.warning(
                "callback refused: window closed",
                extra={"candidate_id": entry.candidate.id, "action": action},
            )
            self._transport.answer_callback(
                cq_id, "Window closed - candidate will expire."
            )
            return None
        if action == "ok":
            return self._finalize(
                key, CandidateStatus.APPROVED, self._approved, cq_id, "Approved"
            )
        if action == "no":
            return self._finalize(
                key, CandidateStatus.REJECTED, self._rejected, cq_id, "Rejected"
            )
        if action == "edit":
            entry.awaiting_edit = True
            logger.info(
                "candidate awaiting edit", extra={"candidate_id": entry.candidate.id}
            )
            self._transport.answer_callback(cq_id, EDIT_INSTRUCTIONS)
            return None
        logger.warning("unknown callback action ignored", extra={"action": action})
        self._transport.answer_callback(cq_id, "Unknown action.")
        return None

    def _finalize(
        self,
        key: str,
        status: CandidateStatus,
        bucket: list[CandidateOrder],
        cq_id: str,
        verb: str,
    ) -> CandidateOrder:
        entry = self._pending.pop(key)
        final = entry.candidate.model_copy(update={"status": status})
        bucket.append(final)
        logger.info(
            "candidate finalized",
            extra={"candidate_id": final.id, "status": status.value},
        )
        self._transport.answer_callback(cq_id, f"{verb} {final.symbol}.")
        return final

    def _handle_message(
        self, msg: dict, now_utc: datetime
    ) -> Optional[CandidateOrder]:
        text = msg.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        entry = self._match_awaiting_edit(msg)
        if entry is None:
            logger.debug("text message with no awaiting-edit match ignored")
            return None
        candidate = entry.candidate
        if not self.in_window(now_utc):
            logger.warning(
                "edit refused: window closed", extra={"candidate_id": candidate.id}
            )
            self._send(
                f"Window closed - edit refused; {candidate.symbol} will expire."
            )
            return None
        changes, errors = _parse_edit(text)
        if errors or not changes:
            reason = "; ".join(errors) if errors else "no editable key=value pairs"
            self._send(
                f"Edit refused for {candidate.symbol} ({reason}). {EDIT_INSTRUCTIONS}"
            )
            return None  # keep awaiting_edit
        try:
            data = candidate.model_dump()
            data.update(changes)
            data["status"] = CandidateStatus.EDITED
            # model_validate RE-RUNS full schema validation: an edit that
            # strips protection (sl<=0) or sets qty<=0 is rejected here.
            final = CandidateOrder.model_validate(data)
        except ValidationError as exc:
            reasons = "; ".join(str(e.get("msg", "")) for e in exc.errors())
            logger.info(
                "edit refused by validation",
                extra={"candidate_id": candidate.id, "reasons": reasons},
            )
            self._send(
                f"Edit refused for {candidate.symbol}: {reasons}. {EDIT_INSTRUCTIONS}"
            )
            return None  # keep awaiting_edit
        del self._pending[candidate.id[:CALLBACK_ID_LEN]]
        self._edited.append(final)
        logger.info(
            "candidate edited & approved (Loop.md §4)",
            extra={"candidate_id": final.id, "changes": changes},
        )
        self._send(
            f"Edited and approved {final.symbol}: "
            + " ".join(f"{k}={v:g}" for k, v in changes.items())
        )
        return final

    def _match_awaiting_edit(self, msg: dict) -> Optional[_Pending]:
        awaiting = [e for e in self._pending.values() if e.awaiting_edit]
        if not awaiting:
            return None
        reply = msg.get("reply_to_message") or {}
        reply_id = reply.get("message_id")
        if reply_id is not None:
            for entry in awaiting:
                if entry.message_id == reply_id:
                    return entry
            return None  # reply to some other message
        if len(awaiting) == 1:
            return awaiting[0]
        self._send(
            "Multiple edits pending - reply directly to the card you want to edit."
        )
        return None

    def _send(self, text: str) -> None:
        self._transport.send_message(self._chat_id, text)

    # ------------------------------------------------------------------ expiry

    def expire_stale(self, now_utc: datetime) -> list[CandidateOrder]:
        """After the cutoff, move every still-pending candidate to EXPIRED."""
        now_t = self._now_et(now_utc).time()
        if now_t < self._cutoff_et:
            return []
        expired: list[CandidateOrder] = []
        for key in list(self._pending):
            entry = self._pending.pop(key)
            final = entry.candidate.model_copy(
                update={"status": CandidateStatus.EXPIRED}
            )
            self._expired.append(final)
            expired.append(final)
            logger.info(
                "candidate expired (window passed)",
                extra={"candidate_id": final.id, "symbol": final.symbol},
            )
        return expired

    # ------------------------------------------------------------------ accessors

    def finalized(self) -> FinalizedCandidates:
        """Approved / edited / rejected / expired lists (copies)."""
        return FinalizedCandidates(
            approved=list(self._approved),
            edited=list(self._edited),
            rejected=list(self._rejected),
            expired=list(self._expired),
        )

    @property
    def pending(self) -> dict[str, CandidateOrder]:
        """Still-unanswered candidates keyed by truncated callback id."""
        return {k: e.candidate for k, e in self._pending.items()}

    def is_awaiting_edit(self, candidate_id: str) -> bool:
        entry = self._pending.get(candidate_id[:CALLBACK_ID_LEN])
        return entry is not None and entry.awaiting_edit
