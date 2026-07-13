// Shared, single-source constants for the Finance master-detail layout:
// the research desks (markets) and the read-only cross-asset watch modules
// (Loop.md §3 — watch modules are research-only; no order authority).

import type { FinanceTranslations } from "@/i18n/types";

/** Research desk selectable in the Research sidebar. */
export type FinanceDesk =
  // Active markets.
  | "us"
  | "china"
  | "hk"
  // Disabled Phase 0.9 placeholders (not selectable).
  | "uk"
  | "korea"
  | "japan"
  // Read-only cross-asset watch modules.
  | "gold"
  | "oil"
  | "rates"
  | "crypto";

/** Watch-module keys (the read-only cross-asset modules). */
export type WatchModuleKey = "gold" | "oil" | "rates" | "crypto";

/** One symbol tracked inside a watch module. */
export interface WatchSymbol {
  symbol: string;
  label: string;
}

/**
 * Symbol config for the read-only watch modules. Defined once here and
 * shared by the sidebar + the detail panels. Some symbols (GC=F, ^TNX,
 * 518880.SS) 404 from yfinance intermittently — the panel handles that
 * per-symbol without crashing.
 */
export const WATCH_MODULES: Record<WatchModuleKey, WatchSymbol[]> = {
  gold: [
    { symbol: "GC=F", label: "COMEX Gold" },
    { symbol: "GLD", label: "Gold ETF (SPDR)" },
    { symbol: "518880.SS", label: "Shanghai Gold ETF" },
  ],
  oil: [
    { symbol: "CL=F", label: "WTI Crude" },
    { symbol: "BZ=F", label: "Brent Crude" },
    { symbol: "USO", label: "Oil ETF" },
  ],
  rates: [
    { symbol: "^TNX", label: "US 10Y Yield" },
    { symbol: "TLT", label: "20Y Treasury ETF" },
  ],
  crypto: [
    { symbol: "BTC-USD", label: "Bitcoin" },
    { symbol: "ETH-USD", label: "Ethereum" },
  ],
};

export const WATCH_MODULE_KEYS: WatchModuleKey[] = [
  "gold",
  "oil",
  "rates",
  "crypto",
];

/** Localized display name for a watch module. */
export function watchModuleName(
  key: WatchModuleKey,
  ft: FinanceTranslations,
): string {
  return ft.watch[key];
}

/** Active research markets shown in the "Markets" sidebar group. */
export const ACTIVE_MARKETS: FinanceDesk[] = ["us", "china", "hk"];

/** Disabled Phase 0.9 market placeholders (badged, not selectable). */
export const PLACEHOLDER_MARKETS: FinanceDesk[] = ["uk", "korea", "japan"];

/** Localized label for a market desk entry. */
export function marketName(desk: FinanceDesk, ft: FinanceTranslations): string {
  switch (desk) {
    case "us":
      return ft.layout.marketUs;
    case "china":
      return ft.layout.marketChina;
    case "hk":
      return ft.layout.marketHk;
    case "uk":
      return ft.layout.marketUk;
    case "korea":
      return ft.layout.marketKorea;
    case "japan":
      return ft.layout.marketJapan;
    default:
      return desk;
  }
}

/** True for the read-only watch-module desks. */
export function isWatchDesk(desk: FinanceDesk): desk is WatchModuleKey {
  return (
    desk === "gold" || desk === "oil" || desk === "rates" || desk === "crypto"
  );
}

/** True for a selectable, live research market (US/China/HK). */
export function isActiveMarketDesk(desk: FinanceDesk): boolean {
  return desk === "us" || desk === "china" || desk === "hk";
}
