"""Paper-trading readiness report (Loop.md Phase 0.95 exit criterion).

The go-live gate requires progress toward **≥20 paper trading days** logged
end-to-end. This derives that count from the ledger — distinct market-calendar
dates on which the loop actually produced candidates (a session ran) and on
which fills occurred — plus the closed-trade count, so the human sign-off can
SEE how close the system is to the exit criterion instead of guessing.

Pure over the ledger; no network, no orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone
from zoneinfo import ZoneInfo

from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.schemas import Mode

logger = get_logger(__name__)

__all__ = ["PaperReadiness", "assess_paper_readiness"]

#: The Phase-0.95 exit target.
DEFAULT_MIN_DAYS = 20
#: Trading days are bucketed in the market calendar's tz (US session by default).
DEFAULT_MARKET_TZ = "America/New_York"


@dataclass(frozen=True)
class PaperReadiness:
    min_days: int
    session_days: int          # distinct dates a session produced candidates
    fill_days: int             # distinct dates a fill occurred
    closed_trades: int         # closed paper trades (round-trips)
    first_day: str | None      # ISO date of earliest activity
    last_day: str | None       # ISO date of latest activity

    @property
    def ready(self) -> bool:
        """The ≥N-day criterion is met AND at least one round-trip has closed
        (a run with zero closed trades proves nothing about exits)."""
        return self.session_days >= self.min_days and self.closed_trades >= 1

    @property
    def days_remaining(self) -> int:
        return max(0, self.min_days - self.session_days)

    def summary(self) -> str:
        state = "READY" if self.ready else "not yet"
        return (f"paper readiness [{state}]: {self.session_days}/{self.min_days} "
                f"session days ({self.days_remaining} to go), {self.fill_days} "
                f"days with fills, {self.closed_trades} closed trades "
                f"[{self.first_day or '—'} → {self.last_day or '—'}]")


def _dates(items, tz: ZoneInfo) -> set:
    out = set()
    for it in items:
        ts = getattr(it, "ts", None) or getattr(it, "entry_ts", None)
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.add(ts.astimezone(tz).date())
    return out


def assess_paper_readiness(
    ledger: Ledger,
    *,
    min_days: int = DEFAULT_MIN_DAYS,
    mode: Mode = Mode.PAPER,
    market_tz: str = DEFAULT_MARKET_TZ,
) -> PaperReadiness:
    """Count distinct trading days with loop activity from the ledger."""
    tz = ZoneInfo(market_tz)
    session_dates = _dates(ledger.get_candidates(mode=mode), tz)
    fill_dates = _dates(ledger.get_fills(mode), tz)
    closed = ledger.get_trades(mode, closed_only=True)
    all_dates = session_dates | fill_dates
    report = PaperReadiness(
        min_days=min_days,
        session_days=len(session_dates),
        fill_days=len(fill_dates),
        closed_trades=len(closed),
        first_day=min(all_dates).isoformat() if all_dates else None,
        last_day=max(all_dates).isoformat() if all_dates else None,
    )
    logger.info("paper readiness", extra={"summary": report.summary()})
    return report
