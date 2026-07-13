// China/HK share ONE CN research brief (Loop.md §7 Phase 0.5). We fetch it
// once and partition it in the frontend: China keeps movers/signals whose
// symbol ends in .SS or .SZ (Shanghai/Shenzhen); HK keeps those ending in
// .HK. Regime, news, themes, freshness, uncertainty and provenance are
// shared and shown on both. Per-region briefs are a Phase 0.9 refinement.

import type {
  FinanceBriefMover,
  FinanceBriefSignal,
  FinanceResearchBrief,
} from "@/lib/api";

export type CnRegion = "china" | "hk";

function matchesRegion(symbol: string, region: CnRegion): boolean {
  const s = symbol.toUpperCase();
  if (region === "hk") return s.endsWith(".HK");
  // Mainland China: Shanghai (.SS) and Shenzhen (.SZ) listings.
  return s.endsWith(".SS") || s.endsWith(".SZ");
}

/**
 * Return a shallow copy of the CN brief with movers + signals filtered to
 * one region. Everything else (regime/news/themes/freshness/uncertainty/
 * provenance) is passed through unchanged so it renders on both desks.
 */
export function partitionCnBrief(
  brief: FinanceResearchBrief,
  region: CnRegion,
): FinanceResearchBrief {
  const filterMovers = (rows: FinanceBriefMover[]) =>
    rows.filter((m) => matchesRegion(m.symbol, region));
  const filterSignals = (rows: FinanceBriefSignal[]) =>
    rows.filter((s) => matchesRegion(s.symbol, region));
  return {
    ...brief,
    movers: {
      top: filterMovers(brief.movers.top),
      bottom: filterMovers(brief.movers.bottom),
    },
    signals_today: filterSignals(brief.signals_today),
  };
}
