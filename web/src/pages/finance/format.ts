// Shared formatting + tone helpers for the Finance tab (Loop.md §5.9).

import type { FinanceCandidateStatus } from "@/lib/api";
import type { FinanceTranslations } from "@/i18n/types";
import type { WatchCurrency, WatchUnitKey } from "./constants";

/** Badge tones supported by @nous-research/ui's <Badge tone=...>. */
export type BadgeTone =
  | "default"
  | "destructive"
  | "outline"
  | "secondary"
  | "success"
  | "warning";

export function fmtMoney(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return v.toLocaleString(undefined, {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/** Per-symbol currency + unit for a watch-module price. */
export interface WatchPriceDisplay {
  currency: WatchCurrency | null;
  unit: WatchUnitKey | null;
}

/**
 * Format a watch-module price with its per-symbol currency + unit. Shapes:
 *  - percent (unit "pct"):     "4.30 %"            (no currency, no slash)
 *  - currency + unit (full):   "4,079.00 $ / 盎司"  (currency AFTER the value)
 *  - currency, no unit:        "$67,000.00"        (currency PREFIX)
 *  - unit only, no currency:   "12.30 / 桶"
 * Pass `withUnit: false` for a compact readout (currency-prefixed value, or
 * "4.30 %") used in tooltips / axis labels. Localizes the unit word via
 * `ft.watch.units`. Nullish / NaN → "—".
 */
export function fmtWatchPrice(
  value: number | null | undefined,
  display: WatchPriceDisplay,
  ft: FinanceTranslations,
  opts?: { withUnit?: boolean },
): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const { currency, unit } = display;
  const num = fmtMoney(value);
  // Percent stays "%" in every context (no currency, no localization).
  if (unit === "pct") return `${num} %`;
  const withUnit = opts?.withUnit !== false;
  if (currency === null) {
    return unit && withUnit ? `${num} / ${ft.watch.units[unit]}` : num;
  }
  // Currency present: compact + unit-less both render as a prefix ("$67,000").
  if (unit === null || !withUnit) return `${currency}${num}`;
  return `${num} ${currency} / ${ft.watch.units[unit]}`;
}

export function fmtSigned(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${fmtMoney(v)}`;
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}%`;
}

export function fmtQty(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number.isInteger(v) ? String(v) : v.toFixed(2);
}

/** Signed percentage, e.g. "+3.2%" — used for SMA-distance readouts. */
export function fmtSignedPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${v.toFixed(digits)}%`;
}

/**
 * Signed P&L percentage from a *fraction* (0.2 → "+20.0%"): the valuation
 * endpoint reports `pnl_pct` as unrealized_pnl / cost. Nullish / NaN → "—",
 * never a fabricated 0%.
 */
export function fmtPnlPct(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v > 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;
}

export function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (Number.isNaN(d.getTime())) return ts;
    return d.toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return ts;
  }
}

/** Text color class for a signed PnL value. */
export function pnlClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) {
    return "text-muted-foreground";
  }
  return v > 0 ? "text-success" : "text-destructive";
}

export function candidateStatusTone(status: FinanceCandidateStatus): BadgeTone {
  switch (status) {
    case "approved":
    case "edited":
    case "placed":
      return "success";
    case "rejected":
    case "risk_vetoed":
      return "destructive";
    case "expired":
      return "secondary";
    case "pushed":
      return "warning";
    case "proposed":
    case "risk_approved":
    default:
      return "outline";
  }
}

export function sideTone(side: "BUY" | "SELL"): BadgeTone {
  return side === "BUY" ? "success" : "destructive";
}

/** Badge tone for the market regime chip (risk_on / neutral / risk_off). */
export function regimeTone(regime: string | undefined): BadgeTone {
  switch (regime) {
    case "risk_on":
      return "success";
    case "risk_off":
      return "destructive";
    default:
      return "outline";
  }
}

/** Badge tone for a signal direction (long / short / neutral). */
export function directionTone(direction: string): BadgeTone {
  switch (direction) {
    case "long":
      return "success";
    case "short":
      return "destructive";
    default:
      return "outline";
  }
}

/** Left-border tint class for a news item by sentiment sign. */
export function sentimentBorderClass(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v) || v === 0) {
    return "border-border";
  }
  return v > 0 ? "border-success" : "border-destructive";
}

/** Actor identity attached to every human action relayed by this surface. */
// TODO: wire real dashboard identity (e.g. /api/auth/me) once available.
export const FINANCE_ACTOR = "hermes-user";
