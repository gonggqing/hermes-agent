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

/** Currency symbol a watch price is quoted in (null for unit-only, e.g. %). */
export type WatchCurrency = "$" | "¥";

/**
 * Localized unit key for a watch price. Rendered via `ft.watch.units[...]`
 * (except `pct`, which always prints "%"). `null` = no unit suffix.
 */
export type WatchUnitKey = "oz" | "share" | "bbl" | "gram" | "pct";

/**
 * Derived-symbol config. A derived watch symbol has no direct Yahoo quote
 * (e.g. AU9999 / SGE domestic spot gold): its quote + candlestick are computed
 * from a `base` symbol (a real Yahoo price series) rescaled by an `fx` quote.
 * For AU9999: ¥/gram = GC=F(USD/oz) * CNY=X(CNY/USD) / gramsPerOunce.
 */
export interface WatchDerived {
  /** Base price series (real Yahoo symbol), e.g. "GC=F" (USD/oz). */
  base: string;
  /** FX quote symbol whose `last` rescales the base, e.g. "CNY=X" (CNY/USD). */
  fx: string;
  /** Grams per troy ounce (1 oz = 31.1035 g). */
  gramsPerOunce: number;
}

/** One symbol tracked inside a watch module. */
export interface WatchSymbol {
  symbol: string;
  label: string;
  /** Currency the price is quoted in; `null` for unit-only (e.g. yields). */
  currency: WatchCurrency | null;
  /** Localized unit suffix key; `null` for none (e.g. BTC → "$67,000"). */
  unit: WatchUnitKey | null;
  /** Present only for computed symbols (e.g. AU9999) — see {@link WatchDerived}. */
  derived?: WatchDerived;
}

/**
 * Symbol config for the read-only watch modules. Defined once here and
 * shared by the sidebar + the detail panels. Some symbols (GC=F, ^TNX,
 * 518880.SS) 404 from yfinance intermittently — the panel handles that
 * per-symbol without crashing. Each entry carries its display currency + unit
 * so prices render as e.g. "4079 $ / 盎司", "8.42 ¥ / 股", "4.30 %", "$67,000".
 */
export const WATCH_MODULES: Record<WatchModuleKey, WatchSymbol[]> = {
  gold: [
    { symbol: "GC=F", label: "COMEX Gold", currency: "$", unit: "oz" },
    { symbol: "GLD", label: "Gold ETF (SPDR)", currency: "$", unit: "share" },
    {
      symbol: "518880.SS",
      label: "Shanghai Gold ETF",
      currency: "¥",
      unit: "share",
    },
    // Derived: domestic spot gold (AU9999/SGE) is NOT on Yahoo. Computed from
    // international gold (GC=F, USD/oz) rescaled by USD/CNY (CNY=X) into ¥/gram.
    {
      symbol: "AU9999",
      label: "AU9999",
      currency: "¥",
      unit: "gram",
      derived: { base: "GC=F", fx: "CNY=X", gramsPerOunce: 31.1035 },
    },
  ],
  oil: [
    { symbol: "CL=F", label: "WTI Crude", currency: "$", unit: "bbl" },
    { symbol: "BZ=F", label: "Brent Crude", currency: "$", unit: "bbl" },
    { symbol: "USO", label: "Oil ETF", currency: "$", unit: "share" },
  ],
  rates: [
    // 10Y yield renders as a percentage ("4.30 %") — unit only, no currency.
    { symbol: "^TNX", label: "US 10Y Yield", currency: null, unit: "pct" },
    { symbol: "TLT", label: "20Y Treasury ETF", currency: "$", unit: "share" },
  ],
  crypto: [
    // Crypto renders currency-prefixed, no unit → "$67,000".
    { symbol: "BTC-USD", label: "Bitcoin", currency: "$", unit: null },
    { symbol: "ETH-USD", label: "Ethereum", currency: "$", unit: null },
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
export const ACTIVE_MARKETS: FinanceDesk[] = ["us", "china", "hk", "korea"];

/** Disabled market placeholders (badged, not selectable). UK/Japan were
 *  dropped (human directive 2026-07-14: keep only KR, semiconductor-focused). */
export const PLACEHOLDER_MARKETS: FinanceDesk[] = [];

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
