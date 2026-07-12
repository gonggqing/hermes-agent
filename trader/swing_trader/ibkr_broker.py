"""IBKRBroker — Phase 1 stub behind the broker port (Loop.md §5.1, §2, §7).

Loop.md §2: "IBKR not opened yet ⇒ Phase 0 runs entirely on a PaperBroker
backend + free market data; IBKR is a stub behind an interface (instrumented
for later)." This module is that stub (backlog item 15): it satisfies
:class:`swing_trader.interfaces.BrokerInterface` so the core wiring can select
it via config today, but every method raises ``NotImplementedError`` until
Phase 1 implements it with ``ib_async`` (Loop.md §7 Phase 1).

The constructor only STORES connection parameters — it opens no socket,
imports no ``ib_async``, and touches no network (Loop.md §3: tests never hit
the network).

Phase 1 acceptance checklist (Loop.md §7 Phase 1)
-------------------------------------------------
- [ ] Connect to IBKR **paper** (TWS paper port 7497 / IB Gateway paper port
      4002) with a stable ``client_id``; survive TWS restarts (reconnect).
- [ ] Mirror **20 paper trades**: every order approved through the daily loop
      is placed on BOTH PaperBroker and IBKR paper, ledger rows mode-tagged
      so the comparison is exact (Loop.md §6 paper=live schema parity).
- [ ] Compare fills vs the PaperBroker sim: per-trade and aggregate
      **slippage** (fill px vs sim px, bps), **fill quality** (fill rate,
      partial-fill frequency, time-to-fill), commission deltas — the
      sim→real gap stats required by Loop.md §7 Phase 1.
- [ ] No guardrail breaches: live placement still requires
      ``Settings.live_orders_allowed`` (HUMAN_CONFIRM ∧ BROKER != paper ∧
      ¬DRY_RUN, Loop.md §3/§9); RiskEngine remains authoritative.
- [ ] Order lifecycle covered against a mock IB (place → partial → fill →
      cancel → reject) without network (Loop.md §9).
"""

from __future__ import annotations

from swing_trader.interfaces import BrokerInterface, PlaceResult
from swing_trader.log import get_logger
from swing_trader.schemas import AccountSnapshot, Fill, Order, Position

logger = get_logger(__name__)

# --------------------------------------------------------------------------
# TODO(Phase 1) — connection lifecycle (applies to the whole adapter):
#
# * Use `ib_async.IB()` and `await ib.connectAsync(host, port,
#   clientId=client_id, timeout=...)`. The sync wrapper `ib.connect()` exists
#   but the daily loop is scheduler-driven, so prefer the async API and run
#   it inside the scheduler's event loop.
# * clientId collisions: TWS/Gateway rejects a second session with the same
#   clientId ("client id is already in use", error 326). Reserve a fixed id
#   per process role (e.g. 1 = execution, 2 = reporter) and surface a clear
#   error instead of retrying with the same id.
# * Reconnect: TWS restarts nightly and Gateway auto-restarts weekly. Handle
#   `ib.disconnectedEvent` -> exponential backoff reconnect; on reconnect,
#   re-sync state via `ib.reqOpenOrders()` / `ib.reqExecutions()` before
#   trusting local caches. Resting GTC orders survive on the IBKR side, so
#   reconciliation (broker_ref -> Order) is mandatory, not optional.
# * Ports: 7497 = TWS **paper**, 7496 = TWS **live**; IB Gateway uses
#   4002 (paper) / 4001 (live). The `paper` flag must be cross-checked
#   against the port at connect time — refuse to connect when paper=True
#   but the port is a live port (guardrail belt-and-braces, Loop.md §3).
# * Pacing: the API allows ~45-50 requests/sec (violations -> error 100
#   "max rate of messages per second exceeded", and repeated violations can
#   disconnect the session). Historical data has its own pacing rules
#   (identical requests within 15s, ~60 requests / 10 min -> pacing
#   violation). Funnel all requests through a rate limiter.
# * Market data: an HK-based (IBKR Hong Kong) account needs explicit US
#   market-data subscriptions (e.g. "US Securities Snapshot and Futures
#   Value Bundle" / NASDAQ-NYSE-AMEX non-professional) before
#   `reqMktData`/`reqHistoricalData` return live data; otherwise expect
#   error 354 ("not subscribed") or 15-min delayed data. Either buy the
#   subscription, or request delayed data explicitly via
#   `ib.reqMarketDataType(3)` and tag quotes as delayed. Quotes/bars belong
#   on the DataFeed port (IBKRFeed) per the ports-and-adapters split — this
#   broker adapter should only need order/account endpoints.
# --------------------------------------------------------------------------


def _todo(method: str) -> NotImplementedError:
    """Uniform Phase 1 stub error (message asserted in tests)."""
    return NotImplementedError(
        f"TODO(Phase 1): IBKRBroker.{method} — implement with ib_async; "
        "see Loop.md section 7 Phase 1"
    )


