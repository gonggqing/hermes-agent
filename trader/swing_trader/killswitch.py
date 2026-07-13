"""Manual kill-switch (Loop.md §3 / Phase 0.95 go-live gate).

A file-based operator HALT that stops all NEW entries immediately and until it
is explicitly released. Chosen deliberately over an in-memory flag or a DB row:

- **Out-of-band.** An operator can engage it with a plain ``touch`` (or the
  runbook's ``python -m swing_trader kill``) even if the HTTP service is wedged
  or the event loop is stuck — the only thing that must work is the filesystem.
- **Persistent + fail-safe.** It survives a process restart, so a halt is never
  silently forgotten on a crash-restart. If we cannot even *read* whether the
  file exists, :meth:`engaged` returns True (fail CLOSED — a kill-switch whose
  state is unknown must assume "halt").

IMPORTANT (Loop.md §4): engaging the kill-switch does NOT cancel resting
protective stops — that would leave open positions naked. Flattening/cancelling
is a SEPARATE, deliberate operator action (ExecutionEngine.cancel_all_orders).
The kill-switch only blocks NEW entries; exits and protection are untouched.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["KillSwitch", "KillSwitchState", "kill_switch_path"]

#: Filename of the halt flag; lives next to the ledger DB so the service and the
#: `python -m swing_trader kill/release` CLI resolve the SAME file.
KILL_SWITCH_FILENAME = "KILL_SWITCH"


def kill_switch_path(db_path: str | Path) -> Path:
    """The kill-switch file path derived from the ledger DB path (its sibling).
    A relative DB path (default ``trader.db``) puts the flag in the cwd."""
    return Path(db_path).expanduser().resolve().parent / KILL_SWITCH_FILENAME


class KillSwitchState:
    """Immutable snapshot of the switch for display/health."""

    __slots__ = ("engaged", "reason", "actor", "since")

    def __init__(self, engaged: bool, reason: str = "", actor: str = "",
                 since: Optional[str] = None) -> None:
        self.engaged = engaged
        self.reason = reason
        self.actor = actor
        self.since = since

    def to_dict(self) -> dict:
        return {"engaged": self.engaged, "reason": self.reason,
                "actor": self.actor, "since": self.since}


class KillSwitch:
    """Filesystem HALT flag. The presence of ``path`` means ENGAGED."""

    def __init__(self, path: str | Path,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.path = Path(path)
        self._clock = clock

    # --------------------------------------------------------------- query

    def engaged(self) -> bool:
        """True when the switch is engaged. Fails CLOSED: if the presence of the
        file cannot be determined, assume engaged (safest)."""
        try:
            return self.path.exists()
        except OSError as exc:  # pragma: no cover - unusual FS error
            logger.warning("kill-switch presence check failed — assuming ENGAGED",
                           extra={"error": str(exc)[:200]})
            return True

    def state(self) -> KillSwitchState:
        if not self.engaged():
            return KillSwitchState(engaged=False)
        reason, actor, since = "", "", None
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            if raw:
                data = json.loads(raw)
                reason = str(data.get("reason", ""))
                actor = str(data.get("actor", ""))
                since = data.get("since")
        except (OSError, ValueError):
            # Engaged by a bare `touch` (no JSON body) — still a valid halt.
            pass
        return KillSwitchState(engaged=True, reason=reason, actor=actor, since=since)

    # -------------------------------------------------------------- mutate

    def engage(self, reason: str = "", actor: str = "operator") -> KillSwitchState:
        """Engage the halt (idempotent). Records reason/actor/timestamp."""
        since = self._clock().isoformat()
        payload = {"reason": reason, "actor": actor, "since": since}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            logger.error("could not write kill-switch file", extra={"error": str(exc)[:200]})
            raise
        logger.warning("KILL-SWITCH ENGAGED — new entries halted",
                       extra={"actor": actor, "reason": reason[:200]})
        return KillSwitchState(engaged=True, reason=reason, actor=actor, since=since)

    def release(self, actor: str = "operator") -> KillSwitchState:
        """Release the halt (idempotent — a no-op if not engaged)."""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            logger.error("could not remove kill-switch file", extra={"error": str(exc)[:200]})
            raise
        logger.warning("kill-switch RELEASED — new entries permitted again",
                       extra={"actor": actor})
        return KillSwitchState(engaged=False)
