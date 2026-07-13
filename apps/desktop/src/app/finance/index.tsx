import { useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { ErrorState } from '@/components/ui/error-state'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { type FinanceHealth, type FinanceMode, getFinanceHealth } from '@/hermes'
import { useI18n } from '@/i18n'
import { AlertTriangle, RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { PageSearchShell } from '../page-search-shell'
import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'

import { FinanceAccountTab } from './account'
import { FinanceHistoryTab } from './history'
import { BREAKER_TONE, enumLabel, FINANCE_KEY, financeKey, fmtTs, parseFinanceError } from './lib'
import { FinanceMarketTab } from './market'
import { FinancePill } from './primitives'
import { FinanceQueue, usePendingCandidates } from './queue'
import { FinanceReportsTab } from './reports'
import { FinanceResearchTab } from './research'

// Permanent Finance portal (Loop.md §5.9): a native, structured companion
// surface over the swing-trader service. Phase 0.5 (Loop.md §7) makes the
// Investment Research brief the DEFAULT canvas — research and risk awareness
// are primary; the approval queue is a compact, badged SECONDARY action area
// (approve/edit/reject only, Loop.md §5.6); account/market/history/reports
// live under a third Portfolio tab.

const TABS = ['research', 'queue', 'portfolio'] as const

type FinanceTabId = (typeof TABS)[number]

// Portfolio sub-sections (the Phase-0 tabs, demoted under one tab).
const PORTFOLIO_SECTIONS = ['account', 'market', 'history', 'reports'] as const

type PortfolioSectionId = (typeof PORTFOLIO_SECTIONS)[number]

// Portfolio sections where the shared search field filters rows by symbol/theme.
const SEARCHABLE_SECTIONS: ReadonlySet<PortfolioSectionId> = new Set(['account', 'history', 'market'])

interface FinanceViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
}

export function FinanceView({ setStatusbarItemGroup: _setStatusbarItemGroup, ...props }: FinanceViewProps) {
  const { t } = useI18n()
  const copy = t.finance
  const queryClient = useQueryClient()
  const [tab, setTab] = useRouteEnumParam('tab', TABS, 'research')
  const [section, setSection] = useRouteEnumParam('view', PORTFOLIO_SECTIONS, 'account')
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

  const searchable = tab === 'portfolio' && SEARCHABLE_SECTIONS.has(section) && online

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
      searchPlaceholder={copy.searchPlaceholder}
      searchValue={query}
      tabs={TABS.map(id => ({
        id,
        label: copy.tabs[id],
        // The queue tab is the badged action area (Loop.md §7 Phase 0.5): its
        // label always carries the pending count while the service is up.
        meta: id === 'queue' && online ? (pendingQuery.isPending ? null : pendingCount || undefined) : undefined
      }))}
    >
      <div className="h-full min-h-0 overflow-y-auto px-4 pb-6">
        <div className="mx-auto w-full max-w-4xl space-y-4 pt-2">
          {health?.breaker === 'TRIPPED' && <BreakerBanner />}

          {healthQuery.isPending ? (
            <PageLoader label={copy.connecting} />
          ) : offline ? (
            <FinanceOfflinePanel
              error={healthQuery.error}
              onRetry={() => void healthQuery.refetch()}
              retrying={healthQuery.isFetching}
            />
          ) : (
            <>
              {tab === 'research' && <FinanceResearchTab enabled={online} onOpenQueue={() => setTab('queue')} />}
              {tab === 'queue' && <FinanceQueue enabled={online} />}
              {tab === 'portfolio' && (
                <PortfolioTab enabled={online} mode={mode} onSectionChange={setSection} query={query} section={section} />
              )}
            </>
          )}
        </div>
      </div>
    </PageSearchShell>
  )
}

// Account / market / history / reports under one secondary tab — kept intact
// from Phase 0, just demoted below the research brief (Loop.md §7 Phase 0.5).
function PortfolioTab({
  enabled,
  mode,
  onSectionChange,
  query,
  section
}: {
  enabled: boolean
  mode: FinanceMode
  onSectionChange: (section: PortfolioSectionId) => void
  query: string
  section: PortfolioSectionId
}) {
  const { t } = useI18n()

  return (
    <div className="space-y-4">
      <SegmentedControl
        onChange={onSectionChange}
        options={PORTFOLIO_SECTIONS.map(id => ({ id, label: t.finance.sections[id] }))}
        value={section}
      />

      {section === 'account' && <FinanceAccountTab enabled={enabled} mode={mode} query={query} />}
      {section === 'market' && <FinanceMarketTab enabled={enabled} query={query} />}
      {section === 'history' && <FinanceHistoryTab enabled={enabled} mode={mode} query={query} />}
      {section === 'reports' && <FinanceReportsTab enabled={enabled} />}
    </div>
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
  const { t } = useI18n()
  const copy = t.finance

  return (
    <div className="flex w-full flex-wrap items-center gap-x-3 gap-y-1.5">
      <span className="inline-flex items-center gap-1.5 text-xs text-(--ui-text-secondary)">
        <StatusDot tone={offline ? 'bad' : health ? 'good' : 'muted'} />
        {offline ? copy.serviceOffline : health ? copy.serviceOnline : copy.serviceConnecting}
      </span>

      <SegmentedControl
        onChange={onModeChange}
        options={[
          { id: 'paper' as const, label: copy.modePaper },
          { id: 'live' as const, label: copy.modeLive }
        ]}
        value={mode}
      />

      {health && (
        <>
          <FinancePill variant={health.breaker === 'TRIPPED' ? 'destructive' : 'muted'}>
            <StatusDot tone={BREAKER_TONE[health.breaker] ?? 'muted'} />
            {copy.breakerPill(enumLabel(t.finance.enums.breaker, health.breaker))}
          </FinancePill>
          <FinancePill variant={health.loop_attached ? 'default' : 'muted'}>
            {health.loop_attached ? copy.loopAttached : copy.loopIdle}
          </FinancePill>
          <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">{copy.asOf(fmtTs(health.ts))}</span>
        </>
      )}

      <Button className="ml-auto" onClick={onRefresh} size="xs" variant="ghost">
        <RefreshCw className="size-3" />
        {t.common.refresh}
      </Button>
    </div>
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
