import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { type FormEvent, type ReactNode, useEffect, useMemo, useState } from 'react'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList
} from '@/components/ui/command'
import { controlVariants } from '@/components/ui/control'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { ErrorBanner } from '@/components/ui/error-state'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import { Textarea } from '@/components/ui/textarea'
import {
  type FinanceAccountType,
  type FinanceDraftActionPayload,
  type FinanceImportPreview,
  type FinanceInstrumentMatch,
  type FinancePortfolioAccount,
  type FinancePortfolioDraft,
  type FinancePortfolioMarket,
  type FinancePriceSource,
  type FinanceValuationHolding,
  type FinanceValuationTotal,
  getPortfolioAccounts,
  getPortfolioDrafts,
  getPortfolioEvents,
  getPortfolioReconcile,
  getPortfolioValuation,
  postPortfolioAccount,
  postPortfolioAccountUpdate,
  postPortfolioDraft,
  postPortfolioDraftAction,
  postPortfolioImportCommit,
  postPortfolioImportPreview,
  refreshPortfolioMarks,
  searchInstruments,
  setPortfolioMark
} from '@/hermes'
import { useI18n } from '@/i18n'
import { FileText, Landmark, Pencil, Plus, RefreshCw, Search, Upload } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import { useDebounced } from '../hooks/use-debounced'
import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { DetailColumn, ListColumn, ListStrip, MasterDetail } from '../master-detail'

import { FinanceDetailPlaceholder, FinanceListGroup, FinanceNavRow, FinanceRowGlyph } from './chrome'
import {
  enumLabel,
  FINANCE_ACTOR,
  FINANCE_KEY,
  financeKey,
  fmtMoney,
  fmtPrice,
  fmtQty,
  fmtSignedMoney,
  fmtSignedPct,
  fmtTs,
  idempotencyKeyFor,
  randomId,
  parseFinanceError,
  pnlClass,
  settleIdempotencyKey
} from './lib'
import { FinanceCard, FinancePill, FinanceSectionLabel, FinanceTable, QuerySection, StatTile } from './primitives'

// Real multi-account holdings (Loop.md Phase 0.9): the user's REAL US/HK/CN
// brokerage accounts, tracked separately from the paper book. READ / DRAFT
// surface only — the single write is confirming a draft you review. Nothing
// here places an order. Mirrors the queue's master-detail + EditCandidateDialog
// house style: grouped sidebar, per-account detail with inner tabs, dialogs.

const SPECIAL_IDS = ['aggregate', 'drafts'] as const

const NO_ACCOUNTS: FinancePortfolioAccount[] = []

// Sidebar row glyph per market (flag chip, mirrors the research desks).
const MARKET_GLYPH: Record<FinancePortfolioMarket, { color: string; emoji: string }> = {
  CN: { color: '#ef4444', emoji: '🇨🇳' },
  HK: { color: '#ec4899', emoji: '🇭🇰' },
  US: { color: '#3b82f6', emoji: '🇺🇸' }
}

const DRAFT_STATUS_TONE: Record<string, StatusTone> = {
  confirmed: 'good',
  draft: 'warn',
  expired: 'muted',
  rejected: 'bad'
}

// Event types offered in the manual "record trade" form. The wire vocab is
// open-ended, so the backend may accept more — these are the common ones a
// human records by hand; enumLabel humanizes anything else that comes back.
const TRADE_EVENT_TYPES = ['buy', 'sell', 'dividend', 'deposit', 'withdrawal', 'fee'] as const

const MARKET_OPTIONS: FinancePortfolioMarket[] = ['US', 'HK', 'CN']
const ACCOUNT_TYPE_OPTIONS: FinanceAccountType[] = ['cash', 'margin']
const PROVIDER_OPTIONS = ['manual', 'ibkr'] as const
const CURRENCY_OPTIONS = ['USD', 'HKD', 'CNY', 'EUR', 'GBP', 'JPY'] as const

// Sensible default base currency for a freshly picked market.
const MARKET_CURRENCY: Record<FinancePortfolioMarket, string> = { CN: 'CNY', HK: 'HKD', US: 'USD' }

// Shared drafts query (status=draft) so the sidebar badge and the review list
// never disagree and fetch once.
function usePortfolioDrafts(enabled: boolean) {
  return useQuery({
    enabled,
    queryFn: () => getPortfolioDrafts({ status: 'draft' }),
    queryKey: financeKey('portfolio', 'drafts', 'draft'),
    retry: 1
  })
}

