"""Execution engine (Loop.md §5.7).

Translates HUMAN-APPROVED candidates into broker orders:

- entries become BRACKET orders with a DAY limit-entry parent + attached GTC
  protective stop + optional GTC take-profit in an OCA group (Loop.md §5.7):
  the entry is cancelled at the close if unfilled and re-researched next day,
  while a partial fill keeps GTC protection sized to the filled quantity — a
  position can never exist without a resting stop (Loop.md §4);
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

from swing_trader.cn_market import (
    is_cn_order_acceptable,
    is_cn_symbol,
    off_grid_cn_prices,
    quantity_violation as cn_quantity_violation,
)
from swing_trader.interfaces import BrokerInterface
from swing_trader.ledger import AuditEvent, Ledger
from swing_trader.log import get_logger
from swing_trader.hk_market_hours import is_hk_order_acceptable
from swing_trader.sehk_rules import is_sehk_symbol, off_grid_prices
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
    recovered: list[tuple[CandidateOrder, Order]] = field(default_factory=list)
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
        # symbol -> protective STPs cleared to place a discretionary exit. If the
        # exit later rests unfilled (and expires as a DAY order at the close),
        # reprotect_positions() re-establishes the stop so the position is never
        # left naked (Loop.md §4). Cleared when the position goes flat / re-armed.
        self._cleared_protection: dict[str, list[Order]] = {}

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
            existing = self._existing_order(cand)
            if existing is not None:
                self.ledger.update_candidate(cand.id, CandidateStatus.PLACED)
                self._audit_once(
                    cand, "execute_recovered", cand.status,
                    CandidateStatus.PLACED, now,
                    detail=f"existing order {existing.id} recovered",
                    key=f"execute:{cand.id}",
                )
                report.recovered.append((cand, existing))
                continue
            reason = self._revalidate(cand, quotes.get(cand.symbol), now)
            if reason is not None:
                self._skip(cand, reason, report, now)
                continue

            order = self._translate(cand)
            if order is None:
                self._skip(
                    cand, "unsupported candidate shape for Phase 0", report, now
                )
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
                self._audit_once(
                    cand, "execute_rejected", cand.status, cand.status, now,
                    detail=result.reason,
                    key=f"execute-rejected:{cand.id}:{result.order.id}",
                    applied=False,
                )
                report.rejected.append((result.order, result.reason))
                continue

            self.ledger.record_order(result.order)
            for child in result.child_orders:
                self.ledger.record_order(child)
            stop_px = cand.stop if cand.stop is not None else cand.sl
            if cand.side is Side.BUY and stop_px is not None:
                self._stop_by_order[result.order.id] = stop_px
            # A SELL exit stripped protection to place: remember the stops so the
            # close-time safety net can re-arm them if the exit rests unfilled
            # (a MOC exit fills at close and leaves nothing to re-protect).
            protective_cleared = [
                o for o in cleared if o.order_type is OrderType.STP
            ]
            if protective_cleared:
                self._cleared_protection[cand.symbol] = protective_cleared
            self.ledger.update_candidate(cand.id, CandidateStatus.PLACED)
            self._audit_once(
                cand, "execute", cand.status, CandidateStatus.PLACED, now,
                detail=f"order {result.order.id} submitted",
                key=f"execute:{cand.id}",
            )
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

        # HK microstructure: an SEHK limit/stop/tp must sit on the exchange tick
        # grid. Normalization happens once BEFORE approval so the card is valid;
        # here — after a human has approved — we must NOT silently round (rollout
        # doc), so an off-grid HK order is refused fail-closed and re-proposed.
        if is_sehk_symbol(cand.symbol):
            bad = off_grid_prices(
                {"limit": cand.limit, "stop": cand.stop, "tp": cand.tp}
            )
            if bad:
                return "HK order off SEHK tick grid: " + "; ".join(bad.values())
            # Gate only NEW ENTRIES on trading hours: SEHK doesn't accept orders
            # during the lunch break / outside sessions, and the scheduled
            # 10:30 flow is always in-hours so this just refuses an off-schedule
            # entry. A human-approved EXIT is never dropped here — blocking it
            # would discard the exit intent (the protective stop stays either
            # way); an exit is allowed to rest until the session resumes.
            if cand.side is Side.BUY and not is_hk_order_acceptable(now):
                return "HK market closed / lunch break — not accepting orders"

        if is_cn_symbol(cand.symbol):
            bad = off_grid_cn_prices(
                cand.symbol,
                {"limit": cand.limit, "stop": cand.stop, "tp": cand.tp},
            )
            if bad:
                return "CN order off exchange tick grid: " + "; ".join(bad.values())
            held = next(
                (p.qty for p in self.broker.get_positions() if p.symbol == cand.symbol),
                None,
            )
            qty_reason = cn_quantity_violation(cand.qty, cand.side, held_qty=held)
            if qty_reason:
                return "CN order quantity invalid: " + qty_reason
            if not is_cn_order_acceptable(now):
                return "CN market closed / lunch break — not accepting orders"

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
                id=f"candidate-{cand.id}",
                mode=self.mode,
                symbol=cand.symbol,
                side=Side.BUY,
                qty=cand.qty,
                order_type=OrderType.BRACKET,
                limit=limit,
                stop=stop,
                tp=cand.tp,
                # Entry parent is DAY (Loop.md §5.7): an unfilled entry is
                # cancelled at the close (broker.end_of_day) and re-researched
                # next day, never left resting overnight. The broker attaches
                # the protective stop/tp children as GTC on fill, so a partial
                # fill keeps GTC protection sized to the filled quantity while
                # the unfilled remainder expires. The engine enforces DAY here
                # regardless of the upstream candidate tif.
                tif=TimeInForce.DAY,
                broker_ref=f"candidate:{cand.id}",
            )

        # SELL: discretionary exit passthrough
        return Order(
            id=f"candidate-{cand.id}",
            mode=self.mode,
            symbol=cand.symbol,
            side=Side.SELL,
            qty=cand.qty,
            order_type=cand.order_type,
            limit=cand.limit,
            stop=cand.stop,
            tif=cand.tif,
            broker_ref=f"candidate:{cand.id}",
        )

    def _skip(
        self, cand: CandidateOrder, reason: str, report: ExecutionReport,
        now: datetime,
    ) -> None:
        logger.info("candidate skipped", extra={"symbol": cand.symbol, "reason": reason})
        self._audit_once(
            cand, "execute_skipped", cand.status, cand.status, now,
            detail=reason,
            key=f"execute-skipped:{cand.id}:{now.isoformat()}",
            applied=False,
        )
        report.skipped.append((cand, reason))

    def _existing_order(self, cand: CandidateOrder) -> Order | None:
        """Find a prior submission by its durable candidate correlation id."""
        marker = f"candidate:{cand.id}"
        order_id = f"candidate-{cand.id}"
        seen: dict[str, Order] = {}
        # Broker state is AUTHORITATIVE — list it LAST so its copy wins over any
        # stale ledger row (the ledger lags the broker between end_of_day and the
        # next sync, so a broker-EXPIRED order must not read as still-live here).
        for order in [*self.ledger.get_orders(self.mode), *self.broker.get_orders()]:
            seen[order.id] = order
        return next(
            (order for order in seen.values()
             if (order.id == order_id or order.broker_ref == marker)
             and order.status not in {
                 OrderStatus.REJECTED,
                 OrderStatus.CANCELLED,
                 OrderStatus.EXPIRED,
             }),
            None,
        )

    def _audit_once(
        self,
        cand: CandidateOrder,
        action: str,
        prev: CandidateStatus,
        new: CandidateStatus,
        now: datetime,
        *,
        detail: str,
        key: str,
        applied: bool = True,
    ) -> None:
        if self.ledger.get_audit(
            mode=self.mode, candidate_id=cand.id, idempotency_key=key
        ):
            return
        self.ledger.record_audit(AuditEvent(
            ts=now,
            mode=self.mode.value,
            candidate_id=cand.id,
            action=action,
            actor="system",
            surface="system",
            idempotency_key=key,
            prev_status=prev.value,
            new_status=new.value,
            applied=applied,
            detail=detail,
        ))

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

    def reprotect_positions(self, now: datetime) -> list[Order]:
        """Close-time safety net (Loop.md §4): re-arm a protective stop for any
        long position a discretionary exit stripped protection from but that is
        still open (the exit rested unfilled / expired as a DAY order). A MOC
        exit closes the position at the close, so there is nothing to re-protect;
        this only fires for a resting exit that did not fill. Returns re-placed
        stops. Call AFTER the close fills + broker.end_of_day()."""
        restored: list[Order] = []
        if not self._cleared_protection:
            return restored
        positions = {p.symbol: p for p in self.broker.get_positions() if p.qty > 0}
        active = self.broker.get_orders(active_only=True)
        for symbol, stops in list(self._cleared_protection.items()):
            pos = positions.get(symbol)
            if pos is None:  # exit closed the position — nothing to protect
                self._cleared_protection.pop(symbol, None)
                continue
            if any(
                o.symbol == symbol and o.side is Side.SELL
                and o.order_type is OrderType.STP for o in active
            ):
                self._cleared_protection.pop(symbol, None)  # already protected
                continue
            reserved = sum(
                o.qty - o.filled_qty for o in active
                if o.symbol == symbol and o.side is Side.SELL
            )
            unreserved = pos.qty - reserved
            stop_px = next((s.stop for s in stops if s.stop is not None), None)
            if unreserved <= 0 or stop_px is None:
                continue  # still committed to a resting exit — retry next close
            result = self.broker.place_order(Order(
                mode=self.mode, symbol=symbol, side=Side.SELL, qty=unreserved,
                order_type=OrderType.STP, stop=stop_px, tif=TimeInForce.GTC,
            ))
            if result.accepted:
                self.ledger.record_order(result.order)
                restored.append(result.order)
                self._cleared_protection.pop(symbol, None)
                logger.warning(
                    "protective stop re-armed for unfilled discretionary exit",
                    extra={"symbol": symbol, "qty": unreserved, "stop": stop_px},
                )
            else:
                logger.error(
                    "POSITION MAY BE UNPROTECTED: could not re-arm stop",
                    extra={"symbol": symbol, "reason": result.reason},
                )
        return restored
