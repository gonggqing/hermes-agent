import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useEffect, useMemo, useState } from 'react'

import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { ErrorBanner } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import {
  type FinanceActionPayload,
  type FinanceCandidate,
  type FinanceCandidateEdits,
  type FinancePendingCandidate,
  getFinancePendingCandidates,
  postFinanceCandidateAction
} from '@/hermes'
import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { DetailColumn, ListColumn, MasterDetail } from '../master-detail'

import { FinanceDetailPlaceholder, FinanceNavRow } from './chrome'
import {
  CANDIDATE_STATUS_TONE,
  enumLabel,
  FINANCE_ACTOR,
  FINANCE_KEY,
  financeKey,
  fmtPct,
  fmtPrice,
  fmtQty,
  fmtTs,
  idempotencyKeyFor,
  parseFinanceError,
  settleIdempotencyKey
} from './lib'
import { FinanceCard, FinancePill, QuerySection } from './primitives'

// Candidates land at 11:30 ET and expire at 12:30 ET (Loop.md §4) with no push
// signal to this surface, so poll while the tab is mounted.
const PENDING_POLL_MS = 15_000

export const PENDING_QUERY_KEY = financeKey('candidates', 'pending')

// Stable empty reference so the candidate-id memo doesn't recompute every render
// while the queue is empty (react-hooks/exhaustive-deps).
const NO_PENDING: FinancePendingCandidate[] = []

export function usePendingCandidates(enabled: boolean) {
  return useQuery({
    enabled,
    queryFn: getFinancePendingCandidates,
    queryKey: PENDING_QUERY_KEY,
    refetchInterval: PENDING_POLL_MS,
    retry: 1
  })
}

interface ActionVars {
  action: FinanceActionPayload['action']
  candidate: FinanceCandidate
  edits?: FinanceCandidateEdits
  version: number
}

