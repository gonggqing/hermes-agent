import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Tip } from '@/components/ui/tooltip'
import { type FinancePredictionMetrics, type FinanceResearchMarket, getFinancePredictionSummary } from '@/hermes'
import { useI18n } from '@/i18n'
import { BarChart3, Info } from '@/lib/icons'

import { financeKey, fmtPct, fmtSignedPct, fmtTs } from './lib'
import { FinanceCard, FinancePill, FinanceSectionLabel, FinanceTable, QuerySection, StatTile } from './primitives'

type MarketFilter = 'all' | FinanceResearchMarket

const MARKET_FILTERS: MarketFilter[] = ['all', 'us', 'hk', 'cn', 'kr']

function accuracy(value: null | number): string {
  return value === null ? '—' : fmtPct(value * 100)
}

function marketLabel(value: MarketFilter, copy: ReturnType<typeof useI18n>['t']['finance']): string {
  if (value === 'all') {
    return copy.prediction.allMarkets
  }

  return {
    us: copy.research.marketUs,
    hk: copy.research.marketHk,
    cn: copy.research.marketChina,
    kr: copy.research.marketKorea
  }[value]
}

function directionStyle(direction: string): {
  className?: string
  variant: 'destructive' | 'outline' | 'warn'
} {
  switch (direction.toLowerCase()) {
    case 'positive':

    case 'long':
      return {
        className: 'border-0 bg-emerald-500/10 text-emerald-600 dark:text-emerald-300',
        variant: 'outline'
      }

    case 'negative':

    case 'short':
      return { variant: 'destructive' }

    case 'watch':
      return { variant: 'warn' }

    default:
      return { variant: 'outline' }
  }
}

function outcomeClass(state: string): string | undefined {
  switch (state.toLowerCase()) {
    case 'confirmed':
      return 'font-medium text-emerald-600 dark:text-emerald-300'

    case 'refuted':
      return 'font-medium text-destructive'

    default:
      return undefined
  }
}

function MetricsTable({
  horizon,
  rows,
  title
}: {
  horizon?: boolean
  rows: Array<FinancePredictionMetrics & { key: string }>
  title: string
}) {
  const { t } = useI18n()
  const copy = t.finance.prediction

  return (
    <section className="space-y-2">
      <FinanceSectionLabel>{title}</FinanceSectionLabel>
      <FinanceTable
        columns={[
          { label: horizon ? copy.colHorizon : copy.colMarket },
          { align: 'right', label: copy.colSample },
          { align: 'right', label: copy.colAccuracy },
          { align: 'right', label: copy.colReturn }
        ]}
        rows={rows.map(row => ({
          cells: [
            horizon ? `${row.key}D` : row.key,
            row.directional_samples,
            accuracy(row.directional_accuracy),
            fmtSignedPct(row.mean_return_pct)
          ],
          key: row.key
        }))}
      />
    </section>
  )
}