export function FinanceHoldingsView({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const queryClient = useQueryClient()

  const accountsQuery = useQuery({
    enabled,
    queryFn: getPortfolioAccounts,
    queryKey: financeKey('portfolio', 'accounts'),
    retry: 1
  })

  const accounts = accountsQuery.data ?? NO_ACCOUNTS

  const draftsQuery = usePortfolioDrafts(enabled)
  const draftCount = draftsQuery.data?.length

  const selectableIds = useMemo(() => [...SPECIAL_IDS, ...accounts.map(account => account.id)], [accounts])
  const [selected, setSelected] = useRouteEnumParam('acct', selectableIds, 'aggregate')
  const selectedAccount = accounts.find(account => account.id === selected) ?? null

  const [addOpen, setAddOpen] = useState(false)
  const [editing, setEditing] = useState<FinancePortfolioAccount | null>(null)
  const [trading, setTrading] = useState<FinancePortfolioAccount | null>(null)
  const [importing, setImporting] = useState<FinancePortfolioAccount | null>(null)

  const invalidate = () => void queryClient.invalidateQueries({ queryKey: FINANCE_KEY })

  return (
    <>
      <MasterDetail>
        <ListColumn
          header={
            <ListStrip
              right={
                <Button onClick={() => setAddOpen(true)} size="xs" variant="ghost">
                  <Plus className="size-3" />
                  {copy.addAccount}
                </Button>
              }
            />
          }
        >
          <FinanceListGroup label={copy.groupPortfolio}>
            <FinanceNavRow
              active={selected === 'aggregate'}
              leading={<FinanceRowGlyph icon={Landmark} muted />}
              onSelect={() => setSelected('aggregate')}
              title={copy.aggregate}
            />
            <FinanceNavRow
              active={selected === 'drafts'}
              leading={<FinanceRowGlyph icon={FileText} muted />}
              meta={
                draftCount ? (
                  <span className="rounded bg-(--ui-bg-quinary) px-1 py-px text-[0.6rem] tabular-nums text-(--ui-text-tertiary)">
                    {draftCount}
                  </span>
                ) : undefined
              }
              onSelect={() => setSelected('drafts')}
              title={copy.drafts}
            />
          </FinanceListGroup>

          <FinanceListGroup label={copy.groupAccounts}>
            <QuerySection
              empty={copy.accountsEmpty}
              error={accountsQuery.isError ? accountsQuery.error : undefined}
              isEmpty={accounts.length === 0}
              loading={accountsQuery.isPending}
            >
              {accounts.map(account => (
                <FinanceNavRow
                  active={selected === account.id}
                  key={account.id}
                  leading={
                    <FinanceRowGlyph
                      color={MARKET_GLYPH[account.market_scope]?.color}
                      emoji={MARKET_GLYPH[account.market_scope]?.emoji}
                    />
                  }
                  meta={
                    <span className="text-[0.58rem] uppercase tracking-wide text-muted-foreground/70">
                      {account.base_currency}
                    </span>
                  }
                  onSelect={() => setSelected(account.id)}
                  subtitle={copy.accountMeta(
                    enumLabel(t.finance.enums.market, account.market_scope),
                    enumLabel(t.finance.enums.accountType, account.account_type)
                  )}
                  title={account.name}
                />
              ))}
            </QuerySection>
          </FinanceListGroup>
        </ListColumn>

        <DetailColumn>
          {selected === 'aggregate' ? (
            <AggregateSection enabled={enabled} />
          ) : selected === 'drafts' ? (
            <DraftsSection enabled={enabled} />
          ) : selectedAccount ? (
            <AccountDetail
              account={selectedAccount}
              enabled={enabled}
              key={selectedAccount.id}
              onEdit={() => setEditing(selectedAccount)}
              onImport={() => setImporting(selectedAccount)}
              onRecordTrade={() => setTrading(selectedAccount)}
            />
          ) : (
            <FinanceDetailPlaceholder>{accountsQuery.isPending ? '' : copy.selectPrompt}</FinanceDetailPlaceholder>
          )}
        </DetailColumn>
      </MasterDetail>

      <AddAccountDialog onClose={() => setAddOpen(false)} onDone={invalidate} open={addOpen} />
      <EditAccountDialog account={editing} onClose={() => setEditing(null)} onDone={invalidate} />
      <RecordTradeDialog account={trading} onClose={() => setTrading(null)} onDone={invalidate} />
      <ImportDialog account={importing} onClose={() => setImporting(null)} onDone={invalidate} />
    </>
  )
}

// ── Valuation tables + summary (shared by the account and aggregate views) ───
// The valuation endpoints layer live/imported/manual price marks over the
// derived holdings so we can show 现价/市值/盈亏. When the price OR the cost is
// unknown the money fields are null and we render a muted 未知, NEVER a 0.

// price_source → tag tone. A manual override is highlighted (warn), a live feed
// reads as an accent, an imported CSV is muted, and "none" (no price) is a quiet
// outline.
const PRICE_SOURCE_VARIANT: Record<FinancePriceSource, 'default' | 'muted' | 'outline' | 'warn'> = {
  csv: 'muted',
  live: 'default',
  manual: 'warn',
  none: 'outline'
}

function PriceSourceTag({ source }: { source: FinancePriceSource }) {
  const { t } = useI18n()

  return (
    <FinancePill variant={PRICE_SOURCE_VARIANT[source]}>{enumLabel(t.finance.enums.priceSource, source)}</FinancePill>
  )
}

// A muted "未知" for a null money/price field — the shared "never render 0" cell.
function UnknownCell() {
  const { t } = useI18n()

  return <span className="italic text-muted-foreground/70">{t.finance.holdings.unknownValue}</span>
}

// POST /portfolio/marks/refresh, then reload the valuation. Bare fund codes have
// no live feed and come back in `skipped` — the toast surfaces the counts.
function RefreshPricesButton() {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const queryClient = useQueryClient()

  const mutation = useMutation({
    mutationFn: refreshPortfolioMarks,
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.refreshFailed),
    onSuccess: result => {
      notify({
        kind: 'success',
        message: copy.refreshDone(result.refreshed.length, result.skipped.length, result.failed.length),
        title: copy.refreshTitle
      })
      void queryClient.invalidateQueries({ queryKey: FINANCE_KEY })
    }
  })

  return (
    <Button disabled={mutation.isPending} onClick={() => mutation.mutate()} size="xs" variant="outline">
      <RefreshCw className="size-3" />
      {mutation.isPending ? copy.refreshing : copy.refreshPrices}
    </Button>
  )
}

