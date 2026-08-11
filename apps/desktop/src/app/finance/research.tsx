import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Tip } from '@/components/ui/tooltip'
import {
  createResearchWatchlist,
  type FinanceBriefPendingCandidate,
  type FinanceDiscoveryPool,
  type FinanceFreshness,
  type FinanceMover,
  type FinanceNewsDigestItem,
  type FinanceProvenanceLink,
  type FinanceRegimeView,
  type FinanceResearchBrief,
  type FinanceResearchWatchlist,
  type FinanceRiskView,
  type FinanceSignalView,
  type FinanceThemeView,
  getFinanceResearchBrief,
  getResearchWatchlists,
  postFinanceResearchRun,
  searchFinanceKnowledge
} from '@/hermes'
import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/external-link'
import { Activity, AlertTriangle, BarChart3, FileText, Info, RefreshCw, Search } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { DetailColumn, ListColumn, MasterDetail } from '../master-detail'

import { FinanceListGroup, FinanceNavRow, FinanceRowGlyph } from './chrome'
import {
  enumLabel,
  financeKey,
  fmtMoney,
  fmtPct,
  fmtPrice,
  fmtQty,
  fmtSignedMoney,
  fmtSignedPct,
  fmtTs,
  parseFinanceError,
  pnlClass,
  REGIME_TONE
} from './lib'
import { PredictionReviewPanel, StrategyBacktestsPanel } from './prediction-review'
import { FinanceCard, FinancePill, FinanceSectionLabel, QuerySection, StatTile } from './primitives'
import { WATCH_MODULE_IDS, type WatchModuleId, WatchModulePanel } from './watch'
import { CustomWatchlistPanel } from './watchlists'

// Investment Research — the DEFAULT Finance view (Loop.md §7 Phase 0.5):
// research and risk awareness are primary; execution stays in the secondary
// action-queue tab. Everything here is read-only: the brief carries as-of
// times, PAPER/LIVE mode, explicit freshness/staleness, citations and an
// uncertainty section, and this surface adds no authority beyond rendering it.

const BRIEF_POLL_MS = 60_000

// Knowledge search results per query (server clamps k to 1..25).
const SEARCH_K = 5

type ResearchCopy = ReturnType<typeof useI18n>['t']['finance']['research']

function ExplainedSectionLabel({ children, description }: { children: React.ReactNode; description: string }) {
  return (
    <div className="flex items-center gap-1.5">
      <FinanceSectionLabel>{children}</FinanceSectionLabel>
      <Tip label={description} side="top">
        <button
          aria-label={description}
          className="inline-flex size-6 cursor-help items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          type="button"
        >
          <Info className="size-3.5" />
        </button>
      </Tip>
    </div>
  )
}

function explainDiscoveryReason(reason: string, copy: ResearchCopy): string {
  const trend = reason.match(/^20d trend\s+(.+)$/i)

  if (trend) {
    return copy.discoveryReasonTrend(trend[1])
  }

  const relative = reason.match(/^relative strength\s+(.+)$/i)

  if (relative) {
    return copy.discoveryReasonRelativeStrength(relative[1])
  }

  const volume = reason.match(/^volume\s+(.+)\s+20d average$/i)

  if (volume) {
    return copy.discoveryReasonVolume(volume[1])
  }

  return reason
}

// Selectable sidebar items. The four active markets in the product-defined
// order (US, HK, China, Korea Semiconductor), followed by the
// read-only watch modules; disabled market placeholders (UK/Korea/Japan) are
// NOT selectable so they stay out of the enum.
const ACTIVE_MARKETS = ['us', 'hk', 'china', 'korea'] as const

const RESEARCH_DESKS = [...ACTIVE_MARKETS, 'predictions', 'backtests', ...WATCH_MODULE_IDS] as const

type ResearchDesk = (typeof RESEARCH_DESKS)[number]

// UK/Japan placeholders were dropped (human directive 2026-07-14 — keep only
// KR, semiconductor-focused); KR is now an active desk above.

const isMarketDesk = (desk: ResearchDesk): desk is (typeof ACTIVE_MARKETS)[number] =>
  (ACTIVE_MARKETS as readonly string[]).includes(desk)

// DESKTOP-ONLY sidebar glyphs (region markers + asset marks) rendered as the
// leading chip on each Finance row — mirrors the messaging PlatformAvatar.
// Markets use recognizable flag emoji; the coming-soon placeholders render
// muted. Each accent tints the chip behind its glyph.
const MARKET_GLYPH: Record<string, { color: string; emoji: string }> = {
  us: { color: '#3B82F6', emoji: '🇺🇸' },
  china: { color: '#EF4444', emoji: '🇨🇳' },
  hk: { color: '#F43F5E', emoji: '🇭🇰' },
  uk: { color: '#6366F1', emoji: '🇬🇧' },
  korea: { color: '#0EA5E9', emoji: '🇰🇷' },
  japan: { color: '#EC4899', emoji: '🇯🇵' }
}

interface FinanceViewCommonProps {
  bottomBar: React.ReactNode
  enabled: boolean
}

