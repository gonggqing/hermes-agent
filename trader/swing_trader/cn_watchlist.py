"""China/HK research universe for the CN morning session (Loop.md two-session
extension).

Production mainland research uses liquid broad/sector ETFs as stable market
anchors and adds a dynamic, cross-industry company universe on every run. The
same universe can feed the isolated CN paper loop; execution eligibility still
passes the independent tick/lot/T+1/risk/confirmation gates. The older mixed
CN/HK catalog remains available for compatibility, but it no longer defines
the production A-share stock-picking universe.

The universe is **config-editable**: ``FINANCE_CN_SYMBOLS`` (or ``Settings.
cn_symbols``) is a comma-separated override. Symbols already known here keep
their theme/role tags; unknown overrides are tagged ``cn-custom``.

Reuses :class:`~swing_trader.watchlist.WatchlistItem` so the research brief's
mover/theme tagging works uniformly across the US and CN universes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from swing_trader.schemas import AiPhase, Role
from swing_trader.watchlist import WatchlistItem

__all__ = [
    "CN_MAINLAND_UNIVERSE",
    "CN_UNIVERSE",
    "CnWatchlist",
    "build_cn_watchlist",
    "build_mainland_watchlist",
]


def _mk(symbols: str, theme: str, phase: AiPhase, role: Role) -> list[WatchlistItem]:
    return [
        WatchlistItem(symbol=s, theme=theme, ai_phase=phase, role=role) for s in symbols.split()
    ]


#: Legacy mixed CN/HK research universe retained for API compatibility.
#: Mainland symbols are best-effort — they are skipped when the free feed has
#: no data for them (HK-only degrade). Tags drive brief theme aggregation.
CN_UNIVERSE: list[WatchlistItem] = [
    # --- Semiconductors (FOCUS) — foundry, IP, AI chips ---
    *_mk("0981.HK 1347.HK", "cn-semiconductor", AiPhase.INFRA, Role.CONVICTION),
    *_mk("688981.SS 688256.SS 002049.SZ", "cn-semiconductor", AiPhase.INFRA, Role.CONVICTION),
    # --- Optical / components (FOCUS) ---
    *_mk("2382.HK", "cn-optics-components", AiPhase.NETWORK, Role.ROTATION),
    # --- AI / software (FOCUS) ---
    *_mk("002415.SZ", "cn-ai-software", AiPhase.APPLICATION, Role.ROTATION),
    # --- Internet platforms (context) ---
    *_mk("0700.HK 3690.HK", "cn-internet-platform", AiPhase.APPLICATION, Role.CONVICTION),
    # --- Cloud / e-commerce (context) ---
    *_mk("9988.HK 9618.HK", "cn-cloud-ecommerce", AiPhase.CLOUD, Role.ROTATION),
    # --- Consumer electronics / hardware (context, informative) ---
    *_mk("1810.HK 0992.HK 000725.SZ", "cn-consumer-electronics", AiPhase.NONE, Role.ROTATION),
    # --- EV / battery / power (context) ---
    *_mk("1211.HK 2015.HK", "cn-ev-battery", AiPhase.POWER, Role.ROTATION),
    *_mk("300750.SZ 002594.SZ", "cn-ev-battery", AiPhase.POWER, Role.ROTATION),
    # --- Index proxies (regime context) ---
    *_mk("3033.HK 2800.HK", "cn-index", AiPhase.NONE, Role.CORE),
    # --- Non-tech consumer anchor (context only) ---
    *_mk("600519.SS", "cn-consumer-anchor", AiPhase.NONE, Role.HEDGE),
]

#: HK/China index symbols shown as regime context in the CN brief.
CN_INDEX_SYMBOLS: tuple[str, ...] = ("^HSI", "^HSCE")

# Phase 0.95 keeps the legacy mixed universe above for API compatibility, but
# production CN and HK sessions consume disjoint views.
# Production mainland baseline is a set of broad/sector market anchors rather
# than a frozen stock-picking list.  Individual companies enter the daily
# evidence packet through ``EastmoneyMarketUniverse`` and can rotate every run.
CN_MAINLAND_UNIVERSE: list[WatchlistItem] = [
    *_mk("510300.SS 510500.SS 588000.SS", "cn-broad-market", AiPhase.NONE, Role.CORE),
    *_mk("512800.SS", "cn-banks", AiPhase.NONE, Role.HEDGE),
    *_mk("512880.SS", "cn-brokerage", AiPhase.NONE, Role.ROTATION),
    *_mk("512010.SS", "cn-healthcare", AiPhase.NONE, Role.ROTATION),
    *_mk("159928.SZ", "cn-consumer", AiPhase.NONE, Role.HEDGE),
    *_mk("512660.SS", "cn-defense", AiPhase.NONE, Role.ROTATION),
    *_mk("516160.SS 515790.SS", "cn-new-energy", AiPhase.POWER, Role.ROTATION),
    *_mk("159869.SZ", "cn-digital-content", AiPhase.APPLICATION, Role.ROTATION),
    *_mk("159995.SZ", "cn-semiconductor", AiPhase.INFRA, Role.CONVICTION),
]
CN_MAINLAND_INDEX_SYMBOLS: tuple[str, ...] = ("000001.SS", "399001.SZ")


@dataclass(frozen=True)
class CnWatchlist:
    """A resolved CN universe: items, symbol list, and a lookup callable."""

    items: list[WatchlistItem]
    lookup: Callable[[str], Optional[WatchlistItem]]

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.items]


def _norm(symbol: str) -> str:
    """Uppercase + trim; keeps exchange suffixes (``0700.HK`` -> ``0700.HK``)."""
    return symbol.strip().upper()


def build_cn_watchlist(override: str = "") -> CnWatchlist:
    """Resolve the CN universe, applying an optional comma-separated override.

    With no override the built-in :data:`CN_UNIVERSE` is used. An override
    restricts the universe to the listed symbols, preserving the built-in
    theme/role tags for known symbols and tagging unknown ones ``cn-custom``.
    """
    known = {_norm(i.symbol): i for i in CN_UNIVERSE}
    if override.strip():
        items: list[WatchlistItem] = []
        for raw in override.split(","):
            sym = _norm(raw)
            if not sym:
                continue
            items.append(
                known.get(sym)
                or WatchlistItem(
                    symbol=sym,
                    theme="cn-custom",
                    ai_phase=AiPhase.NONE,
                    role=Role.ROTATION,
                )
            )
    else:
        items = list(CN_UNIVERSE)

    by_symbol = {_norm(i.symbol): i for i in items}

    def lookup(symbol: str) -> Optional[WatchlistItem]:
        return by_symbol.get(_norm(symbol))

    return CnWatchlist(items=items, lookup=lookup)


def build_mainland_watchlist(override: str = "") -> CnWatchlist:
    """Resolve a mainland-only universe; reject HK suffixes from overrides."""
    known = {_norm(i.symbol): i for i in CN_MAINLAND_UNIVERSE}
    raw_symbols = [s for s in override.split(",") if s.strip()] if override.strip() else []
    items = []
    for raw in raw_symbols:
        sym = _norm(raw)
        if not sym.endswith((".SS", ".SZ")):
            continue
        items.append(
            known.get(sym)
            or WatchlistItem(
            symbol=sym, theme="cn-custom", ai_phase=AiPhase.NONE, role=Role.ROTATION
            )
        )
    if not override.strip():
        items = list(CN_MAINLAND_UNIVERSE)
    by_symbol = {_norm(i.symbol): i for i in items}
    return CnWatchlist(items=items, lookup=lambda symbol: by_symbol.get(_norm(symbol)))
