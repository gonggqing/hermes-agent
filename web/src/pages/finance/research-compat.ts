import type { FinanceDiscoveryPool } from "@/lib/api";

/** Archived briefs created before Phase 0.95 have no discovery field. */
export function discoveryCandidates(
  pool: FinanceDiscoveryPool | null | undefined,
) {
  return pool?.candidates ?? [];
}
