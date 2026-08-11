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

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from swing_trader.cn_market import is_cn_symbol, is_listed_fund, quantity_violation
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
        starting_cash_by_currency: Optional[dict[str, float]] = None,
        base_currency: str = "USD",
        fx_to_base: Optional[dict[str, float]] = None,
        commission_per_order: float = 1.0,
        slippage_bps: float = 5.0,
        liquidity_fraction: float = 1.0,
        apply_hk_fees: bool = False,
        apply_cn_fees: bool = False,
    ) -> None:
        if starting_cash <= 0:
            raise ValueError("starting_cash must be positive")
        if commission_per_order < 0:
            raise ValueError("commission_per_order must be >= 0")
        if slippage_bps < 0:
            raise ValueError("slippage_bps must be >= 0")
        if not 0 < liquidity_fraction <= 1:
            raise ValueError("liquidity_fraction must be in (0, 1]")

        base_currency = base_currency.upper()
        balances = {
            key.upper(): float(value)
            for key, value in (starting_cash_by_currency or {base_currency: starting_cash}).items()
        }
        if not balances or any(value < 0 for value in balances.values()):
            raise ValueError("starting_cash_by_currency must contain non-negative balances")
        self.base_currency = base_currency
        self.starting_cash_by_currency = dict(balances)
        self.starting_cash = balances.get(base_currency, starting_cash)
        self._fx_to_base = {
            "USD": 1.0,
            "HKD": 1.0 / 7.8,
            "CNY": 1.0 / 7.2,
            "KRW": 1.0 / 1380.0,
            **{key.upper(): float(value) for key, value in (fx_to_base or {}).items()},
        }
        self._fx_to_base[base_currency] = 1.0
        self.commission_per_order = commission_per_order
        self.slippage_bps = slippage_bps
        self.liquidity_fraction = liquidity_fraction
        # When True, HKD fills also bear the statutory + HKEX charges (stamp
        # duty, levies, trading fee, CCASS) so paper P&L reflects real HK cost;
        # off by default keeps the deterministic test oracle's round numbers.
        self.apply_hk_fees = apply_hk_fees
        # Estimated mainland retail costs for PAPER attribution only: 0.025%
        # commission with a CNY 5 minimum, plus 0.05% sell stamp duty on stocks
        # (listed funds are exempt). A real broker fill remains authoritative.
        self.apply_cn_fees = apply_cn_fees

        self._cash_by_currency: dict[str, float] = dict(balances)
        self._day_open_equity: float = sum(
            value * self._fx_to_base.get(currency, 0.0)
            for currency, value in balances.items()
        )
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

    @property
    def _cash(self) -> float:
        """Backward-compatible base-currency cash view."""
        return self._cash_by_currency.get(self.base_currency, 0.0)

    @_cash.setter
    def _cash(self, value: float) -> None:
        self._cash_by_currency[self.base_currency] = value

    def set_fx_rate(self, currency: str, base_per_unit: float) -> None:
        if base_per_unit <= 0:
            raise ValueError("FX rate must be positive")
        self._fx_to_base[currency.upper()] = float(base_per_unit)

    def _mark_for(self, pos: Position) -> float:
        return pos.mkt_px if pos.mkt_px is not None else pos.avg_px

    def _equity_by_currency(self) -> dict[str, float]:
        values = dict(self._cash_by_currency)
        for pos in self._positions.values():
            values[pos.currency] = values.get(pos.currency, 0.0) + pos.qty * self._mark_for(pos)
        return values

    def _equity(self) -> float:
        values = self._equity_by_currency()
        return sum(
            value * self._fx_to_base.get(currency, 0.0)
            for currency, value in values.items()
        )

    def _total_reserved(self, currency: Optional[str] = None) -> float:
        if currency is None:
            return sum(self._reserved.values())
        return sum(
            value
            for order_id, value in self._reserved.items()
            if self._orders.get(order_id, None) is not None
            and self._orders[order_id].currency == currency
        )

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
            consideration = remaining * self._buy_ref_px[order.id]
            self._reserved[order.id] = (
                consideration
                + self._fees(order.currency, consideration, Side.BUY, order.symbol)
            )

    def _fees(
        self, currency: str, consideration: float, side: Side, symbol: str
    ) -> float:
        """Estimated commission/statutory fees, also used for BUY reservation.

        SEHK charges supplement the configured flat commission. Mainland paper
        trades use an explicit local-currency estimate instead of interpreting
        the default ``1.0`` as one US dollar.

        Used BOTH for
        the BUY cash reservation (on ref/limit price — conservative, so the
        reservation always covers the fill's fee) and the fill deduction, so the
        currency sleeve can never over-commit (cash < 0)."""
        if consideration <= 0:
            return 0.0
        if self.apply_cn_fees and currency == "CNY":
            broker_commission = max(5.0, consideration * 0.00025)
            stamp = (
                consideration * 0.0005
                if side is Side.SELL and not is_listed_fund(symbol)
                else 0.0
            )
            return broker_commission + stamp
        if self.apply_hk_fees and currency == "HKD" and consideration > 0:
            from swing_trader.hk_fees import compute_hk_fees

            return self.commission_per_order + float(compute_hk_fees(consideration).total)
        return self.commission_per_order

    def _cn_bought_today(self, symbol: str, at: datetime) -> float:
        if not is_cn_symbol(symbol):
            return 0.0
        local_day = at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        return sum(
            fill.qty
            for fill in self._fills
            if fill.symbol == symbol
            and fill.side is Side.BUY
            and fill.ts.astimezone(ZoneInfo("Asia/Shanghai")).date() == local_day
        )

    def _cn_sellable_qty(self, symbol: str, at: datetime) -> float:
        pos = self._positions.get(symbol)
        return max(0.0, (pos.qty if pos else 0.0) - self._cn_bought_today(symbol, at))

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
            if is_cn_symbol(stored.symbol):
                qty_reason = quantity_violation(
                    stored.qty, stored.side, held_qty=pos.qty
                )
                if qty_reason:
                    return reject(f"CN order quantity invalid: {qty_reason}")
                sellable = self._cn_sellable_qty(stored.symbol, stored.ts)
                if stored.qty > sellable + _EPS:
                    return reject(
                        f"T+1 restriction: only {sellable:g} settled shares/units "
                        f"of {stored.symbol} are sellable today"
                    )
            available = pos.qty - self._reserved_sell_qty(stored.symbol)
            if stored.qty > available + _EPS:
                return reject(
                    f"insufficient unreserved position in {stored.symbol}: "
                    f"available {available:g}, requested {stored.qty:g}"
                )
        else:  # BUY: reserve cash
            if is_cn_symbol(stored.symbol):
                qty_reason = quantity_violation(stored.qty, stored.side)
                if qty_reason:
                    return reject(f"CN order quantity invalid: {qty_reason}")
            ref_px = self._buy_reference_px(stored)
            if ref_px is None:
                return reject(f"no reference price for {stored.symbol} MOC order")
            reservation = (
                stored.qty * ref_px
                + self._fees(
                    stored.currency, stored.qty * ref_px, Side.BUY, stored.symbol
                )
            )
            currency_cash = self._cash_by_currency.get(stored.currency, 0.0)
            reserved = self._total_reserved(stored.currency)
            if reserved + reservation > currency_cash + _EPS:
                return reject(
                    f"insufficient cash ({stored.currency}): need {reservation:.2f} reserved, "
                    f"already reserved {reserved:.2f}, cash {currency_cash:.2f}"
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

    def step(
        self,
        bars: dict[str, Bar],
        *,
        execution_ts: Optional[datetime] = None,
    ) -> list[Fill]:
        """Advance one bar per symbol; fill resting orders; update marks.

        Orders activated mid-step (bracket children) become fillable from
        the next bar, not the bar that filled their parent.  ``execution_ts``
        records the real callback instant for runtime audit; deterministic
        simulations may omit it and retain the candle timestamp.
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
                if is_cn_symbol(order.symbol):
                    qty = min(
                        qty,
                        self._cn_sellable_qty(order.symbol, execution_ts or bar.ts),
                    )
            if qty <= _EPS:
                continue
            fills.append(self._apply_fill(order, qty, px, bar, execution_ts=execution_ts))
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

    def _apply_fill(
        self,
        order: Order,
        qty: float,
        px: float,
        bar: Bar,
        *,
        execution_ts: Optional[datetime] = None,
    ) -> Fill:
        # SEHK statutory + exchange charges on this fill's consideration are
        # lumped into the fill commission for the paper sim (broker vs statutory
        # reconciliation happens once real IBKR fills exist). The BUY reservation
        # covered these on the ref/limit price, so the sleeve never goes negative.
        commission = self._fees(order.currency, qty * px, order.side, order.symbol)
        fill = Fill(
            # ``bar.ts`` is the bar START (09:30 for a daily Yahoo candle),
            # not the moment the 16:00 close callback executed. Runtime calls
            # pass execution_ts so the audit cannot show a fill before its
            # order; direct deterministic simulations retain bar.ts by default.
            ts=execution_ts or bar.ts,
            order_id=order.id,
            symbol=order.symbol,
            currency=order.currency,
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
            self._cash_by_currency[order.currency] = (
                self._cash_by_currency.get(order.currency, 0.0) - qty * px - commission
            )
            pos = self._positions.get(order.symbol)
            if pos is None:
                self._positions[order.symbol] = Position(
                    symbol=order.symbol, currency=order.currency,
                    qty=qty, avg_px=px, mkt_px=px
                )
            else:  # weighted average entry price
                total = pos.qty + qty
                pos.avg_px = (pos.avg_px * pos.qty + px * qty) / total
                pos.qty = total
            self._update_buy_reservation(order)
        else:
            self._cash_by_currency[order.currency] = (
                self._cash_by_currency.get(order.currency, 0.0) + qty * px - commission
            )
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
        cash: float | dict[str, float],
        positions: list[Position],
        orders: list[Order],
        day_open_equity: Optional[float] = None,
        fills: Optional[list[Fill]] = None,
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
        self._fills = [fill.model_copy(deep=True) for fill in (fills or [])]
        self._cash_by_currency = (
            {self.base_currency: float(cash)}
            if isinstance(cash, (int, float))
            else {key.upper(): float(value) for key, value in cash.items()}
        )
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
                remaining * ref
                + self._fees(order.currency, remaining * ref, Side.BUY, order.symbol)
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
                "cash_by_currency": {
                    key: round(value, 2) for key, value in self._cash_by_currency.items()
                },
                "n_positions": len(self._positions),
                "n_orders": len(self._orders),
                "n_warnings": len(warnings),
            },
        )
        return warnings

    def start_of_day(self) -> None:
        """Reset the day-open equity anchor to current equity."""
        self._day_open_equity = self._equity()

    def end_of_day(self, currency: Optional[str] = None) -> None:
        """Expire resting DAY orders for one sleeve (or all when omitted)."""
        scope = currency.upper() if currency else None
        for order in self._orders.values():
            if scope is not None and order.currency != scope:
                continue
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
            (self._mark_for(pos) - pos.avg_px)
            * pos.qty
            * self._fx_to_base.get(pos.currency, 0.0)
            for pos in self._positions.values()
        )
        day_pnl = equity - self._day_open_equity
        if self._day_open_equity > _EPS:
            drawdown_pct = min(0.0, day_pnl / self._day_open_equity * 100.0)
        else:  # pragma: no cover - defensive; equity starts positive
            drawdown_pct = 0.0
        return AccountSnapshot(
            mode=Mode.PAPER,
            equity=equity,
            cash=sum(
                value * self._fx_to_base.get(currency, 0.0)
                for currency, value in self._cash_by_currency.items()
            ),
            upnl=upnl,
            day_pnl=day_pnl,
            drawdown_pct=drawdown_pct,
            breaker_state=BreakerState.NORMAL,  # breaker decided by the RiskEngine
            base_currency=self.base_currency,
            cash_by_currency=dict(self._cash_by_currency),
            equity_by_currency=self._equity_by_currency(),
            fx_to_base=dict(self._fx_to_base),
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
