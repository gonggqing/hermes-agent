"""PaperBroker — deterministic fill simulator (Loop.md §5.1, §2, §4).

Phase 0 broker adapter: simulates fills at limit / next-bar / close with
configurable slippage + commission, and tracks cash/positions for a CASH
account (Loop.md §2: no margin, no shorting, swing/positional only).

Design notes
------------
- Daily-bar oriented: :meth:`PaperBroker.step` advances one bar per symbol;
  MOC/LOC treat that bar's close as the session close (Loop.md §4: fills
  happen at 16:00 ET while the user sleeps).
- BUY orders reserve cash (qty * reference px + one commission) while they
  rest, so the sum of all resting BUY reservations can never exceed cash —
  a cash account cannot over-commit.
- SELL orders require an existing long position with enough *unreserved*
  quantity (quantity already committed to other resting SELL orders is
  unavailable; OCA siblings count once, since only one of them can fill).
- BRACKET (BUY entry, Loop.md §4 order policy): parent entry LMT plus a
  protective STP child and an optional take-profit LMT child in one OCA
  group. Children start ``NEW`` (inactive) and activate (``SUBMITTED``,
  qty = parent cumulative filled qty) as the parent fills; they become
  fillable from the NEXT bar, never on the bar that filled the parent.
- Fully deterministic and network-free (Loop.md §3): all market data is
  injected via :class:`swing_trader.interfaces.Bar`.
- All timestamps timezone-aware UTC; fills are stamped with the bar's ts.
"""

from __future__ import annotations

from typing import Optional

from swing_trader.config import Mode
from swing_trader.interfaces import Bar, BrokerInterface, PlaceResult
from swing_trader.log import get_logger
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    Fill,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Side,
    TimeInForce,
)

logger = get_logger(__name__)

_EPS = 1e-9

_RESTING = (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)
_ACTIVE = (OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)