export function FinanceQueueView({ bottomBar, enabled }: { bottomBar: React.ReactNode; enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.queue
  const queryClient = useQueryClient()
  const pendingQuery = usePendingCandidates(enabled)
  const [editing, setEditing] = useState<FinancePendingCandidate | null>(null)

  // Localized verbs for notification copy; the wire action stays the enum.
  const actionLabels: Record<FinanceActionPayload['action'], string> = {
    approve: copy.approve,
    edit: copy.edit,
    reject: copy.reject
  }

  const actionMutation = useMutation({
    mutationFn: ({ action, candidate, edits, version }: ActionVars) =>
      // TODO(finance): should also carry `X-Finance-Surface: desktop`; the IPC
      // bridge can't attach headers yet (see postFinanceCandidateAction).
      postFinanceCandidateAction(candidate.id, {
        action,
        actor: FINANCE_ACTOR,
        idempotency_key: idempotencyKeyFor(candidate.id, action, edits),
        expected_version: version,
        ...(edits ? { edits } : {})
      }),
    onError: (error, { action, candidate, edits }) => {
      const parsed = parseFinanceError(error)

      // Terminal server verdicts (window closed, unknown, already-terminal,
      // version conflict, invalid edit): this exact intent can never succeed,
      // so retire its idempotency key. Network/offline failures keep the key
      // so a retry replays instead of double-applying.
      if (parsed.status !== null && parsed.status >= 400 && parsed.status < 500) {
        settleIdempotencyKey(candidate.id, action, edits)
      }

      notifyError(new Error(parsed.message), copy.actionFailed(actionLabels[action], candidate.symbol))
    },
    onSettled: () => {
      // The action moved server-authoritative state: refresh the queue plus
      // everything derived from candidate status.
      void queryClient.invalidateQueries({ queryKey: FINANCE_KEY })
    },
    onSuccess: (result, { action, candidate, edits }) => {
      settleIdempotencyKey(candidate.id, action, edits)
      setEditing(null)
      notify({
        kind: 'success',
        message: result.message || `${candidate.symbol} ${result.code}`,
        title: copy.actionDone(candidate.symbol, actionLabels[action], result.code)
      })
    }
  })

  const pending = pendingQuery.data ?? NO_PENDING
  const candidateIds = useMemo(() => pending.map(entry => entry.candidate.id), [pending])
  const [selectedId, setSelectedId] = useRouteEnumParam('action', candidateIds, candidateIds[0] ?? '')
  const selected = pending.find(entry => entry.candidate.id === selectedId) ?? pending[0] ?? null

  return (
    <>
      <MasterDetail>
        <ListColumn>
          <QuerySection
            empty={copy.empty}
            error={pendingQuery.isError ? pendingQuery.error : undefined}
            isEmpty={pending.length === 0}
            loading={pendingQuery.isPending}
          >
            <div className="space-y-0.5">
              {pending.map(entry => (
                <FinanceNavRow
                  active={selected?.candidate.id === entry.candidate.id}
                  key={entry.candidate.id}
                  leading={<StatusDot tone={CANDIDATE_STATUS_TONE[entry.candidate.status] ?? 'muted'} />}
                  meta={
                    <span className="text-[0.58rem] uppercase tracking-wide text-muted-foreground/70">
                      {enumLabel(t.finance.enums.candidateStatus, entry.candidate.status)}
                    </span>
                  }
                  onSelect={() => setSelectedId(entry.candidate.id)}
                  subtitle={copy.rowMeta(enumLabel(t.finance.enums.side, entry.candidate.side), fmtQty(entry.candidate.qty))}
                  title={entry.candidate.symbol}
                />
              ))}
            </div>
          </QuerySection>
        </ListColumn>

        <DetailColumn actionBar={bottomBar}>
          {selected ? (
            <PendingCandidateCard
              busy={actionMutation.isPending}
              entry={selected}
              onApprove={() =>
                actionMutation.mutate({ action: 'approve', candidate: selected.candidate, version: selected.version })
              }
              onEdit={() => setEditing(selected)}
              onReject={() =>
                actionMutation.mutate({ action: 'reject', candidate: selected.candidate, version: selected.version })
              }
            />
          ) : (
            <FinanceDetailPlaceholder>{pendingQuery.isPending ? '' : copy.empty}</FinanceDetailPlaceholder>
          )}
        </DetailColumn>
      </MasterDetail>

      <EditCandidateDialog
        entry={editing}
        onClose={() => setEditing(null)}
        onSubmit={edits => {
          if (editing) {
            actionMutation.mutate({ action: 'edit', candidate: editing.candidate, edits, version: editing.version })
          }
        }}
        submitting={actionMutation.isPending}
      />
    </>
  )
}

function CandidatePriceRow({ candidate }: { candidate: FinanceCandidate }) {
  const { t } = useI18n()
  const copy = t.finance.queue

  const parts: Array<[string, null | number]> = [
    [copy.limit, candidate.limit],
    [copy.stop, candidate.stop],
    [copy.tp, candidate.tp],
    [copy.sl, candidate.sl],
    [copy.ref, candidate.ref_px]
  ]

  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-(--ui-text-secondary)">
      <span>
        {copy.qty} <span className="font-medium text-foreground">{fmtQty(candidate.qty)}</span>
      </span>
      {parts
        .filter(([, value]) => value !== null)
        .map(([label, value]) => (
          <span key={label}>
            {label} <span className="font-medium text-foreground">{fmtPrice(value)}</span>
          </span>
        ))}
      <span>
        {copy.tif} <span className="font-medium text-foreground">{enumLabel(t.finance.enums.tif, candidate.tif)}</span>
      </span>
    </div>
  )
}

