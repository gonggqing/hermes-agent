"""Watchlist universe (Loop.md §11) — the MONITORED set, NOT a buy list.

Structured along the AI value-chain (infra → memory/network/power →
application/cloud) so the system can reason about rotation (Loop.md §12).
Each symbol carries {theme, ai_phase, role}; the RiskEngine enforces
per-role exposure caps.

NewsMonitor is responsible for keeping this set current; edits here are
data-only and must not change the schema.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from swing_trader.schemas import AiPhase, Role


class WatchlistItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    theme: str
    ai_phase: AiPhase
    role: Role
    enabled: bool = True  # crypto stays disabled until OSL permission confirmed (§11-I)


def _mk(symbols: str, theme: str, phase: AiPhase, role: Role, enabled: bool = True):
    return [
        WatchlistItem(symbol=s, theme=theme, ai_phase=phase, role=role, enabled=enabled)
        for s in symbols.split()
    ]


UNIVERSE: list[WatchlistItem] = [
    # A. Base / reference indices — context, low-vol anchors
    *_mk("SPY VOO IVV DIA QQQ VTI", "base-index", AiPhase.NONE, Role.CORE),
    # B. AI infra — compute & chips (current conviction)
    *_mk("NVDA AMD AVGO MRVL TSM ASML AMAT LRCX KLAC", "compute-chips", AiPhase.INFRA, Role.CONVICTION),
    # C. AI infra — memory / storage (supercycle)
    *_mk("MU WDC SNDK", "memory-storage", AiPhase.MEMORY, Role.CONVICTION),
    # D. AI infra — networking / optical
    *_mk("ANET CIEN LITE COHR CRDO", "network-optical", AiPhase.NETWORK, Role.CONVICTION),
    # E. AI infra — systems / power / cooling / energy
    *_mk("SMCI DELL VRT ETN GEV", "systems-power", AiPhase.POWER, Role.ROTATION),
    *_mk("CEG VST", "power-utility", AiPhase.POWER, Role.ROTATION),
    *_mk("CCJ URA", "nuclear-uranium", AiPhase.POWER, Role.ROTATION),
    *_mk("EQIX DLR", "dc-reit", AiPhase.POWER, Role.ROTATION),
    # F. AI application / software / cloud (the 2–3y upcycle to watch early)
    *_mk("MSFT AMZN GOOGL META ORCL", "hyperscaler", AiPhase.CLOUD, Role.ROTATION),
    *_mk("PLTR NOW CRM SNOW DDOG CRWD ADBE", "software-saas", AiPhase.APPLICATION, Role.ROTATION),
    *_mk("IGV WCLD SKYY", "software-etf", AiPhase.APPLICATION, Role.ROTATION),
    # G. Rotation / rate-sensitive upcycle
    *_mk("XBI IBB", "biotech", AiPhase.NONE, Role.ROTATION),
    *_mk("IWM", "small-caps", AiPhase.NONE, Role.ROTATION),
    # H. Hedges / diversifiers (uncorrelated to the AI bet)
    *_mk("XLE XOP XOM CVX", "energy-oilgas", AiPhase.NONE, Role.HEDGE),
    *_mk("GLD IAU", "gold", AiPhase.NONE, Role.HEDGE),
    *_mk("TLT IEF", "bonds", AiPhase.NONE, Role.HEDGE),
    # I. Crypto — DISABLED until OSL permission + API support confirmed (§11-I)
    *_mk("BTC-USD ETH-USD", "crypto", AiPhase.NONE, Role.ROTATION, enabled=False),
]

_BY_SYMBOL = {item.symbol: item for item in UNIVERSE}


def get(symbol: str) -> WatchlistItem | None:
    return _BY_SYMBOL.get(symbol.strip().upper())


def enabled_symbols() -> list[str]:
    return [i.symbol for i in UNIVERSE if i.enabled]


def by_role(role: Role, enabled_only: bool = True) -> list[WatchlistItem]:
    return [i for i in UNIVERSE if i.role is role and (i.enabled or not enabled_only)]


def by_phase(phase: AiPhase, enabled_only: bool = True) -> list[WatchlistItem]:
    return [i for i in UNIVERSE if i.ai_phase is phase and (i.enabled or not enabled_only)]
