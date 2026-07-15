import { describe, expect, it } from "vitest";

import type { FinanceDiscoveryPool } from "@/lib/api";

import { discoveryCandidates } from "./research-compat";

describe("discoveryCandidates", () => {
  it("treats pre-Phase 0.95 archived briefs as an empty discovery pool", () => {
    expect(discoveryCandidates(undefined)).toEqual([]);
    expect(discoveryCandidates(null)).toEqual([]);
  });

  it("preserves candidates from current briefs", () => {
    const pool = {
      market: "US",
      as_of: "2026-07-15T00:00:00Z",
      candidates: [{ symbol: "NVDA" }],
      rejected: [],
      source_count: 1,
    } as unknown as FinanceDiscoveryPool;

    expect(discoveryCandidates(pool)).toEqual([{ symbol: "NVDA" }]);
  });
});