export function FinanceResearchView({
  bottomBar,
  enabled,
  onOpenQueue
}: FinanceViewCommonProps & { onOpenQueue: () => void }) {
  const { t } = useI18n()
  const copy = t.finance.research
  const [desk] = useRouteEnumParam('desk', RESEARCH_DESKS, 'us')
  const { hash, pathname, search } = useLocation()
  const navigate = useNavigate()
  const customGroupId = new URLSearchParams(search).get('watch_group')

  const setCustomGroupId = (id: null | string) => {
    const params = new URLSearchParams(search)

    if (id) {
      params.set('watch_group', id)
    } else {
      params.delete('watch_group')
    }

    const query = params.toString()
    navigate({ hash, pathname, search: query ? `?${query}` : '' }, { replace: true })
  }

  const selectDesk = (nextDesk: ResearchDesk) => {
    const params = new URLSearchParams(search)

    if (nextDesk === 'us') {
      params.delete('desk')
    } else {
      params.set('desk', nextDesk)
    }

    params.delete('watch_group')

    const query = params.toString()
    navigate({ hash, pathname, search: query ? `?${query}` : '' }, { replace: true })
  }

  const [creatingGroup, setCreatingGroup] = useState(false)
  const [newGroupName, setNewGroupName] = useState('')

  const watchlistsQuery = useQuery({
    enabled,
    queryFn: getResearchWatchlists,
    queryKey: financeKey('research', 'watchlists'),
    retry: 1
  })

  const customGroups = watchlistsQuery.data ?? []
  const selectedCustomGroup = customGroups.find(group => group.id === customGroupId) ?? null
  const marketDesk = isMarketDesk(desk)
  const researchOnly = marketDesk && desk !== 'us'
  const marketKey = desk === 'us' ? 'us' : desk === 'korea' ? 'kr' : desk === 'hk' ? 'hk' : 'cn'

  const briefQuery = useQuery({
    enabled: enabled && marketDesk && !customGroupId,
    queryFn: () => getFinanceResearchBrief(marketKey),
    queryKey: financeKey('research', 'brief', marketKey),
    refetchInterval: BRIEF_POLL_MS,
    retry: 1
  })

  const brief = briefQuery.data

  const marketLabel: Record<(typeof ACTIVE_MARKETS)[number], string> = {
    us: copy.marketUs,
    china: copy.marketChina,
    hk: copy.marketHk,
    korea: copy.marketKorea
  }

  // Manual "run research now": every active market, including US, has a
  // registered read-only callback. Review/watch/custom desks do not.
  const queryClient = useQueryClient()
  const canRun = !selectedCustomGroup && marketDesk

  const runMutation = useMutation({
    mutationFn: () => postFinanceResearchRun(marketKey),
    onError: error => notifyError(error instanceof Error ? error : new Error(String(error)), copy.runResearchFailed),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: financeKey('research', 'brief', marketKey) })
      notify({
        kind: 'success',
        message: result.market_label,
        title: copy.runResearchDone
      })
    }
  })

  const createGroupMutation = useMutation({
    mutationFn: () => createResearchWatchlist(newGroupName),
    onSuccess: created => {
      queryClient.setQueryData<FinanceResearchWatchlist[]>(financeKey('research', 'watchlists'), groups => [
        ...(groups ?? []),
        created
      ])
      setCreatingGroup(false)
      setNewGroupName('')
      setCustomGroupId(created.id)
    }
  })

  const updateCustomGroup = (updated: FinanceResearchWatchlist) =>
    queryClient.setQueryData<FinanceResearchWatchlist[]>(financeKey('research', 'watchlists'), groups =>
      (groups ?? []).map(group => (group.id === updated.id ? updated : group))
    )

  const deleteCustomGroup = (id: string) => {
    queryClient.setQueryData<FinanceResearchWatchlist[]>(financeKey('research', 'watchlists'), groups =>
      (groups ?? []).filter(group => group.id !== id)
    )
    setCustomGroupId(null)
  }

  return (
    <MasterDetail>
      <ListColumn>
        <FinanceListGroup label={copy.marketsGroup}>
          {ACTIVE_MARKETS.map(id => (
            <FinanceNavRow
              active={!customGroupId && desk === id}
              key={id}
              leading={<FinanceRowGlyph color={MARKET_GLYPH[id].color} emoji={MARKET_GLYPH[id].emoji} />}
              onSelect={() => selectDesk(id)}
              title={marketLabel[id]}
            />
          ))}
        </FinanceListGroup>

        <FinanceListGroup label={t.finance.prediction.groupLabel}>
          <FinanceNavRow
            active={!customGroupId && desk === 'predictions'}
            leading={<FinanceRowGlyph color="#8B5CF6" icon={BarChart3} />}
            onSelect={() => selectDesk('predictions')}
            title={t.finance.prediction.navLabel}
          />
          <FinanceNavRow
            active={!customGroupId && desk === 'backtests'}
            leading={<FinanceRowGlyph color="#64748B" icon={Activity} />}
            onSelect={() => selectDesk('backtests')}
            title={t.finance.prediction.backtests}
          />
        </FinanceListGroup>

        <FinanceListGroup label={copy.watchGroup}>
          {WATCH_MODULE_IDS.map(id => (
            <FinanceNavRow
              active={!customGroupId && desk === id}
              key={id}
              onSelect={() => selectDesk(id)}
              title={t.finance.watch.modules[id]}
            />
          ))}
        </FinanceListGroup>

        <FinanceListGroup label={t.finance.watch.custom.groupLabel}>
          {customGroups.map(group => (
            <FinanceNavRow
              active={customGroupId === group.id}
              key={group.id}
              onSelect={() => setCustomGroupId(group.id)}
              title={group.name}
            />
          ))}
          {creatingGroup ? (
            <div className="space-y-2 border-t border-(--ui-stroke-tertiary) p-2">
              <Input
                autoFocus
                onChange={event => setNewGroupName(event.target.value)}
                onKeyDown={event => event.key === 'Enter' && newGroupName.trim() && createGroupMutation.mutate()}
                placeholder={t.finance.watch.custom.namePlaceholder}
                value={newGroupName}
              />
              <div className="flex gap-2">
                <Button
                  disabled={!newGroupName.trim() || createGroupMutation.isPending}
                  onClick={() => createGroupMutation.mutate()}
                  size="xs"
                >
                  {t.common.confirm}
                </Button>
                <Button onClick={() => setCreatingGroup(false)} size="xs" variant="ghost">
                  {t.common.cancel}
                </Button>
              </div>
            </div>
          ) : (
            <FinanceNavRow
              active={false}
              onSelect={() => {
                setNewGroupName(t.finance.watch.custom.newGroup)
                setCreatingGroup(true)
              }}
              title={t.finance.watch.custom.newGroup}
            />
          )}
        </FinanceListGroup>
      </ListColumn>

      {/* The watch desks own the full-bleed K-chart, so drop the centered
          max-w column and let the candles fill the pane edge to edge. */}
      <DetailColumn
        actionBar={bottomBar}
        bleed={Boolean(selectedCustomGroup) || (!marketDesk && desk !== 'predictions' && desk !== 'backtests')}
      >
        {selectedCustomGroup ? (
          <CustomWatchlistPanel
            enabled={enabled}
            group={selectedCustomGroup}
            onChange={updateCustomGroup}
            onDelete={deleteCustomGroup}
          />
        ) : marketDesk ? (
          <div className="space-y-5">
            <QuerySection
              empty={copy.briefError}
              error={briefQuery.isError ? briefQuery.error : undefined}
              isEmpty={!brief}
              loading={briefQuery.isPending}
            >
              {brief && (
                <BriefBody
                  brief={brief}
                  onOpenQueue={onOpenQueue}
                  onRunResearch={canRun ? () => runMutation.mutate() : undefined}
                  researchEnabled={enabled}
                  researchOnly={researchOnly}
                  researchRunning={runMutation.isPending}
                />
              )}
            </QuerySection>
            <KnowledgeSearchSection enabled={enabled} />
          </div>
        ) : desk === 'predictions' ? (
          <PredictionReviewPanel enabled={enabled} />
        ) : desk === 'backtests' ? (
          <StrategyBacktestsPanel />
        ) : (
          <WatchModulePanel enabled={enabled} module={desk as WatchModuleId} />
        )}
      </DetailColumn>
    </MasterDetail>
  )
}

