import { type ReactNode, useMemo } from 'react'

import { SegmentedControl } from '@/components/ui/segmented-control'
import type { FinanceMode, FinancePosition } from '@/hermes'
import { useI18n } from '@/i18n'

import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { DetailColumn, ListColumn, MasterDetail } from '../master-detail'

import { AccountSummary, OrdersTable, TradeStatsSection, useAccountQueries } from './account'
import { FinanceDetailPlaceholder, FinanceListGroup, FinanceNavRow } from './chrome'
import { FinanceHistoryTab } from './history'
import { FinanceHoldingsView } from './holdings'
import { fmtPrice, fmtQty, fmtSignedMoney, pnlClass } from './lib'
import { FinanceMarketTab } from './market'
import { FinancePill, FinanceSectionLabel, QuerySection, StatTile } from './primitives'
import { FinanceReportsTab } from './reports'

// Portfolio master-detail (Loop.md §7 Phase 0.5): the former account/market/
// history/reports sub-tabs fold into selectable sidebar rows, with holdings as
// the primary list and per-position detail. The bottom paper/live switcher
// switches mode and refetches (paper and live ledgers stay separate, §5.8).

const OVERVIEW_IDS = ['account', 'orders', 'stats', 'market', 'history', 'reports'] as const

type OverviewId = (typeof OVERVIEW_IDS)[number]

const BOOKS = ['paper', 'real'] as const

// The Portfolio tab holds two BOOKS: the paper-trading account above (default,
// unchanged), and the user's REAL multi-account holdings (Phase 0.9). A thin
// sub-nav toggles between them; the paper/live footer only makes sense for the
// paper book, so the real book renders without it.
export function FinancePortfolioView({
  bottomBar,
  enabled,
  mode
}: {
  bottomBar: ReactNode
  enabled: boolean
  mode: FinanceMode
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [book, setBook] = useRouteEnumParam('book', BOOKS, 'paper')

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div aria-label={copy.subnavAria} className="shrink-0 px-3 py-2" role="group">
        <SegmentedControl
          onChange={setBook}
          options={[
            { id: 'paper', label: copy.subnavPaper },
            { id: 'real', label: copy.subnavReal }
          ]}
          value={book}
        />
      </div>
      <div className="min-h-0 flex-1">
        {book === 'paper' ? (
          <PaperPortfolio bottomBar={bottomBar} enabled={enabled} mode={mode} />
        ) : (
          <FinanceHoldingsView enabled={enabled} />
        )}
      </div>
    </div>
  )
}

function PaperPortfolio({
  bottomBar,
  enabled,
  mode
}: {
  bottomBar: ReactNode
  enabled: boolean
  mode: FinanceMode
}) {
  const { t } = useI18n()
  const copy = t.finance.portfolio

  const { account, accountQuery, orders, ordersQuery, positions, snap, snapshots, stats } = useAccountQueries(
    enabled,
    mode
  )

  // Overview rows are fixed; position rows are the live holdings. Overview ids
  // are lowercase words, symbols uppercase tickers — never collide.
  const selectableIds = useMemo(
    () => [...OVERVIEW_IDS, ...positions.map(position => position.symbol)],
    [positions]
  )

  const [selected, setSelected] = useRouteEnumParam('holding', selectableIds, 'account')

  const overviewLabel: Record<OverviewId, string> = {
    account: copy.account,
    orders: copy.orders,
    stats: copy.stats,
    market: copy.market,
    history: copy.history,
    reports: copy.reports
  }

  const selectedPosition = positions.find(position => position.symbol === selected) ?? null

  return (
    <MasterDetail>
      <ListColumn>
        <FinanceListGroup label={copy.groupOverview}>
          {OVERVIEW_IDS.map(id => (
            <FinanceNavRow active={selected === id} key={id} onSelect={() => setSelected(id)} title={overviewLabel[id]} />
          ))}
        </FinanceListGroup>

        <FinanceListGroup label={copy.groupPositions}>
          {positions.length === 0 ? (
            <div className="px-2 py-1 text-[0.65rem] text-muted-foreground/70">{copy.positionsEmpty}</div>
          ) : (
            positions.map(position => (
              <FinanceNavRow
                active={selected === position.symbol}
                key={position.symbol}
                meta={
                  <span className={pnlMetaClass(position.upnl)}>{fmtSignedMoney(position.upnl)}</span>
                }
                onSelect={() => setSelected(position.symbol)}
                subtitle={`${fmtQty(position.qty)} · ${position.pool}`}
                title={position.symbol}
              />
            ))
          )}
        </FinanceListGroup>
      </ListColumn>

      <DetailColumn actionBar={bottomBar}>
        {selectedPosition ? (
          <PositionDetail position={selectedPosition} />
        ) : selected === 'account' ? (
          <section className="space-y-2">
            <FinanceSectionLabel>{t.finance.account.title}</FinanceSectionLabel>
            <QuerySection
              empty={t.finance.account.empty}
              error={accountQuery.isError ? accountQuery.error : undefined}
              isEmpty={!snap}
              loading={accountQuery.isPending}
            >
              <AccountSummary account={account} snap={snap} snapshots={snapshots} />
            </QuerySection>
          </section>
        ) : selected === 'orders' ? (
          <section className="space-y-2">
            <FinanceSectionLabel>
              {t.finance.account.ordersTitle}
              {orders.length > 0 ? ` · ${orders.length}` : ''}
            </FinanceSectionLabel>
            <QuerySection
              empty={t.finance.account.ordersEmpty}
              error={ordersQuery.isError ? ordersQuery.error : undefined}
              isEmpty={orders.length === 0}
              loading={ordersQuery.isPending}
            >
              <OrdersTable orders={orders} />
            </QuerySection>
          </section>
        ) : selected === 'stats' ? (
          <QuerySection
            empty={t.finance.account.empty}
            error={accountQuery.isError ? accountQuery.error : undefined}
            isEmpty={!stats}
            loading={accountQuery.isPending}
          >
            {stats && <TradeStatsSection stats={stats} />}
          </QuerySection>
        ) : selected === 'market' ? (
          <FinanceMarketTab enabled={enabled} query="" />
        ) : selected === 'history' ? (
          <FinanceHistoryTab enabled={enabled} mode={mode} query="" />
        ) : selected === 'reports' ? (
          <FinanceReportsTab enabled={enabled} />
        ) : (
          <FinanceDetailPlaceholder>{copy.selectPrompt}</FinanceDetailPlaceholder>
        )}
      </DetailColumn>
    </MasterDetail>
  )
}

const pnlMetaClass = (value: null | number) => `text-[0.62rem] font-medium tabular-nums ${pnlClass(value)}`

function PositionDetail({ position }: { position: FinancePosition }) {
  const { t } = useI18n()
  const copy = t.finance.portfolio

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-[0.9375rem] font-semibold tracking-tight text-foreground">{position.symbol}</h3>
        <FinancePill variant="outline">{position.pool}</FinancePill>
      </div>
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        <StatTile label={copy.positionQty} value={fmtQty(position.qty)} />
        <StatTile label={copy.positionAvgPx} value={fmtPrice(position.avg_px)} />
        <StatTile label={copy.positionMktPx} value={fmtPrice(position.mkt_px)} />
        <StatTile label={copy.positionUpnl} tone={pnlClass(position.upnl)} value={fmtSignedMoney(position.upnl)} />
        <StatTile label={copy.positionPool} value={position.pool} />
      </div>
    </section>
  )
}
