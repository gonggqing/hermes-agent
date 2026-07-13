import { useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { type FinanceMode, getFinanceHealth } from '@/hermes'
import { useI18n } from '@/i18n'
import { AlertTriangle, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { PageSearchShell } from '../page-search-shell'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import { FinanceBottomBar } from './chrome'
import { FINANCE_KEY, financeKey, parseFinanceError } from './lib'
import { FinancePortfolioView } from './portfolio'
import { FinanceQueueView, usePendingCandidates } from './queue'
import { FinanceResearchView } from './research'

// Permanent Finance portal (Loop.md §5.9): a native, structured companion
// surface over the swing-trader service. Each top tab (Research, Queue,
// Portfolio) is its own MESSAGING-style master-detail — a grouped sidebar
// list, a detail pane, and a bottom paper/live switcher — reusing the existing
// brief/queue/account logic unchanged; this is a layout restructure, not a
// rewrite of the data or approval flow (approve/edit/reject stay §5.6-exact).

const TABS = ['research', 'queue', 'portfolio'] as const

type FinanceTabId = (typeof TABS)[number]

interface FinanceViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function FinanceView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: FinanceViewProps) {
  const { t } = useI18n()
  const copy = t.finance
  const queryClient = useQueryClient()
  const [tab, setTab] = useRouteEnumParam('tab', TABS, 'research')
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

  // The service status / breaker / loop / last-updated / refresh cluster and
  // the single paper/live toggle now live in ONE bottom utility bar, docked as
  // each view's DetailColumn actionBar (out of the top header).
  const bottomBar = (
    <FinanceBottomBar
      health={health}
      mode={mode}
      modeOverride={modeOverride}
      offline={offline}
      onModeChange={setModeOverride}
      onRefresh={refreshAll}
    />
  )

  return (
    <PageSearchShell
      {...props}
      activeTab={tab}
      onSearchChange={() => undefined}
      onTabChange={next => setTab(next as FinanceTabId)}
      searchHidden
      searchPlaceholder={copy.searchPlaceholder}
      searchValue=""
      tabs={TABS.map(id => ({
        id,
        label: copy.tabs[id],
        // The queue tab is the badged action area (Loop.md §7 Phase 0.5): its
        // label always carries the pending count while the service is up.
        meta: id === 'queue' && online ? (pendingQuery.isPending ? null : pendingCount || undefined) : undefined
      }))}
    >
      {healthQuery.isPending ? (
        <PageLoader label={copy.connecting} />
      ) : offline ? (
        <div className="h-full overflow-y-auto px-4 py-6">
          <div className="mx-auto w-full max-w-2xl">
            <FinanceOfflinePanel
              error={healthQuery.error}
              onRetry={() => void healthQuery.refetch()}
              retrying={healthQuery.isFetching}
            />
          </div>
        </div>
      ) : (
        <div className="flex h-full min-h-0 flex-col">
          {health?.breaker === 'TRIPPED' && (
            <div className="shrink-0 px-4 pt-3">
              <BreakerBanner />
            </div>
          )}
          <div className="min-h-0 flex-1">
            {tab === 'research' && (
              <FinanceResearchView bottomBar={bottomBar} enabled={online} onOpenQueue={() => setTab('queue')} />
            )}
            {tab === 'queue' && <FinanceQueueView bottomBar={bottomBar} enabled={online} />}
            {tab === 'portfolio' && <FinancePortfolioView bottomBar={bottomBar} enabled={online} mode={mode} />}
          </div>
        </div>
      )}
    </PageSearchShell>
  )
}

function BreakerBanner() {
  const { t } = useI18n()

  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-3 py-2.5',
        'text-xs leading-5 text-destructive'
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <div>
        <div className="font-semibold">{t.finance.breakerBannerTitle}</div>
        <div className="text-destructive/80">{t.finance.breakerBannerBody}</div>
      </div>
    </div>
  )
}

// The finance service is a separate process and is legitimately down most of
// the time (evenings/weekends). Render that as a calm empty state, never an
// error crash; health keeps polling so this heals on its own.
function FinanceOfflinePanel({ error, onRetry, retrying }: { error: unknown; onRetry: () => void; retrying: boolean }) {
  const { t } = useI18n()
  const copy = t.finance
  const parsed = parseFinanceError(error)

  return (
    <div className="grid min-h-64 place-items-center py-10">
      <ErrorState description={parsed.offline ? copy.offlineHint : parsed.message} title={copy.offlineTitle}>
        <div className="flex justify-center">
          <Button disabled={retrying} onClick={onRetry} size="sm" variant="outline">
            <RefreshCw className={cn('size-3.5', retrying && 'animate-spin')} />
            {retrying ? copy.offlineChecking : copy.offlineRetry}
          </Button>
        </div>
      </ErrorState>
    </div>
  )
}