export function BriefBody({
  brief,
  onOpenQueue,
  onRunResearch,
  researchEnabled,
  researchRunning,
  researchOnly
}: {
  brief: FinanceResearchBrief
  onOpenQueue: () => void
  onRunResearch?: () => void
  researchEnabled: boolean
  researchRunning: boolean
  researchOnly: boolean
}) {
  return (
    <div className="space-y-5">
      <BriefHeader
        brief={brief}
        onRunResearch={onRunResearch}
        researchEnabled={researchEnabled}
        researchOnly={researchOnly}
        researchRunning={researchRunning}
      />
      <FreshnessBanner freshness={brief.freshness} />
      <NarrativeBrief brief={brief} />
      {/* Research-only markets carry no account/positions (risk===null), so the
          account-risk strip and the order-approval hand-off are hidden and a
          research-only note takes their place. */}
      {researchOnly ? <ResearchOnlyNote /> : <RiskSection risk={brief.risk} />}
      <RegimeSection regime={brief.regime} />
      <DiscoverySection pool={brief.discovery} />
      {researchOnly && <SynthesisSection synthesis={brief.cross_market_synthesis} />}
      <MoversSection bottom={brief.movers.bottom} top={brief.movers.top} />
      <ThemesSection themes={brief.themes} />
      <NewsSection news={brief.news} />
      <SignalsSection signals={brief.signals_today} />
      {!researchOnly && <CandidatesSection candidates={brief.candidates_today} onOpenQueue={onOpenQueue} />}
      <UncertaintySection items={brief.uncertainty} />
      <ProvenanceFooter links={brief.provenance} />
    </div>
  )
}

