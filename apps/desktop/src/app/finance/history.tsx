import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { StatusDot } from '@/components/status-dot'
import { SegmentedControl } from '@/components/ui/segmented-control'
import {
  type FinanceCandidate,
  type FinanceCandidateStatus,
  type FinanceMode,
  getFinanceAudit,
  getFinanceCandidates
} from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { CANDIDATE_STATUS_TONE, enumLabel, financeKey, fmtPct, fmtPrice, fmtQty, fmtTs } from './lib'
import { FinanceSectionLabel, QuerySection } from './primitives'

// Coarse status filter: terminal human verdicts + the in-flight set.
const STATUS_FILTERS = ['all', 'approved', 'rejected', 'expired', 'placed'] as const

type StatusFilter = (typeof STATUS_FILTERS)[number]

export function FinanceHistoryTab({ enabled, mode, query }: { enabled: boolean; mode: FinanceMode; query: string }) {
  const { t } = useI18n()
  const copy = t.finance.history
  const [statusFilter, setStatusFilter] = useState<StatusFilter>('all')
  const [selectedId, setSelectedId] = useState<null | string>(null)

  const candidatesQuery = useQuery({
    enabled,
    queryFn: () =>
      getFinanceCandidates({
        mode,
        status: statusFilter === 'all' ? undefined : (statusFilter as FinanceCandidateStatus)
      }),
    queryKey: financeKey('candidates', mode, statusFilter),
    retry: 1
  })

  const candidates = candidatesQuery.data ?? []
  const needle = query.trim().toUpperCase()
  const visible = needle ? candidates.filter(candidate => candidate.symbol.includes(needle)) : candidates
  const selected = visible.find(candidate => candidate.id === selectedId) ?? null

  return (
    <div className="space-y-4">
      <section className="space-y-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <FinanceSectionLabel>
            {copy.title}
            {candidates.length > 0 ? ` · ${candidates.length}` : ''}
          </FinanceSectionLabel>
          <SegmentedControl
            onChange={value => setStatusFilter(value)}
            options={STATUS_FILTERS.map(value => ({ id: value, label: copy.filters[value] }))}
            value={statusFilter}
          />
        </div>

        <QuerySection
          empty={needle ? copy.emptySearch : copy.empty}
          error={candidatesQuery.isError ? candidatesQuery.error : undefined}
          isEmpty={visible.length === 0}
          loading={candidatesQuery.isPending}
        >
          <div className="flex flex-col gap-px">
            {visible.map(candidate => (
              <CandidateHistoryRow
                active={candidate.id === selectedId}
                candidate={candidate}
                key={candidate.id}
                onSelect={() => setSelectedId(candidate.id === selectedId ? null : candidate.id)}
              />
            ))}
          </div>
        </QuerySection>
      </section>

      {selected && <AuditTrail candidate={selected} enabled={enabled} />}
    </div>
  )
}

function CandidateHistoryRow({
  active,
  candidate,
  onSelect
}: {
  active: boolean
  candidate: FinanceCandidate
  onSelect: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.history

  return (
    <button
      className={cn(
        'grid grid-cols-[auto_1fr_auto] items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40',
        active ? 'bg-(--ui-bg-tertiary)' : 'hover:bg-(--chrome-action-hover)'
      )}
      onClick={onSelect}
      type="button"
    >
      <StatusDot tone={CANDIDATE_STATUS_TONE[candidate.status] ?? 'muted'} />
      <span className="min-w-0 truncate">
        <span className="font-medium text-foreground">{candidate.symbol}</span>
        <span className="text-muted-foreground">
          {' '}
          {enumLabel(t.finance.enums.side, candidate.side)} {fmtQty(candidate.qty)} ·{' '}
          {enumLabel(t.finance.enums.orderType, candidate.order_type)}
          {candidate.limit !== null && ` @ ${fmtPrice(candidate.limit)}`} ·{' '}
          {enumLabel(t.finance.enums.candidateStatus, candidate.status)} ·{' '}
          {copy.rowConfidence(fmtPct(candidate.confidence * 100, 0))}
        </span>
      </span>
      <span className="shrink-0 text-[0.62rem] tabular-nums text-muted-foreground/70">{fmtTs(candidate.ts)}</span>
    </button>
  )
}

// Immutable approval audit trail (Loop.md §5.6) for the selected candidate.
function AuditTrail({ candidate, enabled }: { candidate: FinanceCandidate; enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.history

  const auditQuery = useQuery({
    enabled,
    queryFn: () => getFinanceAudit({ candidateId: candidate.id }),
    queryKey: financeKey('audit', candidate.id),
    retry: 1
  })

  const events = auditQuery.data ?? []

  return (
    <section className="space-y-2">
      <FinanceSectionLabel>{copy.auditTitle(candidate.symbol, candidate.id.slice(0, 8))}</FinanceSectionLabel>
      <QuerySection
        empty={copy.auditEmpty}
        error={auditQuery.isError ? auditQuery.error : undefined}
        isEmpty={events.length === 0}
        loading={auditQuery.isPending}
      >
        <ol className="space-y-1 border-l border-(--ui-stroke-tertiary) pl-3">
          {events.map((event, index) => (
            <li className="text-xs leading-5" key={`${event.ts}-${index}`}>
              <span className="tabular-nums text-muted-foreground/70">{fmtTs(event.ts)}</span>{' '}
              <span className={cn('font-medium', event.applied ? 'text-foreground' : 'text-destructive')}>
                {enumLabel(t.finance.enums.auditAction, event.action)}
              </span>{' '}
              <span className="text-muted-foreground">
                {copy.auditEvent(event.actor, event.surface, event.version)} —{' '}
                {event.prev_status && event.new_status
                  ? `${enumLabel(t.finance.enums.candidateStatus, event.prev_status)} → ${enumLabel(t.finance.enums.candidateStatus, event.new_status)}`
                  : event.applied
                    ? copy.applied
                    : copy.notApplied}
                {event.detail && ` · ${event.detail}`}
              </span>
            </li>
          ))}
        </ol>
      </QuerySection>
    </section>
  )
}