function ValuationHoldingsTable({
  holdings,
  onEditMark,
  showAccounts
}: {
  holdings: FinanceValuationHolding[]
  onEditMark: (holding: FinanceValuationHolding) => void
  showAccounts?: boolean
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings

  const columns = [
    { label: copy.colSymbol },
    { label: copy.colMarket },
    { align: 'right' as const, label: copy.colQty },
    { align: 'right' as const, label: copy.colAvgCost },
    { align: 'right' as const, label: copy.colCurrentPrice },
    { align: 'right' as const, label: copy.colMarketValue },
    { align: 'right' as const, label: copy.colPnl },
    { align: 'right' as const, label: copy.colPnlPct },
    { label: copy.colCurrency },
    ...(showAccounts ? [{ align: 'right' as const, label: copy.colAccounts }] : []),
    { align: 'right' as const, label: copy.colActions }
  ]

  return (
    <FinanceTable
      columns={columns}
      rows={holdings.map(holding => ({
        cells: [
          // Instrument NAME as the primary label with the bare CODE beneath it in
          // a muted mono style; when the name is unknown we show just the code.
          <div className="min-w-0 max-w-[15rem]" key="s">
            {holding.display_name ? (
              <>
                <span className="block truncate font-medium text-foreground" title={holding.display_name}>
                  {holding.display_name}
                </span>
                <span className="block font-mono text-[0.62rem] text-muted-foreground/70">{holding.symbol}</span>
              </>
            ) : (
              <span className="font-medium text-foreground">{holding.symbol}</span>
            )}
          </div>,
          holding.market ? enumLabel(t.finance.enums.market, holding.market) : <UnknownCell key="m" />,
          fmtQty(holding.qty),
          // Never fabricate a cost when the basis is unknown — show a muted word.
          holding.cost_basis_known && holding.avg_cost !== null ? (
            fmtPrice(holding.avg_cost)
          ) : (
            <span className="italic text-muted-foreground/70" key="c">
              {copy.unknownCost}
            </span>
          ),
          // 现价 + a small tag for where the price came from.
          <span className="inline-flex items-center justify-end gap-1.5" key="px">
            {holding.price === null ? <UnknownCell /> : fmtPrice(holding.price)}
            <PriceSourceTag source={holding.price_source} />
          </span>,
          holding.market_value === null ? <UnknownCell key="mv" /> : fmtMoney(holding.market_value),
          holding.unrealized_pnl === null ? (
            <UnknownCell key="pnl" />
          ) : (
            <span className={pnlClass(holding.unrealized_pnl)} key="pnl">
              {fmtSignedMoney(holding.unrealized_pnl)}
            </span>
          ),
          holding.pnl_pct === null ? (
            <UnknownCell key="pct" />
          ) : (
            <span className={pnlClass(holding.pnl_pct)} key="pct">
              {fmtSignedPct(holding.pnl_pct)}
            </span>
          ),
          holding.currency,
          ...(showAccounts ? [copy.heldInAccounts(holding.account_names.length)] : []),
          <Button key="act" onClick={() => onEditMark(holding)} size="xs" variant="ghost">
            <Pencil className="size-3" />
            {copy.updateMark}
          </Button>
        ],
        key: `${holding.symbol}-${holding.market ?? 'na'}`
      }))}
    />
  )
}

// Per-currency totals: 总市值 (incl. cash), 现金, 总成本, 总盈亏 (+%), plus how many
// holdings are priced vs unpriced so the user knows some 场外基金 may be unpriced.
function ValuationSummary({ totals }: { totals: FinanceValuationTotal[] }) {
  const { t } = useI18n()
  const copy = t.finance.holdings

  if (totals.length === 0) {
    return null
  }

  return (
    <div className="space-y-3">
      {totals.map(total => (
        <div className="space-y-1.5" key={total.currency}>
          {totals.length > 1 && <FinanceSectionLabel>{total.currency}</FinanceSectionLabel>}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <StatTile hint={total.currency} label={copy.totalMarketValue} value={fmtMoney(total.market_value)} />
            <StatTile hint={total.currency} label={copy.cashTitle} value={fmtMoney(total.cash)} />
            <StatTile hint={total.currency} label={copy.totalCost} value={fmtMoney(total.cost)} />
            <StatTile
              hint={fmtSignedPct(total.pnl_pct)}
              label={copy.totalPnl}
              tone={pnlClass(total.unrealized_pnl)}
              value={fmtSignedMoney(total.unrealized_pnl)}
            />
          </div>
          <div className="text-[0.62rem] text-muted-foreground/70">
            {copy.pricedUnpriced(total.n_priced, total.n_unpriced)}
          </div>
        </div>
      ))}
    </div>
  )
}

// Manual price mark — set/override 现价 for one holding (esp. a 场外基金 whose NAV
// has no live feed). Mirrors the EditDraftDialog form house style; the service
// records the "manual" source and the valuation is reloaded on success.
function EditMarkDialog({
  holding,
  onClose
}: {
  holding: FinanceValuationHolding | null
  onClose: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const queryClient = useQueryClient()
  const open = holding !== null
  const [price, setPrice] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (holding) {
      setPrice(holding.price?.toString() ?? '')
      setError('')
    }
  }, [holding])

  const mutation = useMutation({
    mutationFn: () => {
      if (!holding) {
        throw new Error('no holding')
      }

      return setPortfolioMark({
        actor: FINANCE_ACTOR,
        currency: holding.currency,
        price: Number(price),
        symbol: holding.symbol
      })
    },
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.markFailed),
    onSuccess: result => {
      notify({ kind: 'success', message: '', title: copy.markSaved(result.symbol) })
      void queryClient.invalidateQueries({ queryKey: FINANCE_KEY })
      onClose()
    }
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const value = Number(price.trim())

    if (!price.trim() || !Number.isFinite(value) || value <= 0) {
      setError(copy.positiveNumber(copy.colCurrentPrice))

      return
    }

    setError('')
    mutation.mutate()
  }

  return (
    <Dialog onOpenChange={value => !value && !mutation.isPending && onClose()} open={open}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{copy.updateMarkTitle(holding?.symbol ?? '')}</DialogTitle>
          <DialogDescription>{copy.updateMarkDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <Field htmlFor="mark-price" label={copy.colCurrentPrice}>
            <Input
              id="mark-price"
              inputMode="decimal"
              onChange={event => setPrice(event.target.value)}
              placeholder="—"
              value={price}
            />
            {holding ? (
              <span className="text-[0.62rem] text-muted-foreground/70">{copy.markCurrencyNote(holding.currency)}</span>
            ) : null}
          </Field>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={mutation.isPending} type="submit">
              {mutation.isPending ? t.common.saving : copy.updateMark}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── Aggregate roll-up (all accounts) ─────────────────────────────────────────

function AggregateSection({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [riskOnly, setRiskOnly] = useState(false)
  const [editingMark, setEditingMark] = useState<FinanceValuationHolding | null>(null)

  const valuationQuery = useQuery({
    enabled,
    queryFn: () => getPortfolioValuation({ includeInRiskOnly: riskOnly }),
    queryKey: financeKey('portfolio', 'valuation', 'aggregate', riskOnly ? 'risk' : 'all'),
    retry: 1
  })

  const data = valuationQuery.data
  // Bug fix: render account NAMES, never the raw UUIDs the roll-up carries.
  const accountNames = data?.accounts?.map(account => account.name) ?? []

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <FinanceSectionLabel>{copy.aggregateTitle}</FinanceSectionLabel>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-[0.65rem] text-(--ui-text-secondary)">
            <Switch checked={riskOnly} onCheckedChange={setRiskOnly} size="xs" />
            {copy.riskOnly}
          </label>
          <RefreshPricesButton />
        </div>
      </div>

      {data && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[0.62rem] text-muted-foreground/70">
          <span>{copy.accountsCount(data.accounts?.length ?? 0)}</span>
          {accountNames.length > 0 && <span className="text-(--ui-text-secondary)">{accountNames.join(' · ')}</span>}
          <span>{copy.asOf(fmtTs(data.as_of))}</span>
        </div>
      )}

      {data && data.totals.length > 0 && <ValuationSummary totals={data.totals} />}

      <QuerySection
        empty={copy.aggregateEmpty}
        error={valuationQuery.isError ? valuationQuery.error : undefined}
        isEmpty={!data || data.holdings.length === 0}
        loading={valuationQuery.isPending}
      >
        {data && <ValuationHoldingsTable holdings={data.holdings} onEditMark={setEditingMark} showAccounts />}
      </QuerySection>

      <EditMarkDialog holding={editingMark} onClose={() => setEditingMark(null)} />
    </section>
  )
}

// ── Per-account detail (Holdings / Activity / Reconcile) ─────────────────────

type AccountTab = 'activity' | 'holdings' | 'reconcile'

function AccountDetail({
  account,
  enabled,
  onEdit,
  onImport,
  onRecordTrade
}: {
  account: FinancePortfolioAccount
  enabled: boolean
  onEdit: () => void
  onImport: () => void
  onRecordTrade: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [tab, setTab] = useState<AccountTab>('holdings')

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[0.9375rem] font-semibold tracking-tight text-foreground">{account.name}</h3>
            <FinancePill variant="outline">{enumLabel(t.finance.enums.market, account.market_scope)}</FinancePill>
            <FinancePill variant="muted">{enumLabel(t.finance.enums.provider, account.provider)}</FinancePill>
            <FinancePill variant="muted">{enumLabel(t.finance.enums.accountType, account.account_type)}</FinancePill>
            {account.include_in_risk && (
              <FinancePill variant="default">{copy.fieldIncludeInRisk}</FinancePill>
            )}
          </div>
          {account.note ? <p className="text-[0.7rem] text-muted-foreground/80">{account.note}</p> : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-1.5">
          <Button onClick={onRecordTrade} size="xs">
            <Plus className="size-3" />
            {copy.recordTrade}
          </Button>
          <Button onClick={onImport} size="xs" variant="outline">
            <Upload className="size-3" />
            {copy.importCsv}
          </Button>
          <Button aria-label={copy.editAccount} onClick={onEdit} size="xs" variant="ghost">
            <Pencil className="size-3" />
            {copy.editAccount}
          </Button>
        </div>
      </div>

      <div aria-label={copy.tabsAria} role="group">
        <SegmentedControl
          onChange={setTab}
          options={[
            { id: 'holdings', label: copy.tabHoldings },
            { id: 'activity', label: copy.tabActivity },
            { id: 'reconcile', label: copy.tabReconcile }
          ]}
          value={tab}
        />
      </div>

      {tab === 'holdings' ? (
        <AccountHoldingsTab account={account} enabled={enabled} />
      ) : tab === 'activity' ? (
        <AccountActivityTab account={account} enabled={enabled} />
      ) : (
        <AccountReconcileTab account={account} enabled={enabled} />
      )}
    </section>
  )
}

function AccountHoldingsTab({ account, enabled }: { account: FinancePortfolioAccount; enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [editingMark, setEditingMark] = useState<FinanceValuationHolding | null>(null)

  const valuationQuery = useQuery({
    enabled,
    queryFn: () => getPortfolioValuation({ accountId: account.id }),
    queryKey: financeKey('portfolio', 'valuation', account.id),
    retry: 1
  })

  const data = valuationQuery.data

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {data ? (
          <span className="text-[0.62rem] text-muted-foreground/70">{copy.asOf(fmtTs(data.as_of))}</span>
        ) : (
          <span />
        )}
        <RefreshPricesButton />
      </div>

      {data && data.totals.length > 0 && <ValuationSummary totals={data.totals} />}

      <QuerySection
        empty={copy.holdingsEmpty}
        error={valuationQuery.isError ? valuationQuery.error : undefined}
        isEmpty={!data || data.holdings.length === 0}
        loading={valuationQuery.isPending}
      >
        {data && <ValuationHoldingsTable holdings={data.holdings} onEditMark={setEditingMark} />}
      </QuerySection>

      <EditMarkDialog holding={editingMark} onClose={() => setEditingMark(null)} />
    </div>
  )
}

function AccountActivityTab({ account, enabled }: { account: FinancePortfolioAccount; enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings

  const eventsQuery = useQuery({
    enabled,
    queryFn: () => getPortfolioEvents(account.id),
    queryKey: financeKey('portfolio', 'events', account.id),
    retry: 1
  })

  const events = eventsQuery.data ?? []

  return (
    <QuerySection
      empty={copy.activityEmpty}
      error={eventsQuery.isError ? eventsQuery.error : undefined}
      isEmpty={events.length === 0}
      loading={eventsQuery.isPending}
    >
      <FinanceTable
        columns={[
          { label: copy.colDate },
          { label: copy.colEvent },
          { label: copy.colSymbol },
          { align: 'right', label: copy.colQty },
          { align: 'right', label: copy.colPrice },
          { align: 'right', label: copy.colAmount },
          { label: copy.colSource }
        ]}
        rows={events.map((event, index) => ({
          cells: [
            fmtTs(event.occurred_at),
            enumLabel(t.finance.enums.eventType, event.event_type),
            <span className="font-medium text-foreground" key="s">
              {event.symbol ?? '—'}
            </span>,
            fmtQty(event.qty),
            fmtPrice(event.price),
            fmtMoney(event.amount),
            <span className="text-muted-foreground" key="src">
              {event.source}
            </span>
          ],
          key: event.external_id ?? `${event.occurred_at}-${event.event_type}-${index}`
        }))}
      />
    </QuerySection>
  )
}

function AccountReconcileTab({ account, enabled }: { account: FinancePortfolioAccount; enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings

  const reconcileQuery = useQuery({
    enabled,
    queryFn: () => getPortfolioReconcile(account.id),
    queryKey: financeKey('portfolio', 'reconcile', account.id),
    retry: 1
  })

  const data = reconcileQuery.data

  return (
    <QuerySection
      empty={copy.reconcileEmpty}
      error={reconcileQuery.isError ? reconcileQuery.error : undefined}
      isEmpty={!data}
      loading={reconcileQuery.isPending}
    >
      {data && (
        <div className="space-y-3">
          <FinanceCard className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex items-center gap-1.5 text-xs font-medium text-foreground">
                <StatusDot tone={data.ok ? 'good' : 'warn'} />
                {data.ok ? copy.reconcileOk : copy.reconcileDrift}
              </span>
              <FinancePill variant="outline">
                {copy.reconcileAuthority(enumLabel(t.finance.enums.authority, data.authority))}
              </FinancePill>
              <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">{copy.asOf(fmtTs(data.as_of))}</span>
            </div>
            {data.summary ? <p className="text-xs leading-5 text-(--ui-text-secondary)">{data.summary}</p> : null}
            {data.note ? <p className="text-[0.65rem] text-muted-foreground/80">{data.note}</p> : null}
          </FinanceCard>

          {data.drifts.length === 0 ? (
            <div className="py-1 text-xs text-muted-foreground">{copy.noDrift}</div>
          ) : (
            <section className="space-y-2">
              <FinanceSectionLabel>{copy.driftTitle}</FinanceSectionLabel>
              <FinanceTable
                columns={[
                  { label: copy.colSymbol },
                  { align: 'right', label: copy.colPortfolioQty },
                  { align: 'right', label: copy.colBrokerQty }
                ]}
                rows={data.drifts.map(drift => ({
                  cells: [
                    <span className="font-medium text-foreground" key="s">
                      {drift.symbol}
                    </span>,
                    fmtQty(drift.portfolio_qty),
                    fmtQty(drift.broker_qty)
                  ],
                  key: drift.symbol
                }))}
              />
            </section>
          )}
        </div>
      )}
    </QuerySection>
  )
}

// ── Drafts review (the human-confirmation surface) ───────────────────────────

interface DraftActionVars {
  action: FinanceDraftActionPayload['action']
  draft: FinancePortfolioDraft
  edits?: Record<string, boolean | number | string | null>
}

function DraftsSection({ enabled }: { enabled: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const queryClient = useQueryClient()
  const draftsQuery = usePortfolioDrafts(enabled)
  const [editing, setEditing] = useState<FinancePortfolioDraft | null>(null)

  const actionLabels: Record<FinanceDraftActionPayload['action'], string> = {
    confirm: copy.confirm,
    edit: copy.edit,
    reject: copy.reject
  }

  const actionMutation = useMutation({
    mutationFn: ({ action, draft, edits }: DraftActionVars) =>
      postPortfolioDraftAction(draft.id, {
        action,
        actor: FINANCE_ACTOR,
        expected_version: draft.version,
        idempotency_key: idempotencyKeyFor(draft.id, action, edits),
        ...(edits ? { edits } : {})
      }),
    onError: (error, { action, draft, edits }) => {
      const parsed = parseFinanceError(error)

      // Terminal 4xx verdicts (not_human, incomplete, terminal, version
      // conflict, unknown) can never succeed for this exact intent — retire the
      // key. Network failures keep it so a retry replays.
      if (parsed.status !== null && parsed.status >= 400 && parsed.status < 500) {
        settleIdempotencyKey(draft.id, action, edits)
      }

      notifyError(new Error(parsed.message), copy.draftActionFailed(actionLabels[action], draft.symbol ?? '—'))
    },
    onSettled: () => void queryClient.invalidateQueries({ queryKey: FINANCE_KEY }),
    onSuccess: (result, { action, draft, edits }) => {
      settleIdempotencyKey(draft.id, action, edits)
      setEditing(null)
      notify({
        kind: 'success',
        message: result.message || `${draft.symbol ?? '—'} ${result.code}`,
        title: copy.draftActionDone(draft.symbol ?? '—', actionLabels[action], result.code)
      })
    }
  })

  const drafts = draftsQuery.data ?? []

  return (
    <section className="space-y-3">
      <FinanceSectionLabel>{copy.draftsTitle}</FinanceSectionLabel>
      <QuerySection
        empty={copy.draftsEmpty}
        error={draftsQuery.isError ? draftsQuery.error : undefined}
        isEmpty={drafts.length === 0}
        loading={draftsQuery.isPending}
      >
        <div className="space-y-2">
          {drafts.map(draft => (
            <DraftCard
              busy={actionMutation.isPending}
              draft={draft}
              key={draft.id}
              onConfirm={() => actionMutation.mutate({ action: 'confirm', draft })}
              onEdit={() => setEditing(draft)}
              onReject={() => actionMutation.mutate({ action: 'reject', draft })}
            />
          ))}
        </div>
      </QuerySection>

      <EditDraftDialog
        draft={editing}
        onClose={() => setEditing(null)}
        onSubmit={edits => {
          if (editing) {
            actionMutation.mutate({ action: 'confirm', draft: editing, edits })
          }
        }}
        submitting={actionMutation.isPending}
      />
    </section>
  )
}

function DraftCard({
  busy,
  draft,
  onConfirm,
  onEdit,
  onReject
}: {
  busy: boolean
  draft: FinancePortfolioDraft
  onConfirm: () => void
  onEdit: () => void
  onReject: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings

  return (
    <FinanceCard className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-2">
          <span className="text-sm font-semibold tracking-tight text-foreground">{draft.symbol ?? '—'}</span>
          <span className="inline-flex items-center gap-1 text-[0.65rem] text-muted-foreground">
            <StatusDot tone={DRAFT_STATUS_TONE[draft.status] ?? 'muted'} />
            {enumLabel(t.finance.enums.draftStatus, draft.status)}
          </span>
          <FinancePill variant="outline">{enumLabel(t.finance.enums.eventType, draft.event_type)}</FinancePill>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          <Button disabled={busy} onClick={onConfirm} size="xs">
            {copy.confirm}
          </Button>
          <Button disabled={busy} onClick={onEdit} size="xs" variant="outline">
            {copy.edit}
          </Button>
          <Button disabled={busy} onClick={onReject} size="xs" variant="destructive">
            {copy.reject}
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs tabular-nums text-(--ui-text-secondary)">
        <span>
          {copy.colQty} <span className="font-medium text-foreground">{fmtQty(draft.qty)}</span>
        </span>
        <span>
          {copy.colPrice}{' '}
          <span className="font-medium text-foreground">
            {draft.price === null ? copy.unknownCost : fmtPrice(draft.price)}
          </span>
        </span>
        {draft.occurred_at ? <span>{fmtTs(draft.occurred_at)}</span> : null}
      </div>

      {draft.original_text ? (
        <p className="text-[0.7rem] italic leading-4 text-muted-foreground/80">{copy.draftOriginal(draft.original_text)}</p>
      ) : null}

      {draft.missing.length > 0 && (
        <p className="text-[0.65rem] leading-4 text-amber-600 dark:text-amber-300">{copy.draftMissing(draft.missing.join(', '))}</p>
      )}
      {draft.ambiguities.length > 0 && (
        <p className="text-[0.65rem] leading-4 text-amber-600 dark:text-amber-300">
          {copy.draftAmbiguities(draft.ambiguities.join(', '))}
        </p>
      )}

      <div className="text-[0.62rem] text-muted-foreground/70">{copy.draftCreatedBy(draft.created_by ?? '—')}</div>
    </FinanceCard>
  )
}

// ── Instrument type-ahead (Command + Popover over /instruments/search) ───────

function InstrumentCombobox({
  enabled,
  market,
  onSelect,
  value
}: {
  enabled: boolean
  market?: string
  onSelect: (match: FinanceInstrumentMatch) => void
  value: FinanceInstrumentMatch | null
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const debounced = useDebounced(query.trim(), 250)

  const searchQuery = useQuery({
    enabled: enabled && open && debounced.length >= 2,
    queryFn: () => searchInstruments({ limit: 12, market, q: debounced }),
    queryKey: financeKey('instruments', market ?? 'any', debounced),
    retry: false,
    staleTime: 60_000
  })

  const result = searchQuery.data
  const matches = result?.matches ?? []

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <button
          className={cn(controlVariants({}), 'flex items-center justify-between gap-2 text-left')}
          type="button"
        >
          <span className={cn('truncate', value ? 'text-foreground' : 'text-muted-foreground')}>
            {value ? `${value.canonical_symbol} · ${value.display_name}` : copy.instrumentPlaceholder}
          </span>
          <Search className="size-3.5 shrink-0 opacity-60" />
        </button>
      </PopoverTrigger>
      {/* z-[200] lifts the resolver above the dialog (z-[130]) it opens inside. */}
      <PopoverContent align="start" className="z-[200] w-(--radix-popover-trigger-width) p-0">
        <Command shouldFilter={false}>
          <CommandInput
            onValueChange={setQuery}
            placeholder={copy.instrumentSearchPlaceholder}
            right={searchQuery.isFetching ? <Codicon name="loading" size="0.85rem" spinning /> : undefined}
            value={query}
          />
          <CommandList>
            {debounced.length < 2 ? (
              <div className="py-6 text-center text-xs text-muted-foreground">{copy.instrumentMinChars}</div>
            ) : searchQuery.isFetching && matches.length === 0 ? (
              <div className="py-6 text-center text-xs text-muted-foreground">{copy.instrumentSearching}</div>
            ) : matches.length === 0 ? (
              <CommandEmpty>{copy.instrumentEmpty(debounced)}</CommandEmpty>
            ) : (
              <>
                {result?.degraded && (
                  <div className="px-2 py-1 text-[0.62rem] text-amber-600 dark:text-amber-300">
                    {copy.instrumentDegraded}
                  </div>
                )}
                <CommandGroup>
                  {matches.map(match => (
                    <CommandItem
                      key={`${match.canonical_symbol}-${match.provider_id}`}
                      onSelect={() => {
                        onSelect(match)
                        setOpen(false)
                        setQuery('')
                      }}
                      value={`${match.canonical_symbol}-${match.provider_id}`}
                    >
                      <span className="font-medium text-foreground">{match.canonical_symbol}</span>
                      <span className="min-w-0 flex-1 truncate text-muted-foreground">{match.display_name}</span>
                      <span className="ml-auto shrink-0 text-[0.6rem] uppercase tracking-wide text-muted-foreground/70">
                        {enumLabel(t.finance.enums.securityType, match.security_type)} · {match.market}
                      </span>
                    </CommandItem>
                  ))}
                </CommandGroup>
              </>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

// ── Small form field wrapper ─────────────────────────────────────────────────

function Field({ children, htmlFor, label }: { children: ReactNode; htmlFor?: string; label: string }) {
  return (
    <div className="grid gap-1.5">
      <label className="text-xs font-medium text-foreground" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
    </div>
  )
}

// ── Add account dialog ───────────────────────────────────────────────────────

function AddAccountDialog({ onClose, onDone, open }: { onClose: () => void; onDone: () => void; open: boolean }) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const [name, setName] = useState('')
  const [market, setMarket] = useState<FinancePortfolioMarket>('US')
  const [currency, setCurrency] = useState('USD')
  const [provider, setProvider] = useState<string>('manual')
  const [accountType, setAccountType] = useState<FinanceAccountType>('cash')
  const [includeInRisk, setIncludeInRisk] = useState(true)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (open) {
      setName('')
      setMarket('US')
      setCurrency('USD')
      setProvider('manual')
      setAccountType('cash')
      setIncludeInRisk(true)
      setNote('')
      setError('')
    }
  }, [open])

  const mutation = useMutation({
    mutationFn: () =>
      postPortfolioAccount({
        account_type: accountType,
        actor: FINANCE_ACTOR,
        base_currency: currency,
        include_in_risk: includeInRisk,
        market_scope: market,
        name: name.trim(),
        provider,
        ...(note.trim() ? { note: note.trim() } : {})
      }),
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.accountSaveFailed),
    onSuccess: account => {
      notify({ kind: 'success', message: '', title: copy.accountCreated(account.name) })
      onDone()
      onClose()
    }
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()

    if (!name.trim()) {
      setError(copy.nameRequired)

      return
    }

    setError('')
    mutation.mutate()
  }

  return (
    <Dialog onOpenChange={value => !value && !mutation.isPending && onClose()} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{copy.addAccountTitle}</DialogTitle>
          <DialogDescription>{copy.addAccountDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <Field htmlFor="acct-name" label={copy.fieldName}>
            <Input
              id="acct-name"
              onChange={event => setName(event.target.value)}
              placeholder={copy.namePlaceholder}
              value={name}
            />
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label={copy.fieldMarket}>
              <Select
                onValueChange={next => {
                  const value = next as FinancePortfolioMarket
                  setMarket(value)
                  setCurrency(MARKET_CURRENCY[value])
                }}
                value={market}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MARKET_OPTIONS.map(option => (
                    <SelectItem key={option} value={option}>
                      {enumLabel(t.finance.enums.market, option)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label={copy.fieldBaseCurrency}>
              <Select onValueChange={setCurrency} value={currency}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {CURRENCY_OPTIONS.map(option => (
                    <SelectItem key={option} value={option}>
                      {option}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label={copy.fieldProvider}>
              <Select onValueChange={setProvider} value={provider}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {PROVIDER_OPTIONS.map(option => (
                    <SelectItem key={option} value={option}>
                      {enumLabel(t.finance.enums.provider, option)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field label={copy.fieldAccountType}>
              <Select onValueChange={next => setAccountType(next as FinanceAccountType)} value={accountType}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ACCOUNT_TYPE_OPTIONS.map(option => (
                    <SelectItem key={option} value={option}>
                      {enumLabel(t.finance.enums.accountType, option)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          <label className="flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2.5 py-2">
            <span className="min-w-0">
              <span className="block text-xs font-medium text-foreground">{copy.fieldIncludeInRisk}</span>
              <span className="block text-[0.62rem] text-muted-foreground/70">{copy.includeInRiskHint}</span>
            </span>
            <Switch checked={includeInRisk} onCheckedChange={setIncludeInRisk} />
          </label>

          <Field htmlFor="acct-note" label={copy.fieldNote}>
            <Textarea
              id="acct-note"
              onChange={event => setNote(event.target.value)}
              placeholder={copy.notePlaceholder}
              value={note}
            />
          </Field>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={mutation.isPending} type="submit">
              {mutation.isPending ? copy.creating : copy.createAccount}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── Edit account dialog ──────────────────────────────────────────────────────

function EditAccountDialog({
  account,
  onClose,
  onDone
}: {
  account: FinancePortfolioAccount | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const open = account !== null
  const [name, setName] = useState('')
  const [accountType, setAccountType] = useState<FinanceAccountType>('cash')
  const [includeInRisk, setIncludeInRisk] = useState(true)
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (account) {
      setName(account.name)
      setAccountType(account.account_type)
      setIncludeInRisk(account.include_in_risk)
      setNote(account.note)
      setError('')
    }
  }, [account])

  const mutation = useMutation({
    mutationFn: () => {
      if (!account) {
        throw new Error('no account')
      }

      return postPortfolioAccountUpdate(account.id, {
        account_type: accountType,
        actor: FINANCE_ACTOR,
        include_in_risk: includeInRisk,
        name: name.trim(),
        note: note.trim()
      })
    },
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.accountSaveFailed),
    onSuccess: updated => {
      notify({ kind: 'success', message: '', title: copy.accountUpdated(updated.name) })
      onDone()
      onClose()
    }
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()

    if (!name.trim()) {
      setError(copy.nameRequired)

      return
    }

    setError('')
    mutation.mutate()
  }

  return (
    <Dialog onOpenChange={value => !value && !mutation.isPending && onClose()} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{account ? copy.editAccountTitle(account.name) : copy.editAccount}</DialogTitle>
          <DialogDescription>{copy.editAccountDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <Field htmlFor="edit-acct-name" label={copy.fieldName}>
            <Input id="edit-acct-name" onChange={event => setName(event.target.value)} value={name} />
          </Field>

          <Field label={copy.fieldAccountType}>
            <Select onValueChange={next => setAccountType(next as FinanceAccountType)} value={accountType}>
              <SelectTrigger>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {ACCOUNT_TYPE_OPTIONS.map(option => (
                  <SelectItem key={option} value={option}>
                    {enumLabel(t.finance.enums.accountType, option)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>

          <label className="flex items-center justify-between gap-2 rounded-md border border-(--ui-stroke-tertiary) px-2.5 py-2">
            <span className="min-w-0">
              <span className="block text-xs font-medium text-foreground">{copy.fieldIncludeInRisk}</span>
              <span className="block text-[0.62rem] text-muted-foreground/70">{copy.includeInRiskHint}</span>
            </span>
            <Switch checked={includeInRisk} onCheckedChange={setIncludeInRisk} />
          </label>

          <Field htmlFor="edit-acct-note" label={copy.fieldNote}>
            <Textarea
              id="edit-acct-note"
              onChange={event => setNote(event.target.value)}
              placeholder={copy.notePlaceholder}
              value={note}
            />
          </Field>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={mutation.isPending} type="submit">
              {mutation.isPending ? t.common.saving : copy.saveAccount}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── Record trade dialog (draft → confirm) ────────────────────────────────────

function RecordTradeDialog({
  account,
  onClose,
  onDone
}: {
  account: FinancePortfolioAccount | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const open = account !== null
  const [instrument, setInstrument] = useState<FinanceInstrumentMatch | null>(null)
  const [eventType, setEventType] = useState<string>('buy')
  const [qty, setQty] = useState('')
  const [price, setPrice] = useState('')
  const [commission, setCommission] = useState('')
  const [occurredAt, setOccurredAt] = useState('')
  const [note, setNote] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    if (account) {
      setInstrument(null)
      setEventType('buy')
      setQty('')
      setPrice('')
      setCommission('')
      setOccurredAt('')
      setNote('')
      setError('')
    }
  }, [account])

  // One user intent = create a draft (surface desktop, human created_by) then
  // confirm it (surface desktop, human actor) so the manual entry lands as a
  // holding. Confirm carries a human actor/surface or the service 403s.
  const mutation = useMutation({
    mutationFn: async () => {
      if (!account || !instrument) {
        throw new Error('missing input')
      }

      const draft = await postPortfolioDraft({
        account_id: account.id,
        created_by: FINANCE_ACTOR,
        currency: instrument.currency,
        event_type: eventType,
        market: instrument.market,
        qty: Number(qty),
        symbol: instrument.canonical_symbol,
        ...(price.trim() ? { price: Number(price) } : {}),
        ...(commission.trim() ? { commission: Number(commission) } : {}),
        ...(occurredAt ? { occurred_at: new Date(occurredAt).toISOString() } : {}),
        ...(note.trim() ? { note: note.trim() } : {})
      })

      return postPortfolioDraftAction(draft.id, {
        action: 'confirm',
        actor: FINANCE_ACTOR,
        expected_version: draft.version,
        idempotency_key: randomId()
      })
    },
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.tradeFailed),
    onSuccess: () => {
      notify({ kind: 'success', message: '', title: copy.tradeRecorded(instrument?.canonical_symbol ?? '—') })
      onDone()
      onClose()
    }
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()

    if (!instrument) {
      setError(copy.instrumentRequired)

      return
    }

    const qtyValue = Number(qty.trim())

    if (!qty.trim()) {
      setError(copy.qtyRequired)

      return
    }

    if (!Number.isFinite(qtyValue) || qtyValue <= 0) {
      setError(copy.positiveNumber(copy.fieldQty))

      return
    }

    if (price.trim() && (!Number.isFinite(Number(price)) || Number(price) <= 0)) {
      setError(copy.positiveNumber(copy.fieldPrice))

      return
    }

    if (commission.trim() && (!Number.isFinite(Number(commission)) || Number(commission) < 0)) {
      setError(copy.positiveNumber(copy.fieldCommission))

      return
    }

    setError('')
    mutation.mutate()
  }

  return (
    <Dialog onOpenChange={value => !value && !mutation.isPending && onClose()} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{copy.recordTradeTitle(account?.name ?? '')}</DialogTitle>
          <DialogDescription>{copy.recordTradeDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <Field label={copy.fieldInstrument}>
            <InstrumentCombobox
              enabled={open}
              market={account?.market_scope}
              onSelect={match => {
                setInstrument(match)
                setError('')
              }}
              value={instrument}
            />
            {instrument && (
              <span className="text-[0.62rem] text-muted-foreground/70">
                {copy.instrumentPicked(instrument.canonical_symbol, instrument.market, instrument.currency)}
              </span>
            )}
          </Field>

          <div className="grid grid-cols-2 gap-3">
            <Field label={copy.fieldEventType}>
              <Select onValueChange={setEventType} value={eventType}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TRADE_EVENT_TYPES.map(option => (
                    <SelectItem key={option} value={option}>
                      {enumLabel(t.finance.enums.eventType, option)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>

            <Field htmlFor="trade-qty" label={copy.fieldQty}>
              <Input
                id="trade-qty"
                inputMode="decimal"
                onChange={event => setQty(event.target.value)}
                placeholder="—"
                value={qty}
              />
            </Field>

            <Field htmlFor="trade-price" label={copy.fieldPrice}>
              <Input
                id="trade-price"
                inputMode="decimal"
                onChange={event => setPrice(event.target.value)}
                placeholder={copy.priceUnknownHint}
                value={price}
              />
            </Field>

            <Field htmlFor="trade-commission" label={copy.fieldCommission}>
              <Input
                id="trade-commission"
                inputMode="decimal"
                onChange={event => setCommission(event.target.value)}
                placeholder="—"
                value={commission}
              />
            </Field>

            <Field htmlFor="trade-occurred" label={copy.fieldOccurredAt}>
              <Input
                id="trade-occurred"
                onChange={event => setOccurredAt(event.target.value)}
                type="datetime-local"
                value={occurredAt}
              />
            </Field>
          </div>

          <Field htmlFor="trade-note" label={copy.fieldNote}>
            <Input
              id="trade-note"
              onChange={event => setNote(event.target.value)}
              placeholder={copy.notePlaceholder}
              value={note}
            />
          </Field>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={mutation.isPending} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={mutation.isPending} type="submit">
              {mutation.isPending ? copy.submitting : copy.submitTrade}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── Edit draft dialog (edit + confirm in one action) ─────────────────────────

const DRAFT_EDIT_KEYS = ['qty', 'price', 'commission'] as const

function EditDraftDialog({
  draft,
  onClose,
  onSubmit,
  submitting
}: {
  draft: FinancePortfolioDraft | null
  onClose: () => void
  onSubmit: (edits: Record<string, number | string>) => void
  submitting: boolean
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const open = draft !== null

  const [values, setValues] = useState<Record<(typeof DRAFT_EDIT_KEYS)[number] | 'note', string>>({
    commission: '',
    note: '',
    price: '',
    qty: ''
  })

  const [error, setError] = useState('')

  const fieldLabels: Record<(typeof DRAFT_EDIT_KEYS)[number], string> = {
    commission: copy.fieldCommission,
    price: copy.fieldPrice,
    qty: copy.fieldQty
  }

  useEffect(() => {
    if (draft) {
      setValues({
        commission: draft.commission?.toString() ?? '',
        note: draft.note ?? '',
        price: draft.price?.toString() ?? '',
        qty: draft.qty?.toString() ?? ''
      })
      setError('')
    }
  }, [draft])

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const edits: Record<string, number | string> = {}

    for (const key of DRAFT_EDIT_KEYS) {
      const raw = values[key].trim()

      if (!raw) {
        continue
      }

      const parsed = Number(raw)
      const floor = key === 'commission' ? 0 : Number.MIN_VALUE

      if (!Number.isFinite(parsed) || parsed < floor) {
        setError(copy.positiveNumber(fieldLabels[key]))

        return
      }

      edits[key] = parsed
    }

    if (values.note.trim()) {
      edits.note = values.note.trim()
    }

    setError('')
    onSubmit(edits)
  }

  return (
    <Dialog onOpenChange={value => !value && !submitting && onClose()} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{copy.editDraftTitle}</DialogTitle>
          <DialogDescription>{copy.editDraftDesc}</DialogDescription>
        </DialogHeader>

        <form className="grid gap-3" onSubmit={handleSubmit}>
          <div className="grid grid-cols-3 gap-3">
            {DRAFT_EDIT_KEYS.map(key => (
              <Field htmlFor={`draft-edit-${key}`} key={key} label={fieldLabels[key]}>
                <Input
                  id={`draft-edit-${key}`}
                  inputMode="decimal"
                  onChange={event => setValues(prev => ({ ...prev, [key]: event.target.value }))}
                  placeholder={key === 'price' ? copy.priceUnknownHint : '—'}
                  value={values[key]}
                />
              </Field>
            ))}
          </div>

          <Field htmlFor="draft-edit-note" label={copy.fieldNote}>
            <Input
              id="draft-edit-note"
              onChange={event => setValues(prev => ({ ...prev, note: event.target.value }))}
              placeholder={copy.notePlaceholder}
              value={values.note}
            />
          </Field>

          {error && <ErrorBanner>{error}</ErrorBanner>}

          <DialogFooter>
            <Button disabled={submitting} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button disabled={submitting} type="submit">
              {submitting ? t.common.saving : copy.saveConfirm}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

// ── CSV import dialog (paste → preview → commit) ─────────────────────────────

function ImportDialog({
  account,
  onClose,
  onDone
}: {
  account: FinancePortfolioAccount | null
  onClose: () => void
  onDone: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.holdings
  const open = account !== null
  const [csv, setCsv] = useState('')
  const [preview, setPreview] = useState<FinanceImportPreview | null>(null)

  useEffect(() => {
    if (account) {
      setCsv('')
      setPreview(null)
    }
  }, [account])

  const previewMutation = useMutation({
    mutationFn: () => {
      if (!account) {
        throw new Error('no account')
      }

      return postPortfolioImportPreview(account.id, csv)
    },
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.previewFailed),
    onSuccess: result => setPreview(result)
  })

  const commitMutation = useMutation({
    mutationFn: () => {
      if (!account) {
        throw new Error('no account')
      }

      return postPortfolioImportCommit(account.id, csv, FINANCE_ACTOR)
    },
    onError: error_ => notifyError(new Error(parseFinanceError(error_).message), copy.importFailed),
    onSuccess: result => {
      notify({ kind: 'success', message: '', title: copy.importCommitted(result.n_committed) })
      onDone()
      onClose()
    }
  })

  const busy = previewMutation.isPending || commitMutation.isPending

  return (
    <Dialog onOpenChange={value => !value && !busy && onClose()} open={open}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>{copy.importTitle(account?.name ?? '')}</DialogTitle>
          <DialogDescription>{copy.importDesc}</DialogDescription>
        </DialogHeader>

        <div className="grid gap-3">
          <Field htmlFor="import-csv" label={copy.csvLabel}>
            <Textarea
              className="min-h-28 font-mono text-[0.7rem]"
              id="import-csv"
              onChange={event => {
                setCsv(event.target.value)
                setPreview(null)
              }}
              placeholder={copy.csvPlaceholder}
              value={csv}
            />
            <span className="text-[0.62rem] text-muted-foreground/70">{copy.csvColumnsHint}</span>
          </Field>

          {preview && (
            <div className="space-y-2">
              {preview.header_error ? (
                <ErrorBanner>{copy.importHeaderError(preview.header_error)}</ErrorBanner>
              ) : (
                <div className="flex flex-wrap items-center gap-2 text-[0.65rem] text-(--ui-text-secondary)">
                  <FinancePill variant="muted">
                    {copy.importCounts(preview.n_valid, preview.n_invalid, preview.n_duplicate)}
                  </FinancePill>
                  {!preview.committable && (
                    <span className="text-amber-600 dark:text-amber-300">{copy.importNotCommittable}</span>
                  )}
                </div>
              )}

              {preview.rows.length > 0 && (
                <FinanceTable
                  columns={[
                    { align: 'right', label: copy.importColLine },
                    { label: copy.importColStatus },
                    { label: copy.importColEvent },
                    { label: copy.importColSymbol },
                    { align: 'right', label: copy.colQty },
                    { align: 'right', label: copy.colPrice }
                  ]}
                  rows={preview.rows.map(row => ({
                    cells: [
                      row.line,
                      <span
                        className={cn(
                          row.errors.length > 0
                            ? 'text-destructive'
                            : row.duplicate
                              ? 'text-amber-600 dark:text-amber-300'
                              : 'text-primary'
                        )}
                        key="st"
                      >
                        {row.errors.length > 0
                          ? row.errors.join('; ')
                          : row.duplicate
                            ? copy.importRowDuplicate
                            : copy.importRowOk}
                      </span>,
                      row.event_type ? enumLabel(t.finance.enums.eventType, row.event_type) : '—',
                      row.symbol ?? '—',
                      fmtQty(row.qty),
                      fmtPrice(row.price)
                    ],
                    key: String(row.line)
                  }))}
                />
              )}
            </div>
          )}

          <DialogFooter>
            <Button disabled={busy} onClick={onClose} type="button" variant="outline">
              {t.common.cancel}
            </Button>
            <Button
              disabled={busy || csv.trim().length === 0}
              onClick={() => previewMutation.mutate()}
              type="button"
              variant="secondary"
            >
              {previewMutation.isPending ? copy.previewing : copy.preview}
            </Button>
            <Button
              disabled={busy || !preview || !preview.committable}
              onClick={() => commitMutation.mutate()}
              type="button"
            >
              {commitMutation.isPending ? copy.committing : copy.commit}
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  )
}
