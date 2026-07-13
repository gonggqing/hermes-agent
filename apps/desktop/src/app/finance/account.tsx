import { useQuery } from '@tanstack/react-query'

import { StatusDot } from '@/components/status-dot'
import {
  type FinanceAccount,
  type FinanceMode,
  type FinanceSnapshot,
  type FinanceStats,
  getFinanceAccount,
  getFinanceOrders,
  getFinanceSnapshots
} from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

import { BREAKER_TONE, enumLabel, financeKey, fmtMoney, fmtPct, fmtPrice, fmtQty, fmtSignedMoney, fmtTs, pnlClass } from './lib'
import { FinanceSectionLabel, FinanceTable, QuerySection, StatTile } from './primitives'

function accountTiles(account: FinanceAccount) {
  // Loop attached → live AccountView; loop idle → last ledger snapshot.
  const snap = account.source === 'ledger' ? account.snapshot : account

  if (!snap) {
    return null
  }

  return snap
}

export function FinanceAccountTab({ enabled, mode, query }: { enabled: boolean; mode: FinanceMode; query: string }) {
  const { t } = useI18n()
  const copy = t.finance.account

  const accountQuery = useQuery({
    enabled,
    queryFn: () => getFinanceAccount(mode),
    queryKey: financeKey('account', mode),
    refetchInterval: 30_000,
    retry: 1
  })

  const ordersQuery = useQuery({
    enabled,
    queryFn: () => getFinanceOrders({ activeOnly: true, mode }),
    queryKey: financeKey('orders', 'active', mode),
    retry: 1
  })

  const snapshotsQuery = useQuery({
    enabled,
    queryFn: () => getFinanceSnapshots({ limit: 90, mode }),
    queryKey: financeKey('snapshots', mode),
    retry: 1
  })

  const account = accountQuery.data
  const snap = account ? accountTiles(account) : null
  const stats = account?.stats
  const positions = account && account.source !== 'ledger' ? account.positions : []
  const needle = query.trim().toUpperCase()
  const visiblePositions = needle ? positions.filter(p => p.symbol.includes(needle)) : positions
  const orders = ordersQuery.data ?? []
  const visibleOrders = needle ? orders.filter(o => o.symbol.includes(needle)) : orders

  return (
    <div className="space-y-5">
      <section className="space-y-2">
        <FinanceSectionLabel>{copy.title}</FinanceSectionLabel>
        <QuerySection
          empty={copy.empty}
          error={accountQuery.isError ? accountQuery.error : undefined}
          isEmpty={!snap}
          loading={accountQuery.isPending}
        >
          {snap && (
            <>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                <StatTile label={copy.equity} value={fmtMoney(snap.equity)} />
                <StatTile label={copy.cash} value={fmtMoney(snap.cash)} />
                <StatTile label={copy.upnl} tone={pnlClass(snap.upnl)} value={fmtSignedMoney(snap.upnl)} />
                <StatTile label={copy.dayPnl} tone={pnlClass(snap.day_pnl)} value={fmtSignedMoney(snap.day_pnl)} />
                <StatTile
                  label={copy.drawdown}
                  tone={snap.drawdown_pct < 0 ? 'text-destructive' : undefined}
                  value={fmtPct(snap.drawdown_pct)}
                />
                <StatTile
                  hint={account?.source === 'ledger' ? copy.sourceLedger(fmtTs(snap.ts)) : copy.sourceLive(fmtTs(snap.ts))}
                  label={copy.breaker}
                  tone={snap.breaker_state === 'TRIPPED' ? 'text-destructive' : undefined}
                  value={enumLabel(t.finance.enums.breaker, snap.breaker_state)}
                />
              </div>
              <EquitySparkline snapshots={snapshotsQuery.data ?? []} />
            </>
          )}
        </QuerySection>
      </section>

      <section className="space-y-2">
        <FinanceSectionLabel>
          {copy.positionsTitle}
          {positions.length > 0 ? ` · ${positions.length}` : ''}
        </FinanceSectionLabel>
        <QuerySection
          empty={account?.source === 'ledger' ? copy.positionsLoopIdle : copy.positionsEmpty}
          error={undefined}
          isEmpty={visiblePositions.length === 0}
          loading={accountQuery.isPending}
        >
          <FinanceTable
            columns={[
              { label: copy.colSymbol },
              { align: 'right', label: copy.colQty },
              { align: 'right', label: copy.colAvgPx },
              { align: 'right', label: copy.colMktPx },
              { align: 'right', label: copy.colUpnl },
              { label: copy.colPool }
            ]}
            rows={visiblePositions.map(position => ({
              cells: [
                <span className="font-medium text-foreground" key="s">
                  {position.symbol}
                </span>,
                fmtQty(position.qty),
                fmtPrice(position.avg_px),
                fmtPrice(position.mkt_px),
                <span className={pnlClass(position.upnl)} key="u">
                  {fmtSignedMoney(position.upnl)}
                </span>,
                position.pool
              ],
              key: position.symbol
            }))}
          />
        </QuerySection>
      </section>

      <section className="space-y-2">
        <FinanceSectionLabel>
          {copy.ordersTitle}
          {orders.length > 0 ? ` · ${orders.length}` : ''}
        </FinanceSectionLabel>
        <QuerySection
          empty={copy.ordersEmpty}
          error={ordersQuery.isError ? ordersQuery.error : undefined}
          isEmpty={visibleOrders.length === 0}
          loading={ordersQuery.isPending}
        >
          <FinanceTable
            columns={[
              { label: copy.colSymbol },
              { label: copy.colSide },
              { align: 'right', label: copy.colQty },
              { label: copy.colType },
              { align: 'right', label: copy.colLimit },
              { align: 'right', label: copy.colStop },
              { label: copy.colTif },
              { label: copy.colStatus },
              { label: copy.colPlaced }
            ]}
            rows={visibleOrders.map(order => ({
              cells: [
                <span className="font-medium text-foreground" key="s">
                  {order.symbol}
                </span>,
                <span className={order.side === 'BUY' ? 'text-primary' : 'text-amber-600 dark:text-amber-300'} key="d">
                  {enumLabel(t.finance.enums.side, order.side)}
                </span>,
                fmtQty(order.qty),
                enumLabel(t.finance.enums.orderType, order.order_type),
                fmtPrice(order.limit),
                fmtPrice(order.stop),
                enumLabel(t.finance.enums.tif, order.tif),
                enumLabel(t.finance.enums.orderStatus, order.status),
                fmtTs(order.ts)
              ],
              key: order.id
            }))}
          />
        </QuerySection>
      </section>

      {stats && <TradeStatsSection stats={stats} />}
    </div>
  )
}

