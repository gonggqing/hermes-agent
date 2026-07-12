import { useQuery } from '@tanstack/react-query'

import { getFinanceReports } from '@/hermes'
import { useI18n } from '@/i18n'

import { financeKey } from './lib'
import { FinanceCard, FinanceSectionLabel, QuerySection } from './primitives'

// Plain-text daily reports keyed by kind ("morning" first — the overnight
// fills + ledger summary the reporter builds each trading day, Loop.md §4).
const KIND_ORDER = ['morning']

export function FinanceReportsTab({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.reports

  const reportsQuery = useQuery({
    enabled,
    queryFn: getFinanceReports,
    queryKey: financeKey('reports'),
    refetchInterval: 5 * 60_000,
    retry: 1
  })

  const reports = Object.entries(reportsQuery.data ?? {}).sort(([a], [b]) => {
    const rankA = KIND_ORDER.indexOf(a)
    const rankB = KIND_ORDER.indexOf(b)

    return (rankA === -1 ? KIND_ORDER.length : rankA) - (rankB === -1 ? KIND_ORDER.length : rankB) || a.localeCompare(b)
  })

  return (
    <QuerySection
      empty={copy.empty}
      error={reportsQuery.isError ? reportsQuery.error : undefined}
      isEmpty={reports.length === 0}
      loading={reportsQuery.isPending}
    >
      <div className="space-y-4">
        {reports.map(([kind, text]) => (
          <section className="space-y-2" key={kind}>
            <FinanceSectionLabel>{copy.reportKind(kind)}</FinanceSectionLabel>
            <FinanceCard className="overflow-x-auto">
              <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-5 text-(--ui-text-secondary)">
                {text}
              </pre>
            </FinanceCard>
          </section>
        ))}
      </div>
    </QuerySection>
  )
}
