"""Currency-scoped view over a shared multi-currency broker.

US, HK and mainland paper sessions share one durable PaperBroker, but a market
loop must not size a CNY trade from USD cash, expire another market's DAY
orders, or sync another market's fills.  This adapter exposes one currency
sleeve while delegating mutations to the same underlying broker.
"""

from __future__ import annotations

from swing_trader.interfaces import Bar, BrokerInterface, PlaceResult
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    Fill,
    Order,
    OrderStatus,
    Position,
)

__all__ = ["CurrencyBrokerView"]

_EPS = 1e-9


class CurrencyBrokerView(BrokerInterface):
    def __init__(self, broker: BrokerInterface, currency: str) -> None:
        self._broker = broker
        self.currency = currency.upper()
        self._day_open_equity = self._local_equity()

    @property
    def mode(self):
        return self._broker.get_account().mode

    def _local_equity(self) -> float:
        return self._broker.get_account().equity_by_currency.get(self.currency, 0.0)

    def get_account(self) -> AccountSnapshot:
        raw = self._broker.get_account()
        equity = raw.equity_by_currency.get(self.currency, 0.0)
        cash = raw.cash_by_currency.get(self.currency, 0.0)
        upnl = sum(
            ((pos.mkt_px if pos.mkt_px is not None else pos.avg_px) - pos.avg_px)
            * pos.qty
            for pos in self.get_positions()
        )
        day_pnl = equity - self._day_open_equity
        drawdown = (
            min(0.0, day_pnl / self._day_open_equity * 100.0)
            if self._day_open_equity > _EPS
            else 0.0
        )
        return AccountSnapshot(
            mode=raw.mode,
            equity=equity,
            cash=cash,
            upnl=upnl,
            day_pnl=day_pnl,
            drawdown_pct=drawdown,
            breaker_state=BreakerState.NORMAL,
            base_currency=self.currency,
            cash_by_currency={self.currency: cash},
            equity_by_currency={self.currency: equity},
            fx_to_base={self.currency: 1.0},
        )

    def get_positions(self) -> list[Position]:
        return [p for p in self._broker.get_positions() if p.currency == self.currency]

    def place_order(self, order: Order) -> PlaceResult:
        if order.currency != self.currency:
            rejected = order.model_copy(update={"status": OrderStatus.REJECTED})
            return PlaceResult(
                accepted=False,
                order=rejected,
                reason=f"{self.currency} broker view refuses {order.currency} order",
            )
        return self._broker.place_order(order)

    def cancel_order(self, order_id: str) -> bool:
        if not any(order.id == order_id for order in self.get_orders()):
            return False
        return self._broker.cancel_order(order_id)

    def get_orders(self, active_only: bool = False) -> list[Order]:
        return [
            order
            for order in self._broker.get_orders(active_only=active_only)
            if order.currency == self.currency
        ]

    def get_fills(self) -> list[Fill]:
        return [fill for fill in self._broker.get_fills() if fill.currency == self.currency]

    def start_of_day(self) -> None:
        self._day_open_equity = self._local_equity()

    def end_of_day(self) -> None:
        end = getattr(self._broker, "end_of_day")
        try:
            end(currency=self.currency)
        except TypeError:
            end()

    def step(
        self,
        bars: dict[str, Bar],
        *,
        execution_ts=None,
    ) -> list[Fill]:
        scoped = {
            symbol: bar
            for symbol, bar in bars.items()
            if any(
                row.symbol == symbol
                for row in [*self.get_orders(active_only=True), *self.get_positions()]
            )
        }
        if not scoped:
            return []
        return self._broker.step(scoped, execution_ts=execution_ts)
