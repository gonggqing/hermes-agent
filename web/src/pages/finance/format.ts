// Shared formatting + tone helpers for the Finance tab (Loop.md §5.9).

import type { FinanceCandidateStatus } from "@/lib/api";

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