class PaperBroker(BrokerInterface):
    """Deterministic paper-trading broker for a cash account (Loop.md §5.1)."""

    mode: Mode = Mode.PAPER

    def __init__(
        self,
        starting_cash: float = 2000.0,
        commission_per_order: float = 1.0,
        slippage_bps: float = 5.0,
        liquidity_fraction: float = 1.0,
    ) -> None:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        if commission_per_order < 0:
            raise ValueError("commission_per_order must be >= 0")
        if slippage_bps < 0:
            raise ValueError("slippage_bps must be >= 0")
        if not 0 < liquidity_fraction <= 1:
            raise ValueError("liquidity_fraction must be in (0, 1]")

        self.starting_cash = starting_cash
        self.commission_per_order = commission_per_order
        self.slippage_bps = slippage_bps
        self.liquidity_fraction = liquidity_fraction

        self._cash: float = starting_cash
        self._day_open_equity: float = starting_cash
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._marks: dict[str, float] = {}  # last close per symbol
        # BUY-side cash reservations: order_id -> (reference px, reserved cash)
        self._buy_ref_px: dict[str, float] = {}
        self._reserved: dict[str, float] = {}
        # bracket bookkeeping: parent order id -> child order ids
        self._children: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ helpers

    @property
    def _slip(self) -> float:
        return self.slippage_bps / 10_000.0

    def _mark_for(self, pos: Position) -> float:
        return pos.mkt_px if pos.mkt_px is not None else pos.avg_px

    def _equity(self) -> float:
        return self._cash + sum(
            pos.qty * self._mark_for(pos) for pos in self._positions.values()
        )

    def _total_reserved(self) -> float:
        return sum(self._reserved.values())

    def _reserved_sell_qty(self, symbol: str) -> float:
        """Position qty already committed to resting SELL orders.

        OCA siblings target the same shares, so a group counts once (its max
        remaining qty), not the sum of both legs.
        """
        plain = 0.0
        oca_max: dict[str, float] = {}
        for o in self._orders.values():
            if o.symbol != symbol or o.side is not Side.SELL:
                continue
            if o.status not in _RESTING:
                continue
            remaining = o.qty - o.filled_qty
            if o.oca_group:
                oca_max[o.oca_group] = max(oca_max.get(o.oca_group, 0.0), remaining)
            else:
                plain += remaining
        return plain + sum(oca_max.values())

    def _buy_reference_px(self, order: Order) -> Optional[float]:
        """Reference price used to reserve cash for a resting BUY."""
        if order.order_type in (OrderType.LMT, OrderType.LOC, OrderType.BRACKET):
            return order.limit
        if order.order_type is OrderType.STP:
            assert order.stop is not None  # schema-enforced
            return order.stop * (1 + self._slip)
        if order.order_type is OrderType.MOC:
            return self._marks.get(order.symbol)  # None -> no reference price
        return None

    def _update_buy_reservation(self, order: Order) -> None:
        if order.id not in self._reserved:
            return
        remaining = order.qty - order.filled_qty
        if remaining <= _EPS:
            self._release_reservation(order.id)
        else:
            self._reserved[order.id] = (
                remaining * self._buy_ref_px[order.id] + self.commission_per_order
            )

    def _release_reservation(self, order_id: str) -> None:
        self._reserved.pop(order_id, None)
        self._buy_ref_px.pop(order_id, None)

    def _cancel_new_children(self, parent: Order) -> None:
        """Void never-activated (NEW) bracket children of a dead parent."""
        for cid in self._children.get(parent.id, []):
            child = self._orders[cid]
            if child.status is OrderStatus.NEW:
                child.status = OrderStatus.CANCELLED

    # ------------------------------------------------------------------ placement

    def place_order(self, order: Order) -> PlaceResult:
        stored = order.model_copy(deep=True)
        stored.mode = Mode.PAPER

        def reject(reason: str) -> PlaceResult:
            stored.status = OrderStatus.REJECTED
            self._orders[stored.id] = stored
            logger.info(
                "order rejected",
                extra={"order_id": stored.id, "symbol": stored.symbol, "reason": reason},
            )
            return PlaceResult(accepted=False, order=stored.model_copy(deep=True), reason=reason)

        if stored.id in self._orders:
            # do not overwrite the existing order's record
            dup = order.model_copy(deep=True)
            dup.status = OrderStatus.REJECTED
            return PlaceResult(
                accepted=False, order=dup, reason=f"duplicate order id {stored.id}"
            )

        if stored.side is Side.SELL:
            if stored.order_type is OrderType.BRACKET:
                return reject("shorting not allowed: SELL bracket is a short entry")
            pos = self._positions.get(stored.symbol)
            if pos is None or pos.qty <= _EPS:
                return reject(f"shorting not allowed: no long position in {stored.symbol}")
            available = pos.qty - self._reserved_sell_qty(stored.symbol)
            if stored.qty > available + _EPS:
                return reject(
                    f"insufficient unreserved position in {stored.symbol}: "
                    f"available {available:g}, requested {stored.qty:g}"
                )
        else:  # BUY: reserve cash
            ref_px = self._buy_reference_px(stored)
            if ref_px is None:
                return reject(f"no reference price for {stored.symbol} MOC order")
            reservation = stored.qty * ref_px + self.commission_per_order
            if self._total_reserved() + reservation > self._cash + _EPS:
                return reject(
                    f"insufficient cash: need {reservation:.2f} reserved, "
                    f"already reserved {self._total_reserved():.2f}, cash {self._cash:.2f}"
                )
            self._buy_ref_px[stored.id] = ref_px
            self._reserved[stored.id] = reservation

        stored.status = OrderStatus.SUBMITTED
        self._orders[stored.id] = stored

        children: list[Order] = []
        if stored.order_type is OrderType.BRACKET:
            children = self._create_bracket_children(stored)

        logger.info(
            "order accepted",
            extra={
                "order_id": stored.id,
                "symbol": stored.symbol,
                "side": stored.side.value,
                "type": stored.order_type.value,
                "qty": stored.qty,
            },
        )
        return PlaceResult(
            accepted=True,
            order=stored.model_copy(deep=True),
            child_orders=[c.model_copy(deep=True) for c in children],
        )

    def _create_bracket_children(self, parent: Order) -> list[Order]:
        """Protective STP (+ optional take-profit LMT) legs, one OCA group.

        Children start NEW (inactive) with the parent's intended qty and
        activate with qty = parent cumulative filled qty as the parent fills.
        Both legs are GTC: never leave a position without a resting stop
        (Loop.md §4).
        """
        oca_group = f"oca-{parent.id}"
        assert parent.stop is not None  # schema-enforced for BRACKET
        stop_child = Order(
            ts=parent.ts,
            mode=Mode.PAPER,
            symbol=parent.symbol,
            side=Side.SELL,
            qty=parent.qty,
            order_type=OrderType.STP,
            stop=parent.stop,
            tif=TimeInForce.GTC,
            status=OrderStatus.NEW,
            parent_order_id=parent.id,
            oca_group=oca_group,
        )
        children = [stop_child]
        if parent.tp is not None:
            tp_child = Order(
                ts=parent.ts,
                mode=Mode.PAPER,
                symbol=parent.symbol,
                side=Side.SELL,
                qty=parent.qty,
                order_type=OrderType.LMT,
                limit=parent.tp,
                tif=TimeInForce.GTC,
                status=OrderStatus.NEW,
                parent_order_id=parent.id,
                oca_group=oca_group,
            )
            children.append(tp_child)
        for child in children:
            self._orders[child.id] = child
        self._children[parent.id] = [c.id for c in children]
        return children

    # ------------------------------------------------------------------ stepping

    def step(self, bars: dict[str, Bar]) -> list[Fill]:
        """Advance one bar per symbol; fill resting orders; update marks.

        Orders activated mid-step (bracket children) become fillable from
        the next bar, not the bar that filled their parent.
        """
        fills: list[Fill] = []
        # snapshot: children activated during this step must wait one bar
        resting_ids = [oid for oid, o in self._orders.items() if o.status in _RESTING]
        for oid in resting_ids:
            order = self._orders[oid]
            if order.status not in _RESTING:  # cancelled mid-step by an OCA sibling
                continue
            bar = bars.get(order.symbol)
            if bar is None:
                continue
            px = self._fill_price(order, bar)
            if px is None:
                continue
            qty = min(order.qty - order.filled_qty, self.liquidity_fraction * bar.volume)
            if order.side is Side.SELL:  # cash account: never sell below zero
                pos = self._positions.get(order.symbol)
                qty = min(qty, pos.qty if pos else 0.0)
            if qty <= _EPS:
                continue
            fills.append(self._apply_fill(order, qty, px, bar))
        # marks: last close per symbol
        for symbol, bar in bars.items():
            self._marks[symbol] = bar.close
            pos = self._positions.get(symbol)
            if pos is not None:
                pos.mkt_px = bar.close
        return [f.model_copy(deep=True) for f in fills]

    def _fill_price(self, order: Order, bar: Bar) -> Optional[float]:
        """Deterministic fill price for this bar, or None if no fill."""
        ot = order.order_type
        if ot in (OrderType.LMT, OrderType.BRACKET):  # bracket parent fills as LMT
            limit = order.limit
            assert limit is not None
            if order.side is Side.BUY:
                if bar.open <= limit:
                    return bar.open  # gap-open price improvement
                if bar.low <= limit:
                    return limit
            else:
                if bar.open >= limit:
                    return bar.open
                if bar.high >= limit:
                    return limit
            return None
        if ot is OrderType.STP:
            stop = order.stop
            assert stop is not None
            if order.side is Side.SELL:
                if bar.open <= stop:
                    return bar.open * (1 - self._slip)
                if bar.low <= stop:
                    return stop * (1 - self._slip)
            else:
                if bar.open >= stop:
                    return bar.open * (1 + self._slip)
                if bar.high >= stop:
                    return stop * (1 + self._slip)
            return None
        if ot is OrderType.MOC:
            if order.side is Side.BUY:
                return bar.close * (1 + self._slip)
            return bar.close * (1 - self._slip)
        if ot is OrderType.LOC:  # close must satisfy limit; no slippage
            limit = order.limit
            assert limit is not None
            if order.side is Side.BUY and bar.close <= limit:
                return bar.close
            if order.side is Side.SELL and bar.close >= limit:
                return bar.close
            return None
        return None  # pragma: no cover - all order types handled above

    def _apply_fill(self, order: Order, qty: float, px: float, bar: Bar) -> Fill:
        commission = self.commission_per_order
        fill = Fill(
            ts=bar.ts,
            order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=qty,
            px=px,
            commission=commission,
            mode=Mode.PAPER,
        )
        self._fills.append(fill)

        # order bookkeeping (weighted average fill price)
        prev_filled = order.filled_qty
        new_filled = prev_filled + qty
        if order.avg_fill_px is None:
            order.avg_fill_px = px
        else:
            order.avg_fill_px = (order.avg_fill_px * prev_filled + px * qty) / new_filled
        order.filled_qty = new_filled
        fully_filled = (order.qty - new_filled) <= _EPS
        order.status = OrderStatus.FILLED if fully_filled else OrderStatus.PARTIALLY_FILLED

        # cash & positions
        if order.side is Side.BUY:
            self._cash -= qty * px + commission
            pos = self._positions.get(order.symbol)
            if pos is None:
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, qty=qty, avg_px=px, mkt_px=px
                )
            else:  # weighted average entry price
                total = pos.qty + qty
                pos.avg_px = (pos.avg_px * pos.qty + px * qty) / total
                pos.qty = total
            self._update_buy_reservation(order)
        else:
            self._cash += qty * px - commission
            pos = self._positions[order.symbol]
            pos.qty -= qty
            if pos.qty <= _EPS:
                del self._positions[order.symbol]

        # bracket: activate/resize children to the parent's cumulative fill
        if order.order_type is OrderType.BRACKET:
            for cid in self._children.get(order.id, []):
                child = self._orders[cid]
                if child.status in (OrderStatus.NEW, *_RESTING):
                    child.qty = order.filled_qty
                    if child.status is OrderStatus.NEW:
                        child.status = OrderStatus.SUBMITTED

        # OCA: one leg fully filled -> cancel siblings
        if fully_filled and order.oca_group:
            for sibling in self._orders.values():
                if (
                    sibling.oca_group == order.oca_group
                    and sibling.id != order.id
                    and sibling.status in _ACTIVE
                ):
                    sibling.status = OrderStatus.CANCELLED
                    self._release_reservation(sibling.id)

        logger.info(
            "fill",
            extra={
                "order_id": order.id,
                "symbol": order.symbol,
                "side": order.side.value,
                "qty": qty,
                "px": px,
                "status": order.status.value,
            },
        )
        return fill

    # ------------------------------------------------------------------ day boundaries

    def restore_state(
        self,
        cash: float,
        positions: list[Position],
        orders: list[Order],
        day_open_equity: Optional[float] = None,
    ) -> list[str]:
        """Load replayed state into a FRESH broker (Loop.md Phase 0.5:
        rehydration across Finance-service restarts).

        Only :mod:`swing_trader.rehydrate` should call this, with state
        replayed from the Ledger. Must be called before any trading activity;
        refuses to run on a broker that already has state. Returns a list of
        human-readable warnings (never raises for recoverable oddities —
        protection must come back up even if a reservation cannot).
        """
        if self._orders or self._positions or self._fills:
            raise RuntimeError("restore_state requires a fresh PaperBroker")
        warnings: list[str] = []
        self._cash = cash
        for pos in positions:
            if pos.qty <= 0:
                continue
            self._positions[pos.symbol] = pos.model_copy(deep=True)
        for order in orders:
            self._orders[order.id] = order.model_copy(deep=True)
        # Rebuild bracket parent -> children wiring.
        for order in self._orders.values():
            if order.parent_order_id:
                self._children.setdefault(order.parent_order_id, []).append(order.id)
        # Rebuild BUY-side cash reservations for resting orders.
        for order in self._orders.values():
            if order.side is not Side.BUY or order.status not in _RESTING:
                continue
            ref = self._buy_reference_px(order)
            if ref is None:
                warnings.append(
                    f"no reference price to reserve cash for resting BUY "
                    f"{order.id[:8]} ({order.symbol} {order.order_type.value})"
                )
                continue
            self._buy_ref_px[order.id] = ref
            # Write the reservation directly: _update_buy_reservation only
            # refreshes EXISTING entries (it early-returns on unknown ids).
            remaining = order.qty - order.filled_qty
            self._reserved[order.id] = (
                remaining * ref + self.commission_per_order
            )
        # Sanity: resting SELLs must be covered by restored positions.
        for symbol in {o.symbol for o in self._orders.values()}:
            held = self._positions.get(symbol)
            reserved = self._reserved_sell_qty(symbol)
            if reserved > (held.qty if held else 0.0) + _EPS:
                warnings.append(
                    f"resting SELL qty {reserved:g} exceeds held "
                    f"{held.qty if held else 0:g} in {symbol}"
                )
        self._day_open_equity = (
            day_open_equity if day_open_equity is not None else self._equity()
        )
        logger.info(
            "broker state restored",
            extra={
                "cash": round(self._cash, 2),
                "n_positions": len(self._positions),
                "n_orders": len(self._orders),
                "n_warnings": len(warnings),
            },
        )
        return warnings

    def start_of_day(self) -> None:
        """Reset the day-open equity anchor to current equity."""
        self._day_open_equity = self._equity()

    def end_of_day(self) -> None:
        """Expire resting DAY orders (their fills, if any, stand)."""
        for order in self._orders.values():
            if order.tif is TimeInForce.DAY and order.status in _RESTING:
                order.status = OrderStatus.EXPIRED
                self._release_reservation(order.id)
                if order.order_type is OrderType.BRACKET:
                    self._cancel_new_children(order)
                logger.info("order expired", extra={"order_id": order.id})

    # ------------------------------------------------------------------ queries

    def get_account(self) -> AccountSnapshot:
        equity = self._equity()
        upnl = sum(
            (self._mark_for(pos) - pos.avg_px) * pos.qty for pos in self._positions.values()
        )
        day_pnl = equity - self._day_open_equity
        if self._day_open_equity > _EPS:
            drawdown_pct = min(0.0, day_pnl / self._day_open_equity * 100.0)
        else:  # pragma: no cover - defensive; equity starts positive
            drawdown_pct = 0.0
        return AccountSnapshot(
            mode=Mode.PAPER,
            equity=equity,
            cash=self._cash,
            upnl=upnl,
            day_pnl=day_pnl,
            drawdown_pct=drawdown_pct,
            breaker_state=BreakerState.NORMAL,  # breaker decided by the RiskEngine
        )

    def get_positions(self) -> list[Position]:
        return [pos.model_copy(deep=True) for pos in self._positions.values()]

    def cancel_order(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None or order.status not in _ACTIVE:
            return False
        order.status = OrderStatus.CANCELLED
        self._release_reservation(order.id)
        if order.order_type is OrderType.BRACKET:
            self._cancel_new_children(order)
        logger.info("order cancelled", extra={"order_id": order.id})
        return True

    def get_orders(self, active_only: bool = False) -> list[Order]:
        orders = self._orders.values()
        if active_only:
            orders = [o for o in orders if o.status in _ACTIVE]
        return [o.model_copy(deep=True) for o in orders]

    def get_fills(self) -> list[Fill]:
        return [f.model_copy(deep=True) for f in self._fills]
