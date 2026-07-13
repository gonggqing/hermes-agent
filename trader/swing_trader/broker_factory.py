"""Broker factory (Loop.md §5.1, §3) — pick the broker adapter from Settings.

The ONLY place that maps ``BROKER=<backend>`` to a concrete
:class:`~swing_trader.interfaces.BrokerInterface`. Keeping it in one testable
function means the ``__main__`` service wiring stays declarative and the
paper→live safety derivation lives in exactly one place.

Safety (Loop.md §3): the IBKR *account* flag (paper vs live) is derived from the
live-order TRIPLE GATE (``settings.live_orders_allowed`` = HUMAN_CONFIRM &&
BROKER!=paper && !DRY_RUN), never from the port number. So a config that has not
cleared the gate can only ever connect to a PAPER account, and a live IBKR port
under such a config is refused at construction by :class:`IBKRBroker` itself.
This factory places NO orders and opens NO socket — it just constructs.
"""

from __future__ import annotations

from typing import Callable, Optional

from swing_trader.config import BrokerBackend, Settings
from swing_trader.interfaces import BrokerInterface
from swing_trader.log import get_logger
from swing_trader.schemas import Role

logger = get_logger(__name__)

# TWS: 7497=paper, 7496=live. IB Gateway: 4002=paper, 4001=live.
_LIVE_PORTS = frozenset({7496, 4001})


def build_broker(
    settings: Settings,
    *,
    starting_cash: float = 100_000.0,
    client_factory: Optional[Callable[[], object]] = None,
    role_for_symbol: Optional[Callable[[str], Role]] = None,
) -> BrokerInterface:
    """Construct the broker named by ``settings.broker``.

    ``client_factory`` (IBKR only) injects a mock :class:`IBClient` in tests so
    this never touches the network. ``starting_cash`` is used only by the
    PaperBroker.
    """
    backend = settings.broker

    if backend is BrokerBackend.PAPER:
        from swing_trader.paper_broker import PaperBroker

        return PaperBroker(starting_cash=starting_cash)

    if backend is BrokerBackend.IBKR:
        from swing_trader.ibkr_broker import IBKRBroker

        # Derive the account flag from the triple gate, NOT the port: a config
        # that has not cleared HUMAN_CONFIRM && !DRY_RUN can only reach a PAPER
        # account. IBKRBroker refuses paper=True on a live port, so a misconfig
        # (live port, un-gated) fails closed at construction.
        paper = not settings.live_orders_allowed
        if not paper and settings.ibkr_port not in _LIVE_PORTS:
            # Gate cleared but pointed at a paper port — harmless (no real
            # money), but almost certainly a mistake; make it loud.
            logger.warning(
                "live orders allowed but IBKR port is a PAPER port — connecting "
                "to a paper account despite the live gate",
                extra={"port": settings.ibkr_port},
            )
        logger.info(
            "constructing IBKRBroker",
            extra={"host": settings.ibkr_host, "port": settings.ibkr_port,
                   "client_id": settings.ibkr_client_id, "paper": paper},
        )
        return IBKRBroker(
            host=settings.ibkr_host,
            port=settings.ibkr_port,
            client_id=settings.ibkr_client_id,
            paper=paper,
            client_factory=client_factory,  # type: ignore[arg-type]
            role_for_symbol=role_for_symbol,
        )

    # ALPACA is declared in the enum but no adapter exists yet.
    raise NotImplementedError(
        f"broker backend {backend.value!r} has no adapter (only paper/ibkr are built)"
    )
