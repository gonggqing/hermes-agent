import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useState } from 'react'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  type FinanceBriefPendingCandidate,
  type FinanceFreshness,
  type FinanceMover,
  type FinanceNewsDigestItem,
  type FinanceProvenanceLink,
  type FinanceRegimeView,
  type FinanceResearchBrief,
  type FinanceRiskView,
  type FinanceSignalView,
  type FinanceThemeView,
  getFinanceResearchBrief,
  postFinanceResearchRun,
  searchFinanceKnowledge
} from '@/hermes'
import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/external-link'
import { AlertTriangle, Bitcoin, Coin, GasStation, Info, Landmark, RefreshCw, Search } from '@/lib/icons'
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
import { FinanceCard, FinancePill, FinanceSectionLabel, QuerySection, StatTile } from './primitives'
import { WATCH_MODULE_IDS, type WatchModuleId, WatchModulePanel } from './watch'

// Investment Research — the DEFAULT Finance view (Loop.md §7 Phase 0.5):
// research and risk awareness are primary; execution stays in the secondary
// action-queue tab. Everything here is read-only: the brief carries as-of
// times, PAPER/LIVE mode, explicit freshness/staleness, citations and an
// uncertainty section, and this surface adds no authority beyond rendering it.

const BRIEF_POLL_MS = 60_000

// Knowledge search results per query (server clamps k to 1..25).
const SEARCH_K = 5

// Selectable sidebar items. The three ACTIVE markets (US, China, HK) plus the
// read-only watch modules; disabled market placeholders (UK/Korea/Japan) are
// NOT selectable so they stay out of the enum.
const ACTIVE_MARKETS = ['us', 'china', 'hk', 'korea'] as const

const RESEARCH_DESKS = [...ACTIVE_MARKETS, ...WATCH_MODULE_IDS] as const

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

// Watch modules use crisp tabler asset marks: a coin for gold, a fuel pump for
// oil, a bank/landmark for rates & bonds, and the Bitcoin mark for crypto.
const MODULE_GLYPH: Record<WatchModuleId, { color: string; icon: React.ComponentType<React.SVGProps<SVGSVGElement>> }> = {
  gold: { color: '#F59E0B', icon: Coin },
  oil: { color: '#78716C', icon: GasStation },
  rates: { color: '#0D9488', icon: Landmark },
  crypto: { color: '#F7931A', icon: Bitcoin }
}

