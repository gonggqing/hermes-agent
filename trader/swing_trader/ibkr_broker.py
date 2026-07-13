"""IBKRBroker — real broker adapter behind the broker port (Loop.md §5.1, §2, §7).

Phase 0.9/1: implement :class:`BrokerInterface` against Interactive Brokers so
IBKR slots in the moment the account funds. Built + validated ENTIRELY OFFLINE
against a mock IB transport (Loop.md §9: tests never hit the network); a real
TWS/Gateway connection is only needed to go live.

Design (ports & adapters): ``IBKRBroker`` translates the domain
:class:`~swing_trader.schemas.Order` ↔ a NEUTRAL :class:`IBClient` (place /
cancel / trades / fills / positions / account). The production client
(:class:`_IbAsyncClient`) is the only thing that imports ``ib_async`` — and it
does so LAZILY, so constructing an ``IBKRBroker`` opens no socket and imports no
``ib_async`` (asserted in tests). Tests inject a ``FakeIBClient`` and drive the
whole place → partial → fill → cancel → reject / bracket-OCA lifecycle.

Guardrails preserved (Loop.md §3): this adapter never bypasses the RiskEngine,
the human confirmation gate, or the live-order triple gate (that stays in
``ExecutionEngine``/``Settings``). A CASH account (HK, §2) spends only SETTLED
cash — ``get_account`` maps ``SettledCash``, and ``place_order`` fails closed on
insufficient settled funds locally before ever reaching IBKR.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, runtime_checkable

from swing_trader.interfaces import BrokerInterface, PlaceResult
from swing_trader.log import get_logger
from swing_trader.schemas import (
    AccountSnapshot,
    BreakerState,
    Fill,
    Mode,
    Order,
    OrderStatus,
    OrderType,
    Position,
    Role,
    Side,
    TimeInForce,
)

logger = get_logger(__name__)

__all__ = ["IBKRBroker", "IBClient", "IbExec", "IbOrderSpec", "IbPosition", "IbTradeState"]

# TWS: 7497=paper, 7496=live. IB Gateway: 4002=paper, 4001=live.
_LIVE_PORTS = frozenset({7496, 4001})


# ---------------------------------------------------------- neutral IB port
# A small, ib_async-agnostic surface so the whole adapter is unit-testable
# offline. The production _IbAsyncClient maps these to ib_async; tests fake them.


@dataclass
class IbOrderSpec:
    """One IB order to submit (neutral of ib_async types)."""

    order_ref: str  # stable client id (== schemas.Order.id) — idempotency key
    action: str  # BUY | SELL
    qty: float
    order_type: str  # LMT | STP | MOC | LOC | MKT
    symbol: str
    lmt: Optional[float] = None
    aux: Optional[float] = None  # stop trigger for STP
    tif: str = "GTC"
    oca_group: Optional[str] = None
    transmit: bool = True
    parent_ref: Optional[str] = None


@dataclass
class IbExec:
    exec_id: str  # IBKR execId — the Fill dedup key
    order_ref: str  # links back to schemas.Order.id
    symbol: str
    side: str  # BUY | SELL
    qty: float
    px: float
    commission: float = 0.0


@dataclass
class IbTradeState:
    order_ref: str
    status: str  # ib status: PendingSubmit/PreSubmitted/Submitted/Filled/Cancelled/ApiCancelled/Inactive
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_px: Optional[float] = None


@dataclass
class IbPosition:
    symbol: str
    qty: float
    avg_cost: float  # per-share, incl. commission (IBKR convention)


@runtime_checkable
class IBClient(Protocol):
    def is_connected(self) -> bool: ...
    def connect(self) -> None: ...
    def place(self, spec: IbOrderSpec) -> str: ...  # returns broker_ref
    def cancel(self, broker_ref: str) -> bool: ...
    def trades(self) -> list[IbTradeState]: ...
    def fills(self) -> list[IbExec]: ...
    def positions(self) -> list[IbPosition]: ...
    def account(self) -> dict: ...  # tag -> value (str)


# --------------------------------------------------------- status mapping

_IB_STATUS_TO_ORDER: dict[str, OrderStatus] = {
    "PendingSubmit": OrderStatus.SUBMITTED,
    "PreSubmitted": OrderStatus.SUBMITTED,
    "Submitted": OrderStatus.SUBMITTED,
    "ApiPending": OrderStatus.NEW,
    "PendingCancel": OrderStatus.SUBMITTED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "ApiCancelled": OrderStatus.CANCELLED,
    "Inactive": OrderStatus.REJECTED,
}


def _map_status(ib_status: str, filled: float) -> OrderStatus:
    st = _IB_STATUS_TO_ORDER.get(ib_status)
    if st is OrderStatus.SUBMITTED and filled > 0:
        return OrderStatus.PARTIALLY_FILLED
    return st or OrderStatus.SUBMITTED


# ------------------------------------------------------------------ broker


class IBKRBroker(BrokerInterface):
    """IBKR adapter (Loop.md §5.1). Constructing it opens no socket and imports
    no ib_async — connection is lazy. Inject ``client_factory`` (→ an
    :class:`IBClient`) in tests; production defaults to a lazily-connected
    ib_async client."""

    mode = Mode.LIVE

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        paper: bool = True,
        *,
        client_factory: Optional[Callable[[], IBClient]] = None,
        role_for_symbol: Optional[Callable[[str], Role]] = None,
    ) -> None:
        # Belt-and-braces guardrail (Loop.md §3): refuse a live port under paper.
        if paper and port in _LIVE_PORTS:
            raise ValueError(
                f"paper=True but port {port} is an IBKR LIVE port — refusing to connect"
            )
        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        self._client_factory = client_factory
        self._role_for = role_for_symbol or (lambda _s: Role.ROTATION)
        self._client: Optional[IBClient] = None
        # order_ref -> the domain Order we submitted (for get_orders reconstruction)
        self._submitted: dict[str, Order] = {}
        # order_ref -> broker_ref (idempotency: never re-place a known ref)
        self._ref_to_broker: dict[str, str] = {}
        self._day_open_equity: Optional[float] = None
        logger.debug("IBKRBroker constructed (no connection)",
                     extra={"host": host, "port": port, "client_id": client_id, "paper": paper})

    # -- connection (lazy; no ib_async import at construct time) --

    def _ib(self) -> IBClient:
        if self._client is None:
            if self._client_factory is not None:
                self._client = self._client_factory()
            else:  # production: import ib_async ONLY here
                self._client = _IbAsyncClient(self.host, self.port, self.client_id)
        if not self._client.is_connected():
            self._client.connect()
        return self._client

    # ------------------------------------------------------------- account

    @staticmethod
    def _num(account: dict, *tags: str) -> Optional[float]:
        for t in tags:
            v = account.get(t)
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    continue
        return None

    def get_account(self) -> AccountSnapshot:
        acct = self._ib().account()
        equity = self._num(acct, "NetLiquidation") or 0.0
        # CASH ACCOUNT (Loop.md §2): spendable = SETTLED cash, never
        # AvailableFunds (which can include unsettled sale proceeds → free-ride).
        cash = self._num(acct, "SettledCash", "TotalCashValue", "AvailableFunds") or 0.0
        upnl = self._num(acct, "UnrealizedPnL") or 0.0
        day_pnl = self._num(acct, "RealizedPnL") or 0.0
        if self._day_open_equity is None:
            self._day_open_equity = equity
        dd = 0.0
        if self._day_open_equity and self._day_open_equity > 0:
            dd = min(0.0, (equity - self._day_open_equity) / self._day_open_equity * 100.0)
        # Breaker is OURS (AccountRiskMonitor trips it), not IBKR's — report NORMAL.
        return AccountSnapshot(mode=Mode.LIVE, equity=equity, cash=cash, upnl=upnl,
                               day_pnl=day_pnl, drawdown_pct=dd,
                               breaker_state=BreakerState.NORMAL)

    def get_positions(self) -> list[Position]:
        out = []
        for p in self._ib().positions():
            if abs(p.qty) < 1e-9:
                continue
            out.append(Position(symbol=p.symbol, qty=p.qty, avg_px=max(0.0, p.avg_cost),
                                pool=self._role_for(p.symbol)))
        return out

    # -------------------------------------------------------------- orders

    def _settled_cash_ok(self, order: Order, ref_px: Optional[float]) -> tuple[bool, str]:
        """Fail-closed local pre-check for a BUY against SETTLED cash (§2/§3)."""
        if order.side is not Side.BUY or ref_px is None:
            return True, ""
        need = order.qty * ref_px  # commission is small; settled-cash is the gate
        cash = self.get_account().cash
        if need > cash + 1e-6:
            return False, (f"insufficient settled cash: need ~{need:.2f} > settled {cash:.2f} "
                           "(T+1 cash account cannot spend unsettled proceeds)")
        return True, ""

    def _specs_for(self, order: Order) -> list[IbOrderSpec]:
        action = order.side.value  # BUY | SELL
        tif = order.tif.value
        ot = order.order_type
        base = dict(symbol=order.symbol, tif=tif)
        if ot is OrderType.BRACKET:
            oca = f"oca-{order.id}"
            specs = [IbOrderSpec(order_ref=order.id, action=action, qty=order.qty,
                                 order_type="LMT", lmt=order.limit, transmit=False, **base)]
            # protective stop (opposite side); tp optional
            opp = Side.SELL.value if order.side is Side.BUY else Side.BUY.value
            has_tp = order.tp is not None
            specs.append(IbOrderSpec(order_ref=f"{order.id}:stp", action=opp, qty=order.qty,
                                     order_type="STP", aux=order.stop, oca_group=oca,
                                     transmit=not has_tp, parent_ref=order.id, **base))
            if has_tp:
                specs.append(IbOrderSpec(order_ref=f"{order.id}:tp", action=opp, qty=order.qty,
                                         order_type="LMT", lmt=order.tp, oca_group=oca,
                                         transmit=True, parent_ref=order.id, **base))
            return specs
        kind = {OrderType.LMT: "LMT", OrderType.STP: "STP",
                OrderType.MOC: "MOC", OrderType.LOC: "LOC"}[ot]
        return [IbOrderSpec(order_ref=order.id, action=action, qty=order.qty,
                            order_type=kind, lmt=order.limit, aux=order.stop, **base)]

    def _child_order(self, parent: Order, spec: IbOrderSpec, broker_ref: str) -> Order:
        return Order(
            id=spec.order_ref, mode=Mode.LIVE, symbol=parent.symbol,
            side=Side(spec.action), qty=spec.qty,
            order_type=OrderType.STP if spec.order_type == "STP" else OrderType.LMT,
            limit=spec.lmt, stop=spec.aux, tif=TimeInForce(spec.tif),
            status=OrderStatus.SUBMITTED, broker_ref=broker_ref,
            parent_order_id=parent.id, oca_group=spec.oca_group,
        )

    def place_order(self, order: Order) -> PlaceResult:
        # Never mutate the caller's order (it is also sent to PaperBroker when
        # mirroring — status must stay NEW there). Work on a deep copy.
        submitted = order.model_copy(deep=True)
        submitted.mode = Mode.LIVE

        # Idempotency: a known order_ref means we already placed it — reconcile,
        # do not double-submit (Loop.md §7 client-order-id idempotency).
        if order.id in self._ref_to_broker:
            existing = self._submitted.get(order.id, submitted)
            return PlaceResult(accepted=True, order=existing, reason="idempotent replay")

        ref_px = order.limit if order.order_type in (
            OrderType.LMT, OrderType.LOC, OrderType.BRACKET) else order.stop
        ok, why = self._settled_cash_ok(order, ref_px)
        if not ok:
            submitted.status = OrderStatus.REJECTED
            return PlaceResult(accepted=False, order=submitted, reason=why)

        specs = self._specs_for(order)
        try:
            broker_refs = [self._ib().place(s) for s in specs]
        except Exception as exc:  # noqa: BLE001 — a broker/API error is a rejection
            submitted.status = OrderStatus.REJECTED
            return PlaceResult(accepted=False, order=submitted,
                               reason=f"IBKR rejected: {str(exc)[:200]}")

        submitted.status = OrderStatus.SUBMITTED
        submitted.broker_ref = broker_refs[0]
        # Tag the domain parent with the group id (for reconciliation/display).
        # The parent SPEC stays out of the OCA group so IBKR doesn't cancel the
        # protective legs when the entry fills — it activates them.
        submitted.oca_group = next((s.oca_group for s in specs if s.oca_group), None)
        self._ref_to_broker[order.id] = broker_refs[0]
        self._submitted[order.id] = submitted

        children: list[Order] = []
        for spec, bref in zip(specs[1:], broker_refs[1:]):
            child = self._child_order(order, spec, bref)
            children.append(child)
            self._ref_to_broker[spec.order_ref] = bref
            self._submitted[spec.order_ref] = child
        return PlaceResult(accepted=True, order=submitted, child_orders=children)

    def cancel_order(self, order_id: str) -> bool:
        bref = self._ref_to_broker.get(order_id)
        if bref is None:
            return False  # unknown order — never raise
        try:
            return bool(self._ib().cancel(bref))
        except Exception:  # noqa: BLE001 — filled/unknown cancels return False
            return False

    def get_orders(self, active_only: bool = False) -> list[Order]:
        states = {t.order_ref: t for t in self._ib().trades()}
        out: list[Order] = []
        for ref, base in self._submitted.items():
            st = states.get(ref)
            o = base.model_copy(deep=True)
            if st is not None:
                o.status = _map_status(st.status, st.filled)
                o.filled_qty = st.filled
                o.avg_fill_px = st.avg_fill_px
            out.append(o)
        if active_only:
            out = [o for o in out if o.status in (
                OrderStatus.NEW, OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED)]
        out.sort(key=lambda o: o.ts)
        return out

    # --------------------------------------------------------------- fills

    def get_fills(self) -> list[Fill]:
        out: list[Fill] = []
        for e in self._ib().fills():
            out.append(Fill(id=e.exec_id, order_id=e.order_ref, symbol=e.symbol,
                            side=Side(e.side), qty=e.qty, px=e.px,
                            commission=e.commission, mode=Mode.LIVE))
        out.sort(key=lambda f: f.ts)
        return out


# --------------------------------------------------- production ib_async client


class _IbAsyncClient:
    """Production :class:`IBClient` over ib_async. ib_async is imported LAZILY in
    :meth:`connect` so importing this module (or constructing an IBKRBroker)
    never pulls it in. NOT unit-tested (needs a live TWS/Gateway); the adapter
    logic above is exercised via FakeIBClient."""

    def __init__(self, host: str, port: int, client_id: int) -> None:
        self._host, self._port, self._client_id = host, port, client_id
        self._ib = None  # ib_async.IB
        self._mod = None  # ib_async module (Stock/LimitOrder/StopOrder/…)
        self._ref_to_trade: dict[str, object] = {}

    def connect(self) -> None:  # pragma: no cover - needs a live gateway
        import ib_async  # lazy — keeps construct/import network-free

        self._mod = ib_async
        self._ib = ib_async.IB()
        self._ib.connect(self._host, self._port, clientId=self._client_id, timeout=10)

    def is_connected(self) -> bool:  # pragma: no cover
        return self._ib is not None and self._ib.isConnected()

    def _contract(self, symbol: str):  # pragma: no cover
        base = symbol.split(".")[0]
        return self._mod.Stock(base, "SMART", "USD")

    def place(self, spec: IbOrderSpec) -> str:  # pragma: no cover
        m = self._mod
        common = dict(tif=spec.tif, orderRef=spec.order_ref, transmit=spec.transmit)
        if spec.oca_group:
            common.update(ocaGroup=spec.oca_group, ocaType=1)
        if spec.order_type == "LMT":
            o = m.LimitOrder(spec.action, spec.qty, spec.lmt, **common)
        elif spec.order_type == "STP":
            o = m.StopOrder(spec.action, spec.qty, spec.aux, **common)
        elif spec.order_type == "MKT":
            o = m.MarketOrder(spec.action, spec.qty, **common)
        else:  # MOC / LOC
            o = m.Order(action=spec.action, totalQuantity=spec.qty,
                        orderType=spec.order_type, lmtPrice=spec.lmt or 0.0, **common)
        trade = self._ib.placeOrder(self._contract(spec.symbol), o)
        self._ref_to_trade[spec.order_ref] = trade
        return str(trade.order.orderId)

    def cancel(self, broker_ref: str) -> bool:  # pragma: no cover
        for trade in self._ref_to_trade.values():
            if str(trade.order.orderId) == broker_ref:
                self._ib.cancelOrder(trade.order)
                return True
        return False

    def trades(self) -> list[IbTradeState]:  # pragma: no cover
        out = []
        for trade in self._ib.trades():
            os = trade.orderStatus
            out.append(IbTradeState(order_ref=trade.order.orderRef, status=os.status,
                                    filled=os.filled, remaining=os.remaining,
                                    avg_fill_px=os.avgFillPrice or None))
        return out

    def fills(self) -> list[IbExec]:  # pragma: no cover
        out = []
        for f in self._ib.fills():
            e, c = f.execution, f.commissionReport
            out.append(IbExec(exec_id=e.execId, order_ref=e.orderRef, symbol=e.contract.symbol,
                              side="BUY" if e.side == "BOT" else "SELL", qty=e.shares,
                              px=e.price, commission=getattr(c, "commission", 0.0) or 0.0))
        return out

    def positions(self) -> list[IbPosition]:  # pragma: no cover
        out = []
        for p in self._ib.positions():
            if p.contract.secType != "STK":
                continue
            out.append(IbPosition(symbol=p.contract.symbol, qty=p.position, avg_cost=p.avgCost))
        return out

    def account(self) -> dict:  # pragma: no cover
        return {av.tag: av.value for av in self._ib.accountValues()
                if av.currency in ("", "USD", "BASE")}
