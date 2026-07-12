// Finance-tab i18n resolution (NEXT-STEP.md invariant 6: no hard-coded
// user-facing strings). The `finance` catalog section is optional on
// `Translations` — the newer-section convention — so locales that have not
// translated it yet (everything except en/zh today) fall back to the English
// catalog entry here instead of scattering English literals through the
// components.

import { useI18n } from "@/i18n";
import { financeEn } from "@/i18n/en";
import type { FinanceTranslations } from "@/i18n/types";

export function useFinanceT(): FinanceTranslations {
  const { t } = useI18n();
  return t.finance ?? financeEn;
}