// China & Hong Kong derive from the ONE CN brief (research-only). China shows
// mainland listings (.SS/.SZ); HK shows .HK. Regime/news/themes/freshness are
// shared — per-region briefs are a Phase 0.9 refinement.
function partitionCnBrief(brief: FinanceResearchBrief, region: 'china' | 'hk'): FinanceResearchBrief {
  const match =
    region === 'hk'
      ? (symbol: string) => symbol.endsWith('.HK')
      : (symbol: string) => symbol.endsWith('.SS') || symbol.endsWith('.SZ')

  return {
    ...brief,
    movers: {
      top: brief.movers.top.filter(mover => match(mover.symbol)),
      bottom: brief.movers.bottom.filter(mover => match(mover.symbol))
    },
    signals_today: brief.signals_today.filter(signal => match(signal.symbol))
  }
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
  const [desk, setDesk] = useRouteEnumParam('desk', RESEARCH_DESKS, 'us')
  const marketDesk = isMarketDesk(desk)
  // China & HK share ONE fetch: keyed by 'cn' (not the desk) so switching
  // between them never refetches. US sends no market param; KR fetches ?market=kr.
  const isCn = desk === 'china' || desk === 'hk'
  const isKr = desk === 'korea'
  const marketKey = desk === 'us' ? 'us' : isKr ? 'kr' : 'cn'

  const briefQuery = useQuery({
    enabled: enabled && marketDesk,
    queryFn: () => getFinanceResearchBrief(desk === 'us' ? undefined : isKr ? 'kr' : 'cn'),
    queryKey: financeKey('research', 'brief', marketKey),
    refetchInterval: BRIEF_POLL_MS,
    retry: 1
  })

  const rawBrief = briefQuery.data

  // KR is its own single-region brief (no CN-style partition); only china/hk
  // partition the shared CN brief.
  const brief =
    rawBrief && isCn ? partitionCnBrief(rawBrief, desk === 'hk' ? 'hk' : 'china') : rawBrief

  const marketLabel: Record<(typeof ACTIVE_MARKETS)[number], string> = {
    us: copy.marketUs,
    china: copy.marketChina,
    hk: copy.marketHk,
    korea: copy.marketKorea
  }

  // Manual "run research now": force the backend to RE-RUN this market's
  // research session (fresh data), then refetch. Only markets with their own
  // session (China/HK → cn, Korea → kr); US is loop-driven, watch modules are
  // cross-asset. Read-only (no orders) so it is ungated.
  const queryClient = useQueryClient()
  const canRun = marketDesk && desk !== 'us'

  const runMutation = useMutation({
    mutationFn: () => postFinanceResearchRun(isKr ? 'kr' : 'cn'),
    onError: error =>
      notifyError(error instanceof Error ? error : new Error(String(error)), copy.runResearchFailed),
    onSuccess: result => {
      void queryClient.invalidateQueries({ queryKey: financeKey('research', 'brief', marketKey) })
      notify({
        kind: 'success',
        message: result.market_label,
        title: copy.runResearchDone
      })
    }
  })

  return (
    <MasterDetail>
      <ListColumn>
        <FinanceListGroup label={copy.marketsGroup}>
          {ACTIVE_MARKETS.map(id => (
            <FinanceNavRow
              active={desk === id}
              key={id}
              leading={<FinanceRowGlyph color={MARKET_GLYPH[id].color} emoji={MARKET_GLYPH[id].emoji} />}
              onSelect={() => setDesk(id)}
              title={marketLabel[id]}
            />
          ))}
        </FinanceListGroup>

        <FinanceListGroup label={copy.watchGroup}>
          {WATCH_MODULE_IDS.map(id => (
            <FinanceNavRow
              active={desk === id}
              key={id}
              leading={<FinanceRowGlyph color={MODULE_GLYPH[id].color} icon={MODULE_GLYPH[id].icon} />}
              onSelect={() => setDesk(id)}
              title={t.finance.watch.modules[id]}
            />
          ))}
        </FinanceListGroup>
      </ListColumn>

      {/* The watch desks own the full-bleed K-chart, so drop the centered
          max-w column and let the candles fill the pane edge to edge. */}
      <DetailColumn actionBar={bottomBar} bleed={!marketDesk}>
        {marketDesk ? (
          <div className="space-y-5">
            {canRun && (
              <div className="flex justify-end">
                <Button
                  disabled={!enabled || runMutation.isPending}
                  onClick={() => runMutation.mutate()}
                  size="xs"
                  variant="outline"
                >
                  <RefreshCw className={cn('h-3.5 w-3.5', runMutation.isPending && 'animate-spin')} />
                  {runMutation.isPending ? copy.runningResearch : copy.runResearch}
                </Button>
              </div>
            )}
            {isCn && <RegionNote note={copy.regionNote} />}
            <QuerySection
              empty={copy.briefError}
              error={briefQuery.isError ? briefQuery.error : undefined}
              isEmpty={!brief}
              loading={briefQuery.isPending}
            >
              {brief && <BriefBody brief={brief} onOpenQueue={onOpenQueue} researchOnly={isCn} />}
            </QuerySection>
            <KnowledgeSearchSection enabled={enabled} />
          </div>
        ) : (
          <WatchModulePanel enabled={enabled} module={desk as WatchModuleId} />
        )}
      </DetailColumn>
    </MasterDetail>
  )
}

