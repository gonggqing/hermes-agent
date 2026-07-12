import { useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { type FinanceHealth, type FinanceMode, getFinanceHealth } from '@/hermes'
import { AlertTriangle, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { PageSearchShell } from '../page-search-shell'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import { FinanceAccountTab } from './account'
import { FinanceHistoryTab } from './history'
import { BREAKER_TONE, FINANCE_KEY, financeKey, fmtTs, parseFinanceError } from './lib'
import { FinanceMarketTab } from './market'
import { FinancePill } from './primitives'
import { FinanceQueue, usePendingCandidates } from './queue'
import { FinanceReportsTab } from './reports'

// Permanent Finance portal (Loop.md §5.9): a native, structured companion
// surface over the swing-trader service — approval queue, account/positions,
// market regime, history/audit, daily reports. Read-only except the
// confirmation-service candidate actions in the queue tab (Loop.md §5.6).

const TABS = ['queue', 'account', 'market', 'history', 'reports'] as const

type FinanceTabId = (typeof TABS)[number]

const TAB_LABELS: Record<FinanceTabId, string> = {
  account: 'Account',
  history: 'History',
  market: 'Market',
  queue: 'Queue',
  reports: 'Reports'
}

// Tabs where the shared search field filters rows by symbol/theme.
const SEARCHABLE_TABS: ReadonlySet<FinanceTabId> = new Set(['account', 'history', 'market'])

const MODE_OPTIONS = [
  { id: 'paper', label: 'Paper' },
  { id: 'live', label: 'Live' }
] as const

interface FinanceViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function FinanceView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: FinanceViewProps) {
  const queryClient = useQueryClient()
  const [tab, setTab] = useRouteEnumParam('tab', TABS, 'queue')
  const [query, setQuery] = useState('')
  // null = follow the service's own mode from /health; set = explicit override.
  const [modeOverride, setModeOverride] = useState<FinanceMode | null>(null)

  const healthQuery = useQuery({
    queryFn: getFinanceHealth,
    queryKey: financeKey('health'),
    // Keep probing while offline so the portal recovers by itself once the
    // service starts (`cd trader && uv run python -m swing_trader serve`).
    refetchInterval: 30_000,
    retry: 1
  })

  const health = healthQuery.data
  const offline = healthQuery.isError
  const online = Boolean(health) && !offline
  const mode: FinanceMode = modeOverride ?? health?.mode ?? 'paper'

  // Shared with the queue tab (same query key → one fetch) so the tab badge
  // and the list never disagree.
  const pendingQuery = usePendingCandidates(online)
  const pendingCount = pendingQuery.data?.length

  const refreshAll = () => void queryClient.invalidateQueries({ queryKey: FINANCE_KEY })

  useRefreshHotkey(refreshAll)

  const searchable = SEARCHABLE_TABS.has(tab) && online

  return (
    <PageSearchShell
      {...props}
      activeTab={tab}
      filters={
        <FinanceHealthStrip
          health={health}
          mode={mode}
          offline={offline}
          onModeChange={setModeOverride}
          onRefresh={refreshAll}
        />
      }
      onSearchChange={setQuery}
      onTabChange={next => setTab(next as FinanceTabId)}
      searchHidden={!searchable}
      searchPlaceholder="Filter by symbol or theme"
      searchValue={query}
      tabs={TABS.map(id => ({
        id,
        label: TAB_LABELS[id],
        meta: id === 'queue' && online ? (pendingQuery.isPending ? null : pendingCount || undefined) : undefined
      }))}
    >
      <div className="h-full min-h-0 overflow-y-auto px-4 pb-6">
        <div className="mx-auto w-full max-w-4xl space-y-4 pt-2">
          {health?.breaker === 'TRIPPED' && <BreakerBanner />}

          {healthQuery.isPending ? (
            <PageLoader label="Connecting to finance service…" />
          ) : offline ? (
            <FinanceOfflinePanel
              error={healthQuery.error}
              onRetry={() => void healthQuery.refetch()}
              retrying={healthQuery.isFetching}
            />
          ) : (
            <>
              {tab === 'queue' && <FinanceQueue enabled={online} />}
              {tab === 'account' && <FinanceAccountTab enabled={online} mode={mode} query={query} />}
              {tab === 'market' && <FinanceMarketTab enabled={online} query={query} />}
              {tab === 'history' && <FinanceHistoryTab enabled={online} mode={mode} query={query} />}
              {tab === 'reports' && <FinanceReportsTab enabled={online} />}
            </>
          )}
        </div>
      </div>
    </PageSearchShell>
  )
}

// Header strip under the tabs: service status, paper/live switch, breaker and
// loop state — always visible so mode is never ambiguous (paper/live ledgers
// are strictly separate, Loop.md §5.8).
function FinanceHealthStrip({
  health,
  mode,
  offline,
  onModeChange,
  onRefresh
}: {
  health: FinanceHealth | undefined
  mode: FinanceMode
  offline: boolean
  onModeChange: (mode: FinanceMode) => void
  onRefresh: () => void
}) {
  return (
    <div className="flex w-full flex-wrap items-center gap-x-3 gap-y-1.5">
      <span className="inline-flex items-center gap-1.5 text-xs text-(--ui-text-secondary)">
        <StatusDot tone={offline ? 'bad' : health ? 'good' : 'muted'} />
        {offline ? 'Service offline' : health ? 'Service online' : 'Connecting…'}
      </span>

      <SegmentedControl onChange={onModeChange} options={MODE_OPTIONS} value={mode} />

      {health && (
        <>
          <FinancePill variant={health.breaker === 'TRIPPED' ? 'destructive' : 'muted'}>
            <StatusDot tone={BREAKER_TONE[health.breaker] ?? 'muted'} />
            breaker {health.breaker}
          </FinancePill>
          <FinancePill variant={health.loop_attached ? 'default' : 'muted'}>
            {health.loop_attached ? 'loop attached' : 'loop idle'}
          </FinancePill>
          <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">as of {fmtTs(health.ts)}</span>
        </>
      )}

      <Button className="ml-auto" onClick={onRefresh} size="xs" variant="ghost">
        <RefreshCw className="size-3" />
        Refresh
      </Button>
    </div>
  )
}

function BreakerBanner() {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5',
        'text-xs leading-5 text-destructive'
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <div>
        <div className="font-semibold">Circuit breaker TRIPPED — no new entries today.</div>
        <div className="text-destructive/80">
          The −4% daily drawdown guardrail halted new entries (Loop.md §3). Exits and resting protection remain
          active. The breaker resets with the next trading day.
        </div>
      </div>
    </div>
  )
}

// The finance service is a separate process and is legitimately down most of
// the time (evenings/weekends). Render that as a calm empty state, never an
// error crash; health keeps polling so this heals on its own.
function FinanceOfflinePanel({ error, onRetry, retrying }: { error: unknown; onRetry: () => void; retrying: boolean }) {
  const parsed = parseFinanceError(error)

  return (
    <div className="grid min-h-64 place-items-center py-10">
      <ErrorState
        description={
          parsed.offline
            ? 'The swing-trader service is not running. Start it with: cd trader && uv run python -m swing_trader serve'
            : parsed.message
        }
        title="Finance service offline"
      >
        <div className="flex justify-center">
          <Button disabled={retrying} onClick={onRetry} size="sm" variant="outline">
            <RefreshCw className={cn('size-3.5', retrying && 'animate-spin')} />
            {retrying ? 'Checking…' : 'Retry now'}
          </Button>
        </div>
      </ErrorState>
    </div>
  )
}