function TradeStatsSection({ stats }: { stats: FinanceStats }) {
  const { t } = useI18n()
  const copy = t.finance.account

  return (
    <section className="space-y-2">
      <FinanceSectionLabel>{copy.statsTitle}</FinanceSectionLabel>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-5">
        <StatTile label={copy.statClosedWins} value={`${stats.n_closed} / ${stats.n_wins}`} />
        <StatTile label={copy.statWinRate} value={fmtPct(stats.win_rate * 100, 0)} />
        <StatTile
          label={copy.statAvgWinLoss}
          value={`${fmtSignedMoney(stats.avg_win)} / ${fmtSignedMoney(stats.avg_loss)}`}
        />
        <StatTile
          label={copy.statPayoff}
          value={stats.payoff_ratio === null ? '—' : stats.payoff_ratio.toFixed(2)}
        />
        <StatTile label={copy.statExpectancy} tone={pnlClass(stats.expectancy)} value={fmtSignedMoney(stats.expectancy)} />
        <StatTile label={copy.statTotalPnl} tone={pnlClass(stats.total_pnl)} value={fmtSignedMoney(stats.total_pnl)} />
        <StatTile
          label={copy.statAvgHold}
          value={stats.avg_hold_days === null ? '—' : stats.avg_hold_days.toFixed(1)}
        />
        <StatTile
          label={copy.statMaxDrawdown}
          tone={stats.max_drawdown_pct > 0 ? 'text-destructive' : undefined}
          value={fmtPct(stats.max_drawdown_pct)}
        />
      </div>
    </section>
  )
}

// Tiny dependency-free equity sparkline over the ledger snapshot series.
function EquitySparkline({ snapshots }: { snapshots: FinanceSnapshot[] }) {
  const { t } = useI18n()
  const copy = t.finance.account

  if (snapshots.length < 2) {
    return null
  }

  const values = snapshots.map(snapshot => snapshot.equity)
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const width = 100
  const height = 24

  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * width
      const y = height - 2 - ((value - min) / span) * (height - 4)

      return `${x.toFixed(2)},${y.toFixed(2)}`
    })
    .join(' ')

  const rising = values[values.length - 1] >= values[0]

  return (
    <div className="flex items-center gap-3 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-2">
      <svg
        aria-label={copy.equityAlt}
        className={cn('h-8 w-40 shrink-0', rising ? 'text-primary' : 'text-destructive')}
        preserveAspectRatio="none"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        <polyline fill="none" points={points} stroke="currentColor" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="min-w-0 text-[0.65rem] leading-4 text-muted-foreground">
        <div className="flex items-center gap-1.5">
          <StatusDot tone={BREAKER_TONE[snapshots[snapshots.length - 1].breaker_state] ?? 'muted'} />
          {copy.equitySpark(snapshots.length)}
        </div>
        <div className="tabular-nums">
          {fmtMoney(min)} – {fmtMoney(max)}
        </div>
      </div>
    </div>
  )
}