// Calm note above the China/HK brief explaining the shared-CN partition and its
// Phase 0.9 status.
function RegionNote({ note }: { note: string }) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-2',
        'text-[0.7rem] leading-5 text-(--ui-text-secondary)'
      )}
    >
      <Info className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0">{note}</div>
    </div>
  )
}

export function BriefBody({
  brief,
  onOpenQueue,
  researchOnly
}: {
  brief: FinanceResearchBrief
  onOpenQueue: () => void
  researchOnly: boolean
}) {
  return (
    <div className="space-y-5">
      <BriefHeader brief={brief} researchOnly={researchOnly} />
      <FreshnessBanner freshness={brief.freshness} />
      {/* Research-only markets carry no account/positions (risk===null), so the
          account-risk strip and the order-approval hand-off are hidden and a
          research-only note takes their place. */}
      {researchOnly ? <ResearchOnlyNote /> : <RiskSection risk={brief.risk} />}
      <RegimeSection regime={brief.regime} />
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

function BriefHeader({ brief, researchOnly }: { brief: FinanceResearchBrief; researchOnly: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.research
  const { freshness } = brief

  return (
    <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
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
        <FreshnessPill ageMinutes={freshness.market_age_minutes} label={copy.freshMarket} stale={freshness.market_stale} />
        <FreshnessPill ageMinutes={freshness.news_age_minutes} label={copy.freshNews} stale={freshness.news_stale} />
        <FreshnessPill
          ageMinutes={freshness.portfolio_age_minutes}
          label={copy.freshPortfolio}
          stale={freshness.portfolio_stale}
        />
      </span>
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
      <FinanceSectionLabel>{copy.riskTitle}</FinanceSectionLabel>

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
      <StatTile label={account.statExpectancy} tone={pnlClass(stats.expectancy)} value={fmtSignedMoney(stats.expectancy)} />
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
      <FinanceSectionLabel>{market.regimeTitle}</FinanceSectionLabel>

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
      <FinanceSectionLabel>{copy.moversTitle}</FinanceSectionLabel>

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
      <FinanceSectionLabel>{copy.themesTitle}</FinanceSectionLabel>

      {themes.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.themesEmpty}</div>
      ) : (
        <div className="space-y-1">
          {themes.map(theme => (
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs" key={theme.theme}>
              <span className="font-medium text-foreground">{theme.theme}</span>
              <span
                className={cn(
                  'tabular-nums',
                  theme.avg_dist_sma50_pct >= 0 ? 'text-primary' : 'text-destructive'
                )}
              >
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
      <FinanceSectionLabel>{copy.signalsTitle}</FinanceSectionLabel>

      {signals.length === 0 ? (
        <div className="py-1 text-xs text-muted-foreground">{copy.signalsEmpty}</div>
      ) : (
        <div className="space-y-2">
          {signals.map((signal, index) => (
            <FinanceCard className="space-y-1" key={`${signal.symbol}-${signal.source_agent}-${index}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-xs font-semibold text-foreground">{signal.symbol}</span>
                <FinancePill variant={signal.direction.toLowerCase().includes('long') ? 'default' : 'warn'}>
                  {enumLabel(t.finance.enums.direction, signal.direction)}
                </FinancePill>
                <FinancePill variant="outline">{signal.source_agent}</FinancePill>
                <span className="text-[0.62rem] tabular-nums text-muted-foreground">
                  {copy.signalConfidence(fmtPct(signal.confidence * 100, 0))}
                </span>
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

  if (items.length === 0) {
    return null
  }

  return (
    <section className="space-y-2">
      <FinanceSectionLabel>{t.finance.research.uncertaintyTitle}</FinanceSectionLabel>
      <FinanceCard>
        <ul className="list-inside list-disc space-y-0.5 text-xs leading-5 text-(--ui-text-secondary)">
          {items.map(item => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </FinanceCard>
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
        <Input
          onChange={event => setInput(event.target.value)}
          placeholder={copy.searchPlaceholder}
          value={input}
        />
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
