import { type ReactNode, useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  type FinanceMode,
  type FinancePortfolioControls,
  type FinancePortfolioControlsUpdate,
  type FinancePosition,
  getFinancePortfolioControls,
  updateFinancePortfolioControls
} from '@/hermes'
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

const OVERVIEW_IDS = ['account', 'controls', 'orders', 'stats', 'market', 'history', 'reports'] as const

type OverviewId = (typeof OVERVIEW_IDS)[number]

// The bottom-right mode control is the account-scope switch for Portfolio.
// LIVE opens the user's real multi-account holdings by default; PAPER retains
// the full broker/order/history view and identifies the account as IBHK Paper.
export function FinancePortfolioView({
  bottomBar,
  enabled,
  mode
}: {
  bottomBar: ReactNode
  enabled: boolean
  mode: FinanceMode
}) {
  return mode === 'paper' ? (
    <PaperPortfolio bottomBar={bottomBar} enabled={enabled} mode={mode} />
  ) : (
    <FinanceHoldingsView bottomBar={bottomBar} enabled={enabled} environment="live" />
  )
}

export function PaperPortfolio({
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
  const selectableIds = useMemo(() => [...OVERVIEW_IDS, ...positions.map(position => position.symbol)], [positions])

  const [selected, setSelected] = useRouteEnumParam('holding', selectableIds, 'account')

  const overviewLabel: Record<OverviewId, string> = {
    account: t.finance.holdings.paperAccountName,
    controls: copy.controls,
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
            <FinanceNavRow
              active={selected === id}
              key={id}
              onSelect={() => setSelected(id)}
              title={overviewLabel[id]}
            />
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
                meta={<span className={pnlMetaClass(position.upnl)}>{fmtSignedMoney(position.upnl)}</span>}
                onSelect={() => setSelected(position.symbol)}
                subtitle={`${fmtQty(position.qty)} · ${position.currency} · ${position.pool}`}
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
        ) : selected === 'controls' ? (
          <PortfolioControlsPanel />
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

type NumericControlKey = Exclude<keyof FinancePortfolioControlsUpdate, 'base_currency'>

function PortfolioControlsPanel() {
  const { t } = useI18n()
  const copy = t.finance.portfolio
  const [value, setValue] = useState<FinancePortfolioControls | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let alive = true
    void getFinancePortfolioControls().then(
      next => {
        if (alive) {
          setValue(next)
        }
      },
      () => {
        if (alive) {
          setError(copy.controlsLoadError)
        }
      }
    )

    return () => {
      alive = false
    }
  }, [copy.controlsLoadError])

  const fields: Array<{
    key: NumericControlKey
    label: string
    max: number
    min: number
    step: number
    suffix: string
  }> = [
    { key: 'invested_target_pct', label: copy.investedTarget, min: 0, max: 95, step: 1, suffix: '%' },
    { key: 'invested_tolerance_pct', label: copy.investedTolerance, min: 0, max: 20, step: 1, suffix: '%' },
    { key: 'agent_budget_pct', label: copy.agentBudget, min: 0, max: 95, step: 1, suffix: '%' },
    { key: 'agent_budget_tolerance_pct', label: copy.agentTolerance, min: 0, max: 20, step: 1, suffix: '%' },
    { key: 'max_position_pct', label: copy.maxPosition, min: 0.1, max: 30, step: 0.5, suffix: '%' },
    { key: 'per_trade_risk_pct', label: copy.perTradeRisk, min: 0.1, max: 1.6, step: 0.1, suffix: '%' },
    { key: 'max_new_positions_per_day', label: copy.maxNewPositions, min: 1, max: 10, step: 1, suffix: '' }
  ]

  const change = (key: NumericControlKey, raw: string) => {
    const number = Number(raw)

    if (!Number.isFinite(number)) {
      return
    }
    setValue(current => (current ? { ...current, [key]: number } : current))
    setError(null)
    setSaved(false)
  }

  const save = async () => {
    if (!value) {
      return
    }
    setSaving(true)
    setError(null)
    setSaved(false)

    try {
      setValue(
        await updateFinancePortfolioControls({
          invested_target_pct: value.invested_target_pct,
          invested_tolerance_pct: value.invested_tolerance_pct,
          agent_budget_pct: value.agent_budget_pct,
          agent_budget_tolerance_pct: value.agent_budget_tolerance_pct,
          max_position_pct: value.max_position_pct,
          per_trade_risk_pct: value.per_trade_risk_pct,
          max_new_positions_per_day: value.max_new_positions_per_day,
          base_currency: value.base_currency
        })
      )
      setSaved(true)
    } catch {
      setError(copy.controlsSaveError)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="space-y-4">
      <div className="space-y-1">
        <FinanceSectionLabel>{copy.controlsTitle}</FinanceSectionLabel>
        <p className="text-xs leading-relaxed text-muted-foreground">{copy.controlsDescription}</p>
      </div>
      {value ? (
        <>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {fields.map(field => (
              <label className="space-y-1.5" key={field.key}>
                <span className="block text-[0.68rem] font-medium text-muted-foreground">{field.label}</span>
                <div className="relative">
                  <Input
                    aria-label={field.label}
                    className={field.suffix ? 'pr-8' : undefined}
                    max={field.max}
                    min={field.min}
                    onChange={event => change(field.key, event.target.value)}
                    step={field.step}
                    type="number"
                    value={value[field.key]}
                  />
                  {field.suffix ? (
                    <span className="pointer-events-none absolute right-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground">
                      {field.suffix}
                    </span>
                  ) : null}
                </div>
              </label>
            ))}
            <label className="space-y-1.5">
              <span className="block text-[0.68rem] font-medium text-muted-foreground">{copy.baseCurrency}</span>
              <Input disabled value={value.base_currency} />
            </label>
          </div>
          <div className="grid grid-cols-2 gap-2 border-y border-border/70 py-3">
            <StatTile
              label={copy.cashReserve}
              value={`${Math.max(0, 100 - value.invested_target_pct - value.invested_tolerance_pct).toFixed(1)}%`}
            />
            <StatTile
              label={copy.agentCeiling}
              value={`${(value.agent_budget_pct + value.agent_budget_tolerance_pct).toFixed(1)}%`}
            />
          </div>
          <div className="space-y-1 text-xs leading-relaxed text-muted-foreground">
            <p>{copy.controlsNote}</p>
            <p>{copy.currencyNote}</p>
          </div>
          {error ? <p className="text-xs text-destructive">{error}</p> : null}
          {saved ? <p className="text-xs text-primary">{copy.controlsSaved}</p> : null}
          <Button disabled={saving} onClick={() => void save()}>
            {saving ? copy.savingControls : copy.saveControls}
          </Button>
        </>
      ) : (
        <p className="text-xs text-muted-foreground">{error ?? copy.controlsLoadError}</p>
      )}
    </section>
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
        <FinancePill variant="outline">{position.currency}</FinancePill>
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
