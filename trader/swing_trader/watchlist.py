"""Watchlist universe (Loop.md §11) — the MONITORED set, NOT a buy list.

Structured along the AI value-chain (infra → memory/network/power →
application/cloud) so the system can reason about rotation (Loop.md §12).
Each symbol carries {theme, ai_phase, role}; the RiskEngine enforces
per-role exposure caps.

NewsMonitor is responsible for keeping this set current; edits here are
data-only and must not change the schema.
"""

from __future__ import annotations

from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict

from swing_trader.schemas import AiPhase, Role


class WatchlistItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    theme: str
    ai_phase: AiPhase
    role: Role
    enabled: bool = True  # crypto stays disabled until OSL permission confirmed (§11-I)
    security_type: Literal["stock", "etf", "crypto"] = "stock"


def _mk(
    symbols: str,
    theme: str,
    phase: AiPhase,
    role: Role,
    enabled: bool = True,
    security_type: Literal["stock", "etf", "crypto"] = "stock",
):
    return [
        WatchlistItem(
            symbol=s,
            theme=theme,
            ai_phase=phase,
            role=role,
            enabled=enabled,
            security_type=security_type,
        )
        for s in symbols.split()
    ]


UNIVERSE: list[WatchlistItem] = [
    # A. Base / reference indices — context, low-vol anchors
    *_mk(
        "SPY VOO IVV DIA QQQ VTI",
        "base-index",
        AiPhase.NONE,
        Role.CORE,
        security_type="etf",
    ),
    # B. AI infra — compute & chips (current conviction)
    *_mk(
        "NVDA AMD AVGO MRVL TSM ASML AMAT LRCX KLAC",
        "compute-chips",
        AiPhase.INFRA,
        Role.CONVICTION,
    ),
    # C. AI infra — memory / storage (supercycle)
    *_mk("MU WDC SNDK", "memory-storage", AiPhase.MEMORY, Role.CONVICTION),
    # D. AI infra — networking / optical
    *_mk("ANET CIEN LITE COHR CRDO", "network-optical", AiPhase.NETWORK, Role.CONVICTION),
    # E. AI infra — systems / power / cooling / energy
    *_mk("SMCI DELL VRT ETN GEV", "systems-power", AiPhase.POWER, Role.ROTATION),
    *_mk("CEG VST", "power-utility", AiPhase.POWER, Role.ROTATION),
    *_mk("CCJ", "nuclear-uranium", AiPhase.POWER, Role.ROTATION),
    *_mk("URA", "nuclear-uranium", AiPhase.POWER, Role.ROTATION, security_type="etf"),
    *_mk("EQIX DLR", "dc-reit", AiPhase.POWER, Role.ROTATION),
    # F. AI application / software / cloud (the 2–3y upcycle to watch early)
    *_mk("MSFT AMZN GOOGL META ORCL", "hyperscaler", AiPhase.CLOUD, Role.ROTATION),
    *_mk("PLTR NOW CRM SNOW DDOG CRWD ADBE", "software-saas", AiPhase.APPLICATION, Role.ROTATION),
    *_mk(
        "IGV WCLD SKYY",
        "software-etf",
        AiPhase.APPLICATION,
        Role.ROTATION,
        security_type="etf",
    ),
    # G. Rotation / rate-sensitive upcycle
    *_mk("XBI IBB", "biotech", AiPhase.NONE, Role.ROTATION, security_type="etf"),
    *_mk("IWM", "small-caps", AiPhase.NONE, Role.ROTATION, security_type="etf"),
    # H. Hedges / diversifiers (uncorrelated to the AI bet)
    *_mk("XLE XOP", "energy-oilgas", AiPhase.NONE, Role.HEDGE, security_type="etf"),
    *_mk("XOM CVX", "energy-oilgas", AiPhase.NONE, Role.HEDGE),
    *_mk("GLD IAU", "gold", AiPhase.NONE, Role.HEDGE, security_type="etf"),
    *_mk("TLT IEF", "bonds", AiPhase.NONE, Role.HEDGE, security_type="etf"),
    # I. Crypto research universe — still DISABLED for automatic trading until
    # OSL permission + broker/API support are confirmed (§11-I). BTC/ETH are
    # core observations; SOL/BNB/XRP/ADA are higher-volatility satellites.
    *_mk(
        "BTC-USD ETH-USD SOL-USD BNB-USD XRP-USD ADA-USD",
        "crypto",
        AiPhase.NONE,
        Role.ROTATION,
        enabled=False,
        security_type="crypto",
    ),
]

_BY_SYMBOL = {item.symbol: item for item in UNIVERSE}


def get(symbol: str) -> WatchlistItem | None:
    return _BY_SYMBOL.get(symbol.strip().upper())


def enabled_symbols() -> list[str]:
    return [i.symbol for i in UNIVERSE if i.enabled]


def earnings_symbols(
    symbols: list[str] | None = None,
    *,
    lookup: Callable[[str], WatchlistItem | None] | None = None,
) -> list[str]:
    """Return company securities for which an earnings date is meaningful.

    Yahoo's earnings endpoints return slow 404s for ETFs and funds. Unknown
    custom symbols remain eligible (fail-open for research coverage) until an
    instrument provider supplies their type.
    """

    result: list[str] = []
    resolve = lookup or get
    for raw in symbols if symbols is not None else enabled_symbols():
        symbol = raw.strip().upper()
        item = resolve(symbol)
        if item is None or item.security_type == "stock":
            result.append(symbol)
    return result


def by_role(role: Role, enabled_only: bool = True) -> list[WatchlistItem]:
    return [i for i in UNIVERSE if i.role is role and (i.enabled or not enabled_only)]


def by_phase(phase: AiPhase, enabled_only: bool = True) -> list[WatchlistItem]:
    return [i for i in UNIVERSE if i.ai_phase is phase and (i.enabled or not enabled_only)]