function NarrativeBrief({ brief }: { brief: FinanceResearchBrief }) {
  const { t } = useI18n()
  const copy = t.finance.research
  const narrative = brief.narrative

  return (
    <FinanceCard className="space-y-5 border-primary/30 bg-primary/5">
      <div className="flex items-start gap-2.5">
        <FileText className="mt-0.5 size-4 shrink-0 text-primary" />
        <div className="min-w-0">
          <div className="text-sm font-semibold text-foreground">{copy.narrativeTitle}</div>
          {narrative && (
            <p className="mt-1 text-[0.62rem] text-muted-foreground">
              {narrative.market} · {narrative.model}
            </p>
          )}
        </div>
      </div>
      {!narrative ? (
        <p className="text-xs leading-5 text-muted-foreground">{copy.narrativeUnavailable}</p>
      ) : (
        <>
          <div className="space-y-2">
            <h3 className="text-sm font-semibold leading-6 text-foreground">{narrative.headline}</h3>
            {narrative.summary.split(/\n{2,}/).map(paragraph => (
              <p className="text-xs leading-5 text-foreground/90" key={paragraph}>
                {paragraph}
              </p>
            ))}
          </div>
          {(narrative.change_summary ?? []).length > 0 && (
            <section className="border-t border-(--ui-stroke-tertiary) pt-3">
              <FinanceSectionLabel>{copy.narrativeSincePrior}</FinanceSectionLabel>
              <ul className="mt-2 space-y-2">
                {(narrative.change_summary ?? []).map(item => (
                  <li className="border-l-2 border-primary/30 pl-3 text-xs leading-5 text-foreground" key={item}>
                    {item}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {(narrative.action_views ?? []).length > 0 && (
            <section className="border-t border-(--ui-stroke-tertiary) pt-3">
              <FinanceSectionLabel>{copy.narrativeActionMap}</FinanceSectionLabel>
              <div className="mt-2 grid gap-2 sm:grid-cols-2">
                {(narrative.action_views ?? []).map(action => (
                  <FinanceCard className="space-y-2" key={`${action.symbol}-${action.stance}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-semibold text-foreground">{action.symbol}</span>
                      {action.display_name && (
                        <span className="text-[0.62rem] text-muted-foreground">{action.display_name}</span>
                      )}
                      <FinancePill variant="outline">
                        {copy.narrativeStances[action.stance] ?? action.stance}
                      </FinancePill>
                      <span className="text-[0.62rem] text-muted-foreground">
                        {copy.narrativeHorizon(action.horizon_sessions)} · {Math.round(action.confidence * 100)}%
                      </span>
                    </div>
                    <p className="text-xs leading-5 text-foreground">{action.what_changed}</p>
                    <p className="text-[0.68rem] leading-5 text-muted-foreground">{action.rationale}</p>
                    {action.invalidation && (
                      <p className="text-[0.62rem] leading-5 text-muted-foreground">
                        {copy.narrativeInvalidation}: {action.invalidation}
                      </p>
                    )}
                  </FinanceCard>
                ))}
              </div>
            </section>
          )}
          <div className="grid gap-3 sm:grid-cols-2">
            {narrative.sections.map(section => (
              <section className="border-l-2 border-primary/30 pl-3" key={section.title}>
                <FinanceSectionLabel>{section.title}</FinanceSectionLabel>
                <p className="mt-1 text-xs leading-5 text-foreground">{section.analysis}</p>
              </section>
            ))}
          </div>
          {narrative.watch_next.length > 0 && (
            <section className="border-t border-(--ui-stroke-tertiary) pt-3">
              <FinanceSectionLabel>{copy.narrativeWatchNext}</FinanceSectionLabel>
              <ul className="mt-2 grid gap-2 sm:grid-cols-2">
                {narrative.watch_next.map(item => (
                  <li
                    className="border-l border-(--ui-stroke-secondary) pl-3 text-xs leading-5 text-muted-foreground"
                    key={item}
                  >
                    {item}
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </FinanceCard>
  )
}

function SynthesisSection({ synthesis }: { synthesis: FinanceResearchBrief['cross_market_synthesis'] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  if (!synthesis) {
    return null
  }

  return (
    <section className="space-y-2">
      <div className="flex items-center gap-2">
        <FinanceSectionLabel>{copy.synthesisTitle}</FinanceSectionLabel>
        <FinancePill>{synthesis.status}</FinancePill>
      </div>
      <div className="flex flex-wrap gap-2">
        {Object.values(synthesis.markets).map(market => (
          <FinancePill key={market.market}>
            {market.market}:{' '}
            {market.available ? `${market.regime ?? 'unknown'} · ${market.freshness_status}` : 'missing'}
          </FinancePill>
        ))}
      </div>
      {synthesis.shared_themes.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.synthesisEmpty}</div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          {synthesis.shared_themes.map(theme => (
            <FinanceCard key={theme.theme}>
              <div className="text-xs font-medium text-foreground">{theme.theme}</div>
              <div className="mt-1 text-[0.62rem] text-muted-foreground">
                CN {theme.cn_symbols.join(', ') || '—'} · HK {theme.hk_symbols.join(', ') || '—'}
              </div>
            </FinanceCard>
          ))}
        </div>
      )}
    </section>
  )
}

function DiscoverySection({ pool }: { pool?: FinanceDiscoveryPool | null }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.discoveryDescription}>{copy.discoveryTitle}</ExplainedSectionLabel>
      {!pool || pool.candidates.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.discoveryEmpty}</div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
          {pool.candidates.map(candidate => (
            <FinanceCard className="space-y-2" key={candidate.symbol}>
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="text-xs font-semibold text-foreground">
                    #{candidate.rank} {candidate.symbol}
                  </div>
                  <div className="truncate text-[0.65rem] text-muted-foreground">{candidate.display_name}</div>
                </div>
                <FinancePill>{copy.discoveryScore(candidate.score.toFixed(1))}</FinancePill>
              </div>
              <div className="text-[0.68rem] text-foreground">
                {candidate.theme} · {candidate.component}
              </div>
              <div className="line-clamp-3 text-[0.65rem] leading-5 text-muted-foreground">
                {candidate.relationship}
              </div>
              <div className="text-[0.65rem] leading-5">
                <div className="text-muted-foreground/80">{copy.discoveryReasons}</div>
                {candidate.reasons.length > 0 ? (
                  <ul className="list-disc pl-4 text-muted-foreground">
                    {candidate.reasons.slice(0, 3).map(reason => (
                      <li key={reason}>{explainDiscoveryReason(reason, copy)}</li>
                    ))}
                  </ul>
                ) : (
                  <div className="text-muted-foreground">{copy.discoveryNoReasons}</div>
                )}
              </div>
              <div className="flex flex-wrap gap-2">
                {candidate.evidence.slice(0, 2).map(evidence => (
                  <ExternalLink className="text-[0.62rem] text-primary" href={evidence.url} key={evidence.url}>
                    {copy.discoverySource(evidence.source)}
                  </ExternalLink>
                ))}
              </div>
            </FinanceCard>
          ))}
        </div>
      )}
    </section>
  )
}

// Calm, informational note (not a warning) explaining the CN session is
// research-only — no account, positions, or approval queue for this market.
function ResearchOnlyNote() {
  const { t } = useI18n()

  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-2.5',
        'text-xs leading-5 text-(--ui-text-secondary)'
      )}
    >
      <Info className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
      <div className="min-w-0">{t.finance.research.cnResearchOnly}</div>
    </div>
  )
}

// ── Header: trading day, PAPER/LIVE, as-of, per-source freshness ─────────────

function BriefHeader({
  brief,
  onRunResearch,
  researchEnabled,
  researchOnly,
  researchRunning
}: {
  brief: FinanceResearchBrief
  onRunResearch?: () => void
  researchEnabled: boolean
  researchOnly: boolean
  researchRunning: boolean
}) {
  const { t } = useI18n()
  const copy = t.finance.research
  const { freshness } = brief

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
      <span className="text-base font-semibold text-foreground">{copy.briefTitle}</span>
      <span className="text-sm font-semibold tracking-tight text-foreground">
        {copy.tradingDay(brief.trading_date)}
      </span>
      {researchOnly ? (
        <FinancePill variant="muted">{copy.cnBadge}</FinancePill>
      ) : (
        <FinancePill variant={brief.mode === 'live' ? 'warn' : 'outline'}>
          {brief.mode === 'live' ? copy.modeLive : copy.modePaper}
        </FinancePill>
      )}
      <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">{copy.briefAsOf(fmtTs(brief.as_of))}</span>

      <span className="flex flex-wrap items-center gap-1">
        <FreshnessPill
          ageMinutes={freshness.market_age_minutes}
          label={copy.freshMarket}
          stale={freshness.market_stale}
        />
        <FreshnessPill ageMinutes={freshness.news_age_minutes} label={copy.freshNews} stale={freshness.news_stale} />
        <FreshnessPill
          ageMinutes={freshness.portfolio_age_minutes}
          label={copy.freshPortfolio}
          stale={freshness.portfolio_stale}
        />
      </span>
      {onRunResearch && (
        <Button
          className="ml-auto"
          disabled={!researchEnabled || researchRunning}
          onClick={onRunResearch}
          size="xs"
          variant="outline"
        >
          <RefreshCw className={cn('h-3.5 w-3.5', researchRunning && 'animate-spin')} />
          {researchRunning ? copy.runningResearch : copy.runResearch}
        </Button>
      )}
    </div>
  )
}

function FreshnessPill({ ageMinutes, label, stale }: { ageMinutes: null | number; label: string; stale: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <FinancePill variant={stale ? 'warn' : 'muted'}>
      <StatusDot tone={stale ? 'warn' : 'good'} />
      {label} · {ageMinutes === null ? copy.freshMissing : copy.freshAge(Math.round(ageMinutes))}
    </FinancePill>
  )
}

// Stale or missing data is an explicit warning, never silently presented as
// current (Loop.md §5.9) — surface every server freshness warning verbatim.
function FreshnessBanner({ freshness }: { freshness: FinanceFreshness }) {
  const { t } = useI18n()

  if (freshness.warnings.length === 0) {
    return null
  }

  return <WarnBanner items={freshness.warnings} title={t.finance.research.staleTitle} />
}

function WarnBanner({ items, title }: { items: string[]; title?: string }) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2.5',
        'text-xs leading-5 text-amber-700 dark:text-amber-300'
      )}
    >
      <AlertTriangle className="mt-0.5 size-4 shrink-0" />
      <div className="min-w-0">
        {title && <div className="font-semibold">{title}</div>}
        <ul className="list-inside list-disc">
          {items.map(item => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </div>
    </div>
  )
}

// ── Risk strip ───────────────────────────────────────────────────────────────

function RiskSection({ risk }: { risk: FinanceRiskView | null }) {
  const { t } = useI18n()
  const copy = t.finance.research
  const account = t.finance.account

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.riskDescription}>{copy.riskTitle}</ExplainedSectionLabel>

      {!risk ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.riskEmpty}</div>
      ) : (
        <>
          {risk.breaker_state === 'TRIPPED' && (
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
          )}

          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
            <StatTile label={account.equity} value={fmtMoney(risk.equity)} />
            <StatTile label={account.cash} value={fmtMoney(risk.cash)} />
            <StatTile label={account.dayPnl} tone={pnlClass(risk.day_pnl)} value={fmtSignedMoney(risk.day_pnl)} />
            <StatTile
              label={account.drawdown}
              tone={risk.drawdown_pct < 0 ? 'text-destructive' : undefined}
              value={fmtPct(risk.drawdown_pct)}
            />
            <StatTile
              label={account.breaker}
              tone={risk.breaker_state === 'TRIPPED' ? 'text-destructive' : undefined}
              value={enumLabel(t.finance.enums.breaker, risk.breaker_state)}
            />
          </div>

          <RiskStatsRow stats={risk.stats} />

          {Object.keys(risk.pool_exposure_pct).length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 text-[0.65rem] text-muted-foreground">
              <span>{copy.poolExposure}</span>
              {Object.entries(risk.pool_exposure_pct).map(([pool, pct]) => (
                <FinancePill key={pool} variant="outline">
                  {pool} {fmtPct(pct)}
                </FinancePill>
              ))}
            </div>
          )}

          {risk.warnings.length > 0 && <WarnBanner items={risk.warnings} />}
        </>
      )}
    </section>
  )
}

// {n_closed, win_rate, expectancy, max_drawdown_pct} from Ledger.stats; the
// map is empty when the ledger accessor failed (already listed as unknown).
function RiskStatsRow({ stats }: { stats: Record<string, number> }) {
  const { t } = useI18n()
  const account = t.finance.account

  if (Object.keys(stats).length === 0) {
    return null
  }

  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <StatTile label={account.statClosedWins} value={fmtQty(stats.n_closed)} />
      <StatTile label={account.statWinRate} value={fmtPct(stats.win_rate * 100, 0)} />
      <StatTile
        label={account.statExpectancy}
        tone={pnlClass(stats.expectancy)}
        value={fmtSignedMoney(stats.expectancy)}
      />
      <StatTile
        label={account.statMaxDrawdown}
        tone={(stats.max_drawdown_pct ?? 0) > 0 ? 'text-destructive' : undefined}
        value={fmtPct(stats.max_drawdown_pct)}
      />
    </div>
  )
}

// ── Regime ───────────────────────────────────────────────────────────────────

function RegimeSection({ regime }: { regime: FinanceRegimeView | null }) {
  const { t } = useI18n()
  const market = t.finance.market

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={t.finance.research.regimeDescription}>
        {market.regimeTitle}
      </ExplainedSectionLabel>

      {!regime ? (
        <div className="py-1 text-xs text-muted-foreground">{market.regimeEmpty}</div>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            <FinanceCard className="flex items-center gap-2">
              <StatusDot tone={REGIME_TONE[regime.risk_on_off] ?? 'muted'} />
              <div className="min-w-0">
                <div className="text-[0.65rem] font-medium text-(--ui-text-tertiary)">{market.regime}</div>
                <div className="truncate text-sm font-semibold text-foreground">
                  {enumLabel(t.finance.enums.regime, regime.risk_on_off)}
                </div>
              </div>
            </FinanceCard>
            <StatTile label={market.vix} value={fmtPrice(regime.vix)} />
            <StatTile label={market.breadth} value={fmtPct(regime.breadth_pct_above_50dma)} />
          </div>

          {Object.keys(regime.indices).length > 0 && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              {Object.entries(regime.indices).map(([symbol, data]) => (
                <FinanceCard className="min-w-0" key={symbol}>
                  <div className="text-[0.65rem] font-medium text-(--ui-text-tertiary)">{symbol}</div>
                  <div className="text-sm font-semibold tabular-nums text-foreground">{fmtPrice(data.last)}</div>
                  <div
                    className={cn(
                      'text-[0.62rem] tabular-nums',
                      (data.sma50_dist_pct ?? 0) >= 0 ? 'text-primary' : 'text-destructive'
                    )}
                  >
                    {data.sma50_dist_pct === null || data.sma50_dist_pct === undefined
                      ? '—'
                      : market.vs50dma(fmtSignedPct(data.sma50_dist_pct))}
                  </div>
                </FinanceCard>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  )
}

// ── Movers ───────────────────────────────────────────────────────────────────

function MoversSection({ bottom, top }: { bottom: FinanceMover[]; top: FinanceMover[] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.moversDescription}>{copy.moversTitle}</ExplainedSectionLabel>

      {top.length === 0 && bottom.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.moversEmpty}</div>
      ) : (
        <div className="grid gap-2 sm:grid-cols-2">
          <MoversColumn movers={top} title={copy.moversTop} />
          <MoversColumn movers={bottom} title={copy.moversBottom} />
        </div>
      )}
    </section>
  )
}

function MoversColumn({ movers, title }: { movers: FinanceMover[]; title: string }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <FinanceCard className="space-y-1.5">
      <div className="text-[0.62rem] font-medium text-muted-foreground">{title}</div>
      {movers.map(mover => (
        <div className="flex items-baseline justify-between gap-2 text-xs tabular-nums" key={mover.symbol}>
          <span className="min-w-0 truncate">
            <span className="font-medium text-foreground">{mover.symbol}</span>{' '}
            {mover.display_name && <span className="text-[0.62rem] text-muted-foreground">{mover.display_name}</span>}{' '}
            <span className="text-[0.62rem] text-muted-foreground/80">{mover.theme}</span>
          </span>
          <span className="flex shrink-0 items-baseline gap-2">
            <span className="text-muted-foreground">{fmtPrice(mover.last)}</span>
            <span className={mover.dist_sma20_pct >= 0 ? 'text-primary' : 'text-destructive'}>
              {copy.vsSma20(fmtSignedPct(mover.dist_sma20_pct))}
            </span>
          </span>
        </div>
      ))}
    </FinanceCard>
  )
}

// ── Themes ───────────────────────────────────────────────────────────────────

function ThemesSection({ themes }: { themes: FinanceThemeView[] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.themesDescription}>{copy.themesTitle}</ExplainedSectionLabel>

      {themes.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.themesEmpty}</div>
      ) : (
        <div className="space-y-1">
          {themes.map(theme => (
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs" key={theme.theme}>
              <span className="font-medium text-foreground">{theme.theme}</span>
              <span className={cn('tabular-nums', theme.avg_dist_sma50_pct >= 0 ? 'text-primary' : 'text-destructive')}>
                {copy.themeMeta(theme.n_symbols, fmtSignedPct(theme.avg_dist_sma50_pct))}
              </span>
              {theme.leaders.length > 0 && (
                <span className="text-[0.62rem] text-muted-foreground">
                  {copy.themeLeaders(theme.leaders.join(', '))}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

// ── News digest ──────────────────────────────────────────────────────────────

const sentimentTone = (value: null | number): StatusTone =>
  value === null ? 'muted' : value > 0.15 ? 'good' : value < -0.15 ? 'bad' : 'muted'

function NewsSection({ news }: { news: FinanceResearchBrief['news'] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <FinanceSectionLabel>{copy.newsTitle}</FinanceSectionLabel>

      {news.items.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.newsEmpty}</div>
      ) : (
        <div className="space-y-1.5">
          {news.items.map((item, index) => (
            <NewsRow item={item} key={`${item.url || item.headline}-${index}`} />
          ))}
        </div>
      )}
    </section>
  )
}

function NewsRow({ item }: { item: FinanceNewsDigestItem }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <div className="text-xs leading-5">
      {item.url ? (
        <ExternalLink className="font-medium" href={item.url}>
          {item.headline}
        </ExternalLink>
      ) : (
        <span className="font-medium text-foreground">{item.headline}</span>
      )}
      <span className="ml-2 inline-flex flex-wrap items-baseline gap-x-2 text-[0.62rem] text-muted-foreground">
        {item.source && <span>{item.source}</span>}
        {item.age_hours !== null && item.age_hours !== undefined && (
          <span>{item.age_hours < 24 ? `${Math.round(item.age_hours)}h` : `${Math.round(item.age_hours / 24)}d`}</span>
        )}
        {item.symbol && <span className="font-medium">{item.symbol}</span>}
        {item.sentiment !== null && (
          <span className="inline-flex items-center gap-1 tabular-nums">
            <StatusDot tone={sentimentTone(item.sentiment)} />
            {copy.sentiment(`${item.sentiment > 0 ? '+' : ''}${item.sentiment.toFixed(2)}`)}
          </span>
        )}
      </span>
    </div>
  )
}

// ── Signals ──────────────────────────────────────────────────────────────────

function SignalsSection({ signals }: { signals: FinanceSignalView[] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.signalsDescription}>{copy.signalsTitle}</ExplainedSectionLabel>

      {signals.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.signalsEmpty}</div>
      ) : (
        <div className="space-y-2">
          {signals.map((signal, index) => (
            <FinanceCard className="space-y-1" key={`${signal.symbol}-${signal.source_agent}-${index}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-foreground">{signal.symbol}</span>
                {signal.display_name && (
                  <span className="text-[0.62rem] text-muted-foreground">{signal.display_name}</span>
                )}
                <FinancePill variant={signal.direction.toLowerCase().includes('long') ? 'default' : 'warn'}>
                  {enumLabel(t.finance.enums.direction, signal.direction)}
                </FinancePill>
                <FinancePill variant="outline">{signal.source_agent}</FinancePill>
                <span className="text-[0.62rem] tabular-nums text-muted-foreground">
                  {copy.signalConfidence(fmtPct(signal.confidence * 100, 0))}
                </span>
                {signal.as_of_bar && (
                  // DATA as-of (§5.10): the bar these numbers rest on, distinct
                  // from the brief's as_of — so a stale verdict can't mislead.
                  <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">
                    {copy.signalAsOfBar(signal.as_of_bar.slice(0, 10))}
                  </span>
                )}
              </div>
              {signal.thesis && <p className="text-xs leading-5 text-(--ui-text-secondary)">{signal.thesis}</p>}
            </FinanceCard>
          ))}
        </div>
      )}
    </section>
  )
}

// ── Candidates today (read-only pointer to the queue tab) ────────────────────

function CandidatesSection({
  candidates,
  onOpenQueue
}: {
  candidates: FinanceResearchBrief['candidates_today']
  onOpenQueue: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.research
  const counts = Object.entries(candidates.counts)
  const empty = counts.length === 0 && candidates.pending.length === 0

  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <FinanceSectionLabel>{copy.candidatesTitle}</FinanceSectionLabel>
        {/* Deliberate hand-off to the secondary action area — approve/edit/
            reject live ONLY in the queue tab (Loop.md §5.6). */}
        {candidates.pending.length > 0 && (
          <Button onClick={onOpenQueue} size="xs" variant="outline">
            {copy.openQueue}
          </Button>
        )}
      </div>

      {empty ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.candidatesEmpty}</div>
      ) : (
        <>
          {counts.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {counts.map(([status, count]) => (
                <FinancePill key={status} variant="outline">
                  {enumLabel(t.finance.enums.candidateStatus, status)} · {count}
                </FinancePill>
              ))}
            </div>
          )}

          {candidates.pending.map((pending, index) => (
            <PendingRow key={`${pending.symbol}-${index}`} pending={pending} />
          ))}
        </>
      )}
    </section>
  )
}

function PendingRow({ pending }: { pending: FinanceBriefPendingCandidate }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <div className="flex flex-wrap items-center gap-2 text-xs">
      <span className="font-medium text-foreground">{pending.symbol}</span>
      <span className="tabular-nums text-muted-foreground">
        {copy.pendingRow(
          enumLabel(t.finance.enums.side, pending.side),
          fmtQty(pending.qty),
          fmtPct(pending.confidence * 100, 0)
        )}
      </span>
      <FinancePill variant="muted">{enumLabel(t.finance.enums.candidateStatus, pending.status)}</FinancePill>
    </div>
  )
}

// ── Uncertainty & provenance ─────────────────────────────────────────────────

function UncertaintySection({ items }: { items: string[] }) {
  const { t } = useI18n()
  const copy = t.finance.research

  return (
    <section className="space-y-2">
      <ExplainedSectionLabel description={copy.uncertaintyDescription}>{copy.uncertaintyTitle}</ExplainedSectionLabel>
      {items.length > 0 && (
        <FinanceCard>
          <ul className="list-inside list-disc space-y-0.5 text-xs leading-5 text-(--ui-text-secondary)">
            {items.map(item => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </FinanceCard>
      )}
    </section>
  )
}

function ProvenanceFooter({ links }: { links: FinanceProvenanceLink[] }) {
  const { t } = useI18n()

  if (links.length === 0) {
    return null
  }

  return (
    <section className="space-y-2 border-t border-(--ui-stroke-tertiary) pt-3">
      <FinanceSectionLabel>{t.finance.research.provenanceTitle}</FinanceSectionLabel>
      <ul className="space-y-0.5 text-[0.68rem] leading-5">
        {links.map(link => (
          <li key={link.url}>
            <ExternalLink className="font-normal text-muted-foreground" href={link.url}>
              {link.label}
            </ExternalLink>
          </li>
        ))}
      </ul>
    </section>
  )
}

// ── Knowledge search (Loop.md §5.10: fail-closed, always cited) ──────────────

function KnowledgeSearchSection({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.research
  const [input, setInput] = useState('')
  const [submitted, setSubmitted] = useState('')

  const searchQuery = useQuery({
    enabled: enabled && submitted.length >= 2,
    queryFn: () => searchFinanceKnowledge(submitted, SEARCH_K),
    queryKey: financeKey('knowledge', submitted),
    retry: false,
    staleTime: 60_000
  })

  function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    setSubmitted(input.trim())
  }

  const hits = searchQuery.data ?? []
  const parsed = searchQuery.isError ? parseFinanceError(searchQuery.error) : null

  return (
    <section className="space-y-2 border-t border-(--ui-stroke-tertiary) pt-3">
      <FinanceSectionLabel>{copy.searchTitle}</FinanceSectionLabel>

      <form className="flex items-center gap-2" onSubmit={handleSubmit}>
        <Input onChange={event => setInput(event.target.value)} placeholder={copy.searchPlaceholder} value={input} />
        <Button disabled={!enabled || input.trim().length < 2 || searchQuery.isFetching} size="sm" type="submit">
          <Search className="size-3.5" />
          {copy.searchRun}
        </Button>
      </form>

      {submitted.length >= 2 && (
        <QuerySection
          // The vector index being down is an expected state (fail-closed 503,
          // Loop.md §5.10) — render it as a calm note, not an error.
          empty={copy.searchEmpty(submitted)}
          error={parsed && !parsed.offline ? new Error(copy.searchError) : undefined}
          isEmpty={!parsed && hits.length === 0}
          loading={searchQuery.isPending}
        >
          {parsed?.offline ? (
            <div className="py-1 text-xs text-muted-foreground">{copy.searchOffline}</div>
          ) : (
            <div className="space-y-2">
              {hits.map(hit => (
                <FinanceCard className="space-y-1" key={hit.document_id}>
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    {hit.source_url ? (
                      <ExternalLink className="text-xs font-medium" href={hit.source_url}>
                        {hit.title}
                      </ExternalLink>
                    ) : (
                      <span className="text-xs font-medium text-foreground">{hit.title}</span>
                    )}
                    <span className="text-[0.62rem] tabular-nums text-muted-foreground">
                      {hit.publisher && `${hit.publisher} · `}
                      {hit.trading_date} · {copy.searchScore(hit.score.toFixed(2))}
                    </span>
                  </div>
                  <p className="text-xs leading-5 text-(--ui-text-secondary)">{hit.snippet}</p>
                </FinanceCard>
              ))}
            </div>
          )}
        </QuerySection>
      )}
    </section>
  )
}
