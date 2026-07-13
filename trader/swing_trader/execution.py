"""Execution engine (Loop.md §5.7).

Translates HUMAN-APPROVED candidates into broker orders:

- entries become GTC BRACKET orders (limit entry + attached protective stop
  + optional take-profit in an OCA group) — a position can never exist
  without a resting stop (Loop.md §4);
- discretionary exits pass through as MOC/LOC/LMT/STP;
- prices are RE-VALIDATED against a fresh quote before send (§5.7): expired
  candidates and adverse drift beyond tolerance are skipped;
- partials/rejects are handled by syncing broker state into the ledger.

Defense in depth (Loop.md §3/§9): even though config already gates live
trading, this engine independently refuses to run in live mode unless
explicitly told live orders are allowed. In Phase 0 only Mode.PAPER exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from swing_trader.interfaces import BrokerInterface
from swing_trader.ledger import Ledger
from swing_trader.log import get_logger
from swing_trader.schemas import (
    CandidateOrder,
    CandidateStatus,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

logger = get_logger(__name__)

__all__ = ["ExecutionEngine", "ExecutionReport", "GuardrailError"]


class GuardrailError(Exception):
    """Raised when an execution request violates a Loop.md §3 guardrail."""


@dataclass
class ExecutionReport:
    placed: list[Order] = field(default_factory=list)
    skipped: list[tuple[CandidateOrder, str]] = field(default_factory=list)
    rejected: list[tuple[Order, str]] = field(default_factory=list)


_ACTIONABLE = (CandidateStatus.APPROVED, CandidateStatus.EDITED)


class ExecutionEngine:
    def __init__(
        self,
        broker: BrokerInterface,
        ledger: Ledger,
        mode: Mode = Mode.PAPER,
        live_orders_allowed: bool = False,
        price_tolerance_pct: float = 1.5,
    ) -> None:
        self.broker = broker
        self.ledger = ledger
        self.mode = mode
        self.live_orders_allowed = live_orders_allowed
        self.price_tolerance_pct = price_tolerance_pct
        # order_id -> protective stop px of the originating candidate; used
        # to compute r_multiple when entry fills are written to the ledger.
        self._stop_by_order: dict[str, float] = {}
        self._synced_fill_ids: set[str] = set()

    # ------------------------------------------------------------------ api

    def execute(
        self,
        candidates: list[CandidateOrder],
        quotes: dict[str, float],
        now: datetime,
    ) -> ExecutionReport:
        """Place every actionable candidate that survives re-validation."""
        if self.mode is Mode.LIVE and not self.live_orders_allowed:
            raise GuardrailError(
                "live execution requested but live orders are not allowed "
                "(HUMAN_CONFIRM/BROKER/DRY_RUN gate — Loop.md §3)"
            )

        report = ExecutionReport()
        for cand in candidates:
            reason = self._revalidate(cand, quotes.get(cand.symbol), now)
            if reason is not None:
                self._skip(cand, reason, report)
                continue

            order = self._translate(cand)
            if order is None:
                self._skip(cand, "unsupported candidate shape for Phase 0", report)
                continue

            # Discretionary exits: the shares are typically committed to the
            # resting protective stop/tp OCA group, so cancel protection
            # first — and RESTORE it if the exit is then rejected (Loop.md
            # §4: never leave a position without a resting stop).
            cleared: list[Order] = []
            if cand.side is Side.SELL:
                cleared = self._clear_protection(cand.symbol)

            result = self.broker.place_order(order)
            if not result.accepted and cleared:
                self._restore_protection(cand.symbol, cleared)
            if not result.accepted:
                logger.warning(
                    "broker rejected order",
                    extra={"symbol": cand.symbol, "reason": result.reason},
                )
                self.ledger.record_order(result.order)
                self.ledger.update_candidate(
                    cand.id, cand.status, risk_note=f"broker rejected: {result.reason}"
                )
                report.rejected.append((result.order, result.reason))
                continue

            self.ledger.record_order(result.order)
            for child in result.child_orders:
                self.ledger.record_order(child)
            stop_px = cand.stop if cand.stop is not None else cand.sl
            if cand.side is Side.BUY and stop_px is not None:
                self._stop_by_order[result.order.id] = stop_px
            self.ledger.update_candidate(cand.id, CandidateStatus.PLACED)
            report.placed.append(result.order)
            logger.info(
                "order placed",
                extra={
                    "symbol": cand.symbol,
                    "side": cand.side.value,
                    "qty": result.order.qty,
                    "order_type": result.order.order_type.value,
                },
            )
        return report

    def seed_synced_fills(self, fill_ids: set[str]) -> None:
        """Mark ledger-known fills as already synced (rehydration path) so
        sync_fills() never re-records history after a service restart."""
        self._synced_fill_ids |= set(fill_ids)

    def seed_protective_stops(self, orders: list[Order]) -> None:
        """Rebuild the order-id -> protective-stop map after rehydration so
        entry fills on pre-restart orders still record r_multiple risk."""
        for order in orders:
            if order.side is Side.BUY and order.stop is not None:
                self._stop_by_order[order.id] = order.stop

    def sync_fills(self) -> int:
        """Pull new fills + order states from the broker into the ledger.

        Returns the number of new fills recorded. Partials simply arrive as
        multiple fills; bracket children were recorded at placement or are
        picked up here via get_orders().
        """
        new = 0
        for fill in self.broker.get_fills():
            if fill.id in self._synced_fill_ids:
                continue
            self._synced_fill_ids.add(fill.id)
            stop_px = self._stop_by_order.get(fill.order_id)
            self.ledger.record_fill(fill, stop_px=stop_px)
            new += 1
        for order in self.broker.get_orders():
            self.ledger.update_order(order)
        if new:
            logger.info("synced fills", extra={"n": new})
        return new

    def cancel_all_orders(self, *, include_protection: bool = True) -> list[Order]:
        """Deliberate operator flatten (Loop.md §3 kill-switch drill): cancel
        active working orders at the broker and mark them CANCELLED in the
        ledger. Returns the orders successfully cancelled.

        This is SEPARATE from the kill-switch (which only halts NEW entries):
        cancelling protective SELL stops leaves open positions naked, so it is
        an explicit action. ``include_protection=False`` keeps resting
        protective stops while cancelling only pending entries."""
        cancelled: list[Order] = []
        for order in self.broker.get_orders(active_only=True):
            if not include_protection and order.side is Side.SELL:
                continue  # keep protective stops resting
            if self.broker.cancel_order(order.id):
                self.ledger.update_order(
                    order.model_copy(update={"status": OrderStatus.CANCELLED})
                )
                cancelled.append(order)
        if cancelled:
            logger.warning(
                "cancel_all_orders",
                extra={"n": len(cancelled), "include_protection": include_protection},
            )
        return cancelled

    # ------------------------------------------------------------- internals

    def _revalidate(
        self, cand: CandidateOrder, last: float | None, now: datetime
    ) -> str | None:
        """Return a skip reason, or None when the candidate is still valid."""
        if cand.status not in _ACTIONABLE:
            return f"not actionable (status={cand.status.value})"
        if cand.valid_until is not None and now > cand.valid_until:
            self.ledger.update_candidate(
                cand.id, CandidateStatus.EXPIRED, risk_note="expired before execution"
            )
            return "validity window passed"

        if cand.side is Side.SELL:
            return None  # exits are never blocked on price drift

        # BUY entries: require a fresh quote and re-check signal validity (§5.7)
        if last is None:
            return "no fresh quote for re-validation (conservative: skip entry)"
        if cand.ref_px is not None:
            drift_pct = (last - cand.ref_px) / cand.ref_px * 100.0
            if drift_pct > self.price_tolerance_pct:
                return (
                    f"price ran away: last {last:g} is {drift_pct:.2f}% above "
                    f"ref {cand.ref_px:g} (> {self.price_tolerance_pct:g}% tolerance)"
                )
        protective = cand.stop if cand.stop is not None else cand.sl
        if protective is not None and last <= protective:
            return (
                f"thesis broken: last {last:g} already at/below protective "
                f"stop {protective:g}"
            )
        return None

    def _translate(self, cand: CandidateOrder) -> Order | None:
        """Candidate -> broker Order. Entries always carry attached protection."""
        if cand.side is Side.BUY:
            stop = cand.stop if cand.stop is not None else cand.sl
            if cand.order_type is OrderType.BRACKET:
                limit = cand.limit
            elif cand.order_type is OrderType.LMT:
                limit = cand.limit  # LMT + sl is upgraded to a full bracket
            else:
                return None  # MOC/LOC/STP entries not supported in Phase 0 v0
            if limit is None or stop is None:
                return None
            return Order(
                mode=self.mode,
                symbol=cand.symbol,
                side=Side.BUY,
                qty=cand.qty,
                order_type=OrderType.BRACKET,
                limit=limit,
                stop=stop,
                tp=cand.tp,
                tif=TimeInForce.GTC,
            )

        # SELL: discretionary exit passthrough
        return Order(
            mode=self.mode,
            symbol=cand.symbol,
            side=Side.SELL,
            qty=cand.qty,
            order_type=cand.order_type,
            limit=cand.limit,
            stop=cand.stop,
            tif=cand.tif,
        )

    def _skip(
        self, cand: CandidateOrder, reason: str, report: ExecutionReport
    ) -> None:
        logger.info("candidate skipped", extra={"symbol": cand.symbol, "reason": reason})
        report.skipped.append((cand, reason))

    def _clear_protection(self, symbol: str) -> list[Order]:
        """Cancel resting SELL orders (protective stop / tp legs) on a symbol."""
        cancelled: list[Order] = []
        for order in self.broker.get_orders(active_only=True):
            if order.symbol == symbol and order.side is Side.SELL:
                if self.broker.cancel_order(order.id):
                    cancelled.append(order)
                    self.ledger.update_order(
                        order.model_copy(update={"status": OrderStatus.CANCELLED})
                    )
        return cancelled

    def _restore_protection(self, symbol: str, cancelled: list[Order]) -> None:
        """Re-place protective stops after a failed exit (never stay naked).

        TODO(Phase 1): IBKR supports atomic cancel/replace; use it instead.
        """
        for old in cancelled:
            if old.order_type is not OrderType.STP:
                continue  # only the protective stop is safety-critical
            remaining = old.qty - old.filled_qty
            if remaining <= 0:
                continue
            replacement = Order(
                mode=self.mode,
                symbol=symbol,
                side=Side.SELL,
                qty=remaining,
                order_type=OrderType.STP,
                stop=old.stop,
                tif=TimeInForce.GTC,
            )
            result = self.broker.place_order(replacement)
            if result.accepted:
                self.ledger.record_order(result.order)
                logger.warning(
                    "protective stop re-placed after failed exit",
                    extra={"symbol": symbol, "stop": old.stop},
                )
            else:
                logger.error(
                    "POSITION MAY BE UNPROTECTED: could not restore stop",
                    extra={"symbol": symbol, "reason": result.reason},
                )