export function PredictionReviewPanel({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.prediction
  const [market, setMarket] = useState<MarketFilter>('all')

  const query = useQuery({
    enabled,
    queryFn: () => getFinancePredictionSummary(market === 'all' ? undefined : market),
    queryKey: financeKey('research', 'predictions', market),
    refetchInterval: 60_000,
    retry: 1
  })

  const summary = query.data
  const overview = summary?.overview

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-center gap-1.5">
          <h2 className="text-base font-semibold text-foreground">{copy.title}</h2>
          <Tip label={copy.description} side="top">
            <button
              aria-label={copy.description}
              className="inline-flex size-6 cursor-help items-center justify-center rounded-sm text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              type="button"
            >
              <Info className="size-3.5" />
            </button>
          </Tip>
        </div>
        <div aria-label={copy.colMarket} className="flex flex-wrap gap-1" role="group">
          {MARKET_FILTERS.map(value => (
            <Button
              aria-pressed={market === value}
              key={value}
              onClick={() => setMarket(value)}
              size="xs"
              variant={market === value ? 'default' : 'outline'}
            >
              {marketLabel(value, t.finance)}
            </Button>
          ))}
        </div>
      </div>

      <QuerySection empty={copy.empty} error={query.error} isEmpty={!summary} loading={query.isPending}>
        {summary && overview && (
          <div className="space-y-5">
            <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
              <StatTile label={copy.statActive} value={String(overview.active_series)} />
              <StatTile label={copy.statEvaluated} value={String(overview.evaluated_checkpoints)} />
              <StatTile label={copy.statDue} value={String(overview.due_checkpoints)} />
              <StatTile
                hint={`${overview.directional_samples} ${copy.sampleHint}`}
                label={copy.statAccuracy}
                value={accuracy(overview.directional_accuracy)}
              />
            </div>

            <section className="space-y-2">
              <FinanceSectionLabel>{copy.activeForecasts}</FinanceSectionLabel>
              {summary.active_forecasts.length === 0 ? (
                <div className="text-xs text-muted-foreground">{copy.empty}</div>
              ) : (
                <div className="grid gap-2 lg:grid-cols-2">
                  {summary.active_forecasts.slice(0, 12).map(item => (
                    <FinanceCard className="space-y-2" key={item.series_id}>
                      <div className="flex items-center justify-between gap-2">
                        <div className="min-w-0">
                          <div className="truncate text-sm font-semibold">{item.display_name || item.entity_key}</div>
                          {item.display_name && item.entity_type !== 'market' && (
                            <div className="font-mono text-[0.62rem] text-muted-foreground/75">{item.entity_key}</div>
                          )}
                        </div>
                        <FinancePill {...directionStyle(item.direction)}>
                          {item.market} · {item.direction}
                        </FinancePill>
                      </div>
                      <p className="line-clamp-3 text-xs leading-5 text-muted-foreground">{item.thesis}</p>
                      <div className="flex flex-wrap gap-3 text-[0.62rem] text-muted-foreground/75">
                        <span>
                          {copy.colConfidence} {accuracy(item.confidence)}
                        </span>
                        <span>{item.horizons.map(value => `${value}D`).join(' / ')}</span>
                        <span>
                          {item.pending_checkpoints} {copy.pending}
                        </span>
                      </div>
                    </FinanceCard>
                  ))}
                </div>
              )}
            </section>

            <div className="space-y-4">
              <MetricsTable rows={summary.by_market} title={copy.byMarket} />
              <MetricsTable horizon rows={summary.by_horizon} title={copy.byHorizon} />
            </div>

            <section className="space-y-2">
              <FinanceSectionLabel>{copy.recent}</FinanceSectionLabel>
              <FinanceTable
                columns={[
                  { label: copy.colEntity },
                  { label: copy.colDirection },
                  { align: 'right', label: copy.colHorizon },
                  { align: 'right', label: copy.colReturn },
                  { align: 'right', label: copy.colPath },
                  { align: 'right', label: copy.colStatus }
                ]}
                rows={summary.recent_evaluations.map(item => ({
                  cells: [
                    <span key="entity">
                      <span className="font-medium">{item.display_name || item.entity_key}</span>{' '}
                      {item.display_name && item.entity_type !== 'market' && (
                        <span className="font-mono text-muted-foreground/75">{item.entity_key} </span>
                      )}
                      <span className="text-muted-foreground">{item.market}</span>
                    </span>,
                    item.direction,
                    `${item.horizon_sessions}D`,
                    fmtSignedPct(item.return_pct),
                    `${fmtSignedPct(item.mfe_pct)} / ${fmtSignedPct(item.mae_pct)}`,
                    <span className={outcomeClass(item.state)} key="state">
                      {item.state}
                    </span>
                  ],
                  key: item.evaluation_id
                }))}
              />
            </section>

            <div className="text-right text-[0.62rem] text-muted-foreground/70">
              {copy.generatedAt} · {fmtTs(summary.generated_at)}
            </div>
          </div>
        )}
      </QuerySection>
    </div>
  )
}

export function StrategyBacktestsPanel() {
  const { t } = useI18n()
  const copy = t.finance.prediction

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <BarChart3 className="size-4 text-muted-foreground" />
        <h2 className="text-base font-semibold text-foreground">{copy.backtests}</h2>
      </div>
      <FinanceCard>
        <p className="text-xs leading-5 text-muted-foreground">{copy.backtestEmpty}</p>
      </FinanceCard>
    </div>
  )
}