function PendingCandidateCard({
  busy,
  entry,
  onApprove,
  onEdit,
  onReject
}: {
  busy: boolean
  entry: FinancePendingCandidate
  onApprove: () => void
  onEdit: () => void
  onReject: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.queue
  const { candidate, version, window_open: windowOpen } = entry
  const actionable = windowOpen && !busy

  return (
    <FinanceCard className="space-y-2.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-sm font-semibold tracking-tight text-foreground">{candidate.symbol}</span>
          <FinancePill variant={candidate.side === 'BUY' ? 'default' : 'warn'}>
            {enumLabel(t.finance.enums.side, candidate.side)}
          </FinancePill>
          <FinancePill variant="outline">{enumLabel(t.finance.enums.orderType, candidate.order_type)}</FinancePill>
          <span className="inline-flex items-center gap-1 text-[0.65rem] text-muted-foreground">
            <StatusDot tone={CANDIDATE_STATUS_TONE[candidate.status] ?? 'muted'} />
            {enumLabel(t.finance.enums.candidateStatus, candidate.status)}
          </span>
          <span className="text-[0.65rem] tabular-nums text-muted-foreground">
            {copy.confidenceVersion(fmtPct(candidate.confidence * 100, 0), version)}
          </span>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button disabled={!actionable} onClick={onApprove} size="xs">
            {copy.approve}
          </Button>
          <Button disabled={!actionable} onClick={onEdit} size="xs" variant="outline">
            {copy.edit}
          </Button>
          <Button disabled={!actionable} onClick={onReject} size="xs" variant="destructive">
            {copy.reject}
          </Button>
        </div>
      </div>

      <CandidatePriceRow candidate={candidate} />

      {candidate.rationale ? (
        <p className="text-xs leading-5 text-(--ui-text-secondary)">{candidate.rationale}</p>
      ) : null}

      {candidate.risk_note ? (
        <p className="text-[0.65rem] leading-4 text-amber-600 dark:text-amber-300">
          {copy.riskNote(candidate.risk_note)}
        </p>
      ) : null}

      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.62rem] text-muted-foreground/80">
        <span>{copy.pool(candidate.pool)}</span>
        <span>{copy.validUntil(fmtTs(candidate.valid_until))}</span>
        <span>{copy.proposedAt(fmtTs(candidate.ts))}</span>
      </div>

      {!windowOpen && <ErrorBanner>{copy.windowClosed}</ErrorBanner>}
    </FinanceCard>
  )
}

// Editing is limited to qty/limit/stop/sl/tp (Loop.md §5.6); the service
// re-validates the result through CandidateOrder before accepting.
const EDIT_FIELD_KEYS = ['qty', 'limit', 'stop', 'sl', 'tp'] as const satisfies ReadonlyArray<
  keyof FinanceCandidateEdits
>

const emptyValues = { limit: '', qty: '', sl: '', stop: '', tp: '' }

function EditCandidateDialog({
  entry,
  onClose,
  onSubmit,
  submitting
}: {
  entry: FinancePendingCandidate | null
  onClose: () => void
  onSubmit: (edits: FinanceCandidateEdits) => void
  submitting: boolean
}) {
  const { t } = useI18n()
  const copy = t.finance.queue
  const open = entry !== null
  const [values, setValues] = useState<Record<keyof FinanceCandidateEdits, string>>(emptyValues)
  const [error, setError] = useState('')

  const fieldLabels: Record<keyof FinanceCandidateEdits, string> = {
    limit: copy.fieldLimit,
    qty: copy.fieldQty,
    sl: copy.fieldSl,
    stop: copy.fieldStop,
    tp: copy.fieldTp
  }

  useEffect(() => {
    if (entry) {
      setValues({
        limit: entry.candidate.limit?.toString() ?? '',
        qty: entry.candidate.qty.toString(),
        sl: entry.candidate.sl?.toString() ?? '',
        stop: entry.candidate.stop?.toString() ?? '',
        tp: entry.candidate.tp?.toString() ?? ''
      })
      setError('')
    }
  }, [entry])

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    const edits: FinanceCandidateEdits = {}

    for (const key of EDIT_FIELD_KEYS) {
      const raw = values[key].trim()

      if (!raw) {
        continue
      }

      const parsed = Number(raw)

      if (!Number.isFinite(parsed) || parsed <= 0) {
        setError(copy.positiveNumber(fieldLabels[key]))

        return
      }

      edits[key] = parsed
    }

    if (Object.keys(edits).length === 0) {
      setError(copy.atLeastOneEdit)

      return
    }

    setError('')
    onSubmit(edits)
  }

  return (
    <Dialog onOpenChange={value => !value && !submitting && onClose()} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{entry ? copy.editTitleFor(entry.candidate.symbol) : copy.editTitle}</DialogTitle>
          <DialogDescription>{copy.editDescription}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <div className="grid grid-cols-2 gap-3">
            {EDIT_FIELD_KEYS.map(key => (
              <div className="grid gap-1.5" key={key}>
                <label className="text-xs font-medium text-foreground" htmlFor={`finance-edit-${key}`}>
                  {fieldLabels[key]}
                </label>
                <Input
                  id={`finance-edit-${key}`}
                  inputMode="decimal"
                  onChange={event => setValues(prev => ({ ...prev, [key]: event.target.value }))}
                  placeholder="—"
                  value={values[key]}
                />
              </div>
            ))}
          </div>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={submitting} type="submit">
              {submitting ? copy.saving : copy.saveApprove}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
