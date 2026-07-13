import { useQuery } from '@tanstack/react-query'

import { StatusDot } from '@/components/status-dot'
import { getFinanceMarket, getFinanceWatchlist } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { enumLabel, financeKey, fmtPct, fmtPrice, fmtSignedPct, fmtTs, REGIME_TONE } from './lib'
import { FinanceCard, FinancePill, FinanceSectionLabel, QuerySection, StatTile } from './primitives'

export function FinanceMarketTab({ enabled, query }: { enabled: boolean; query: string }) {
  const { t } = useI18n()
  const copy = t.finance.market

  const marketQuery = useQuery({
    enabled,
    queryFn: getFinanceMarket,
    queryKey: financeKey('market'),
    refetchInterval: 60_000,
    retry: 1
  })

  const watchlistQuery = useQuery({
    enabled,
    queryFn: getFinanceWatchlist,
    queryKey: financeKey('watchlist'),
    // The universe is static data (Loop.md §11) — no need to repoll.
    staleTime: 10 * 60_000,
    retry: 1
  })

  const market = marketQuery.data
  const hasSnapshot = Boolean(market && !market.status)
  const regime = market?.risk_on_off ?? 'neutral'
  const indices = market?.indices ?? {}

  const needle = query.trim().toLowerCase()
  const watchlist = watchlistQuery.data ?? []

  const visibleWatchlist = needle
    ? watchlist.filter(
        item =>
          item.symbol.toLowerCase().includes(needle) ||
          item.theme.toLowerCase().includes(needle) ||
          item.ai_phase.toLowerCase().includes(needle) ||
          item.role.toLowerCase().includes(needle)
      )
    : watchlist

  // Group chips by theme so the value-chain structure (§11 A–I) stays legible.
  const themes = new Map<string, typeof watchlist>()

  for (const item of visibleWatchlist) {
    const bucket = themes.get(item.theme)

    if (bucket) {
      bucket.push(item)
    } else {
      themes.set(item.theme, [item])
    }
  }

  return (
    <div className="space-y-5">
      <section className="space-y-2">
        <FinanceSectionLabel>{copy.regimeTitle}</FinanceSectionLabel>
        <QuerySection
          empty={copy.regimeEmpty}
          error={marketQuery.isError ? marketQuery.error : undefined}
          isEmpty={!hasSnapshot}
          loading={marketQuery.isPending}
        >
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <FinanceCard className="flex items-center gap-2">
              <StatusDot tone={REGIME_TONE[regime] ?? 'muted'} />
              <div className="min-w-0">
                <div className="text-[0.65rem] font-medium text-(--ui-text-tertiary)">{copy.regime}</div>
                <div className="truncate text-sm font-semibold text-foreground">
                  {enumLabel(t.finance.enums.regime, regime)}
                </div>
              </div>
            </FinanceCard>
            <StatTile label={copy.vix} value={fmtPrice(market?.vix)} />
            <StatTile label={copy.breadth} value={fmtPct(market?.breadth_pct_above_50dma)} />
            <StatTile label={copy.asOfLabel} value={fmtTs(market?.ts)} />
          </div>

          {Object.keys(indices).length > 0 && (
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
              {Object.entries(indices).map(([symbol, data]) => (
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
                      : copy.vs50dma(fmtSignedPct(data.sma50_dist_pct))}
                  </div>
                </FinanceCard>
              ))}
            </div>
          )}
        </QuerySection>
      </section>

      <section className="space-y-2">
        <FinanceSectionLabel>
          {copy.watchlistTitle}
          {watchlist.length > 0 ? ` · ${watchlist.length}` : ''} {copy.watchlistNote}
        </FinanceSectionLabel>
        <QuerySection
          empty={needle ? copy.watchlistEmptySearch : copy.watchlistEmpty}
          error={watchlistQuery.isError ? watchlistQuery.error : undefined}
          isEmpty={visibleWatchlist.length === 0}
          loading={watchlistQuery.isPending}
        >
          <div className="space-y-3">
            {[...themes.entries()].map(([theme, items]) => (
              <div key={theme}>
                <div className="mb-1 text-[0.62rem] font-medium text-muted-foreground">
                  {theme} · {items[0].ai_phase} / {items[0].role}
                </div>
                <div className="flex flex-wrap gap-1">
                  {items.map(item => (
                    <FinancePill key={item.symbol} variant={item.enabled ? 'outline' : 'muted'}>
                      {item.symbol}
                      {!item.enabled && ` ${copy.disabledTag}`}
                    </FinancePill>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </QuerySection>
      </section>
    </div>
  )
}