class IBKRBroker(BrokerInterface):
    """Stub IBKR adapter (Loop.md §5.1). Stores connection params; every
    method raises ``NotImplementedError`` until Phase 1 wires in ``ib_async``.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 1,
        paper: bool = True,
    ) -> None:
        # Store-only: no socket, no ib_async import, no network (Loop.md §3).
        self.host = host
        self.port = port
        self.client_id = client_id
        self.paper = paper
        logger.debug(
            "IBKRBroker stub constructed (no connection attempted)",
            extra={"host": host, "port": port, "client_id": client_id, "paper": paper},
        )

    # ------------------------------------------------------------- account

    def get_account(self) -> AccountSnapshot:
        # TODO(Phase 1): map `ib.accountSummary()` tags to AccountSnapshot:
        #   equity   <- NetLiquidation
        #   cash     <- TotalCashValue
        #   upnl     <- UnrealizedPnL
        #   day_pnl  <- via `ib.reqPnL()` (dailyPnL) or snapshot diffs
        # T+1 CASH settlement (Loop.md §2): a cash account must NOT spend
        # unsettled sale proceeds. Track `SettledCash` separately from
        # `AvailableFunds` — after a SELL, AvailableFunds may include the
        # proceeds before they settle at T+1; sizing new BUYs off it would
        # let the account free-ride (violation -> 90-day cash-up-front
        # restriction). Spendable cash for the RiskEngine = SettledCash,
        # never AvailableFunds. drawdown_pct/breaker_state stay computed
        # locally against day-open equity (breaker is ours, not IBKR's).
        raise _todo("get_account")

    def get_positions(self) -> list[Position]:
        # TODO(Phase 1): `ib.positions()` -> [Position]. Map
        # `position.contract.symbol` (filter secType == "STK", currency
        # "USD"), `position.position` -> qty, `position.avgCost` -> avg_px
        # (note: avgCost includes commission and is per-share for stocks).
        # mkt_px comes from the DataFeed port or `ib.reqPnLSingle`. The
        # `pool` role tag is OURS (watchlist metadata), not IBKR's — join
        # against swing_trader.watchlist when building the Position.
        raise _todo("get_positions")

    # -------------------------------------------------------------- orders

    def place_order(self, order: Order) -> PlaceResult:
        # TODO(Phase 1): translate schemas.Order -> ib_async order objects:
        # * OrderType.LMT -> LimitOrder(action, qty, order.limit)
        # * OrderType.STP -> StopOrder(action, qty, order.stop)
        # * OrderType.MOC -> Order(orderType="MOC")  (submit before ~15:50 ET
        #   NYSE / 15:55 Nasdaq cutoff; late MOC is rejected)
        # * OrderType.LOC -> Order(orderType="LOC", lmtPrice=order.limit)
        # * tif: TimeInForce.GTC -> "GTC", DAY -> "DAY" (order.tif field).
        # * OrderType.BRACKET -> `ib.bracketOrder(action, qty,
        #   limitPrice=order.limit, takeProfitPrice=order.tp,
        #   stopLossPrice=order.stop)`; if order.tp is None, build parent +
        #   stop child manually and share an `ocaGroup` (ocaType=1 cancels
        #   the sibling on fill). Loop.md §4: protection attaches on entry
        #   fill via bracket/OCA — never leave a position without a stop.
        # * transmit flags: parent.transmit=False, children transmit=False
        #   except the LAST child transmit=True — IBKR then activates the
        #   whole group atomically (a parent sent with transmit=True before
        #   its children exists unprotected for a moment; don't).
        # * Capture the returned Trade/orderId into order.broker_ref, set
        #   status=SUBMITTED, and return PlaceResult(accepted=True, ...,
        #   child_orders=[bracket legs]) mirroring PaperBroker's contract.
        # * Update status from `ib.orderStatusEvent` / trade.orderStatus
        #   (PreSubmitted/Submitted -> SUBMITTED, Filled -> FILLED, ...).
        # * CASH account: pre-check spendable (settled, see get_account)
        #   cash before sending; IBKR rejects on insufficient settled funds
        #   but we must fail closed locally first (Loop.md §3).
        # Stub contract: must NOT mutate `order` (caller re-submits the same
        # object to PaperBroker in Phase 1 mirroring; status stays NEW).
        raise _todo("place_order")

    def cancel_order(self, order_id: str) -> bool:
        # TODO(Phase 1): resolve order_id -> broker_ref (IBKR orderId int),
        # `ib.cancelOrder(ib_order)`, await `orderStatusEvent` reaching
        # "Cancelled" (or "ApiCancelled"). Cancelling a filled/unknown order
        # yields error 104/135 — return False rather than raise. Cancelling
        # a bracket parent cancels its children (same OCA group).
        raise _todo("cancel_order")

    def get_orders(self, active_only: bool = False) -> list[Order]:
        # TODO(Phase 1): `ib.reqAllOpenOrders()` / `ib.openTrades()` for
        # active; completed ones via `ib.reqCompletedOrders(apiOnly=True)`.
        # Map ib status -> OrderStatus (PendingSubmit/PreSubmitted/Submitted
        # -> SUBMITTED, Filled -> FILLED, Cancelled/ApiCancelled ->
        # CANCELLED, Inactive -> REJECTED). Reconcile by broker_ref against
        # the ledger; orders placed outside this process (TWS UI) must
        # surface too, not be silently dropped.
        raise _todo("get_orders")

    # --------------------------------------------------------------- fills

    def get_fills(self) -> list[Fill]:
        # TODO(Phase 1): `ib.fills()` (session cache) + `ib.reqExecutions()`
        # with an ExecutionFilter for backfill after reconnect; subscribe to
        # `ib.execDetailsEvent` for realtime. Map execution.execId -> Fill.id
        # (dedupe key — execDetails can replay), execution.shares -> qty,
        # execution.avgPrice/price -> px, commissionReport.commission ->
        # commission (arrives as a separate CommissionReport event; join on
        # execId). Overnight GTC/MOC fills happen while the user sleeps
        # (Loop.md §4) — the 09:00 ET reporter run must backfill via
        # reqExecutions, not rely on a live event stream. mode=LIVE tagging
        # per Loop.md §6.
        raise _todo("get_fills")
