import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { dispose, init } from 'klinecharts'
import type { Chart, DeepPartial, KLineData, NeighborData, Nullable, Period, PeriodType, Styles, TooltipLegend } from 'klinecharts'
import { useEffect, useMemo, useRef, useState } from 'react'

import { useIsDark } from '@/components/assistant-ui/embeds/use-is-dark'
import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { SegmentedControl, type SegmentedControlOption } from '@/components/ui/segmented-control'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue
} from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import {
  type FinanceAnalyze,
  financeAnalyze,
  type FinanceAnalyzeCitation,
  type FinanceAnalyzeSignal,
  type FinanceBar,
  financeBars,
  financeQuote,
  type FinanceQuote
} from '@/hermes'
import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/external-link'
import { Info, SlidersHorizontal } from '@/lib/icons'
import { fmtDate, fmtDateTime } from '@/lib/time'
import { cn } from '@/lib/utils'

import {
  enumLabel,
  financeKey,
  fmtPct,
  fmtQty,
  fmtTs,
  fmtWatchPrice,
  fmtWatchValue,
  parseFinanceError,
  type WatchCurrency,
  type WatchUnitKey
} from './lib'
import { FinanceCard, FinancePill, FinanceSectionLabel, InlineSpinner } from './primitives'

// Read-only cross-asset watch modules (Loop.md §3). These read three data-only
// endpoints — quote / bars / analyze — and carry NO order or approval path.
// Some tickers (GC=F, ^TNX, 518880.SS) return 404 from yfinance intermittently,
// so every per-symbol query fails softly to an inline "no data" note.
//
// The chart is a professional, TradingView-style K-line rendered by klinecharts:
// ONE large chart per page with a symbol dropdown (top-left), an indicators menu
// (each tool carries a short educational description), and a timeframe switcher
// (bottom-right). klinecharts owns the candles, axes, crosshair, tooltip, pan
// and zoom — we only feed it OHLCV bars + theme colors and toggle its built-in
// indicators.

export const WATCH_MODULE_IDS = ['gold', 'oil', 'rates', 'crypto'] as const

export type WatchModuleId = (typeof WATCH_MODULE_IDS)[number]

// A DERIVED symbol has no Yahoo quote of its own; its price + candles are
// computed from a base future rescaled by an FX rate. AU9999 (国内金价) =
// GC=F(USD/oz) × CNY=X(CNY/USD) / gramsPerOunce → ¥/gram. The candle SHAPE
// equals the base; only the scale changes.
export interface WatchDerivedSpec {
  base: string
  fx: string
  gramsPerOunce: number
}

// Per-symbol watch config: the ticker plus how its price reads (currency + a
// localized unit key). `null` currency + `pct` unit → a yield ("4.30 %");
// currency + `null` unit → a currency-prefixed number ("$67,000"); currency +
// unit → "value currency / unit" ("4,079 $ / 盎司"). `derived` turns the entry
// into a synthetic symbol (see WatchDerivedSpec).
export interface WatchSymbolConfig {
  symbol: string
  currency: WatchCurrency | null
  unit: WatchUnitKey | null
  derived?: WatchDerivedSpec
}

// Symbol universe per module (defined ONCE, shared). Display labels are
// resolved from the i18n `watch.labels` map — tickers themselves never change.
// AU9999 is a DERIVED entry (base GC=F × CNY=X) appended to the gold module.
export const WATCH_MODULE_SYMBOLS: Record<WatchModuleId, readonly WatchSymbolConfig[]> = {
  gold: [
    { symbol: 'GC=F', currency: '$', unit: 'oz' },
    { symbol: 'GLD', currency: '$', unit: 'share' },
    { symbol: '518880.SS', currency: '¥', unit: 'share' },
    { symbol: 'AU9999', currency: '¥', unit: 'g', derived: { base: 'GC=F', fx: 'CNY=X', gramsPerOunce: 31.1035 } }
  ],
  oil: [
    { symbol: 'CL=F', currency: '$', unit: 'bbl' },
    { symbol: 'BZ=F', currency: '$', unit: 'bbl' },
    { symbol: 'USO', currency: '$', unit: 'share' }
  ],
  rates: [
    { symbol: '^TNX', currency: null, unit: 'pct' },
    { symbol: 'TLT', currency: '$', unit: 'share' }
  ],
  crypto: [
    { symbol: 'BTC-USD', currency: '$', unit: null },
    { symbol: 'ETH-USD', currency: '$', unit: null }
  ]
}

// On-chart timeframe presets. Each maps to the (timeframe, limit) passed to
// financeBars plus the klinecharts Period (drives x-axis time granularity:
// intraday shows HH:MM, longer views show dates). The DATE RANGE auto-adjusts
// per preset: an intraday session, five days, ~1 year of daily candles, ~5
// years of weekly candles, ~20 years of monthly candles. Bars are cached per
// (symbol, timeframe) so flipping back to a previously-viewed preset is instant.
type TimeframeId = 'day' | 'intraday1d' | 'intraday5d' | 'month' | 'week'

interface TimeframePreset {
  limit: number
  period: Period
  timeframe: string
}

const TIMEFRAME_PRESETS: Record<TimeframeId, TimeframePreset> = {
  intraday1d: { limit: 78, period: { span: 5, type: 'minute' }, timeframe: '5m' }, // one intraday session
  intraday5d: { limit: 130, period: { span: 30, type: 'minute' }, timeframe: '30m' }, // ~5 days
  day: { limit: 250, period: { span: 1, type: 'day' }, timeframe: '1d' }, // ~1 year — DEFAULT
  week: { limit: 260, period: { span: 1, type: 'week' }, timeframe: '1wk' }, // ~5 years
  month: { limit: 240, period: { span: 1, type: 'month' }, timeframe: '1mo' } // ~20 years
}

// Rendered left-to-right order of the switcher (intraday → longer horizon).
const TIMEFRAME_ORDER: TimeframeId[] = ['intraday1d', 'intraday5d', 'day', 'week', 'month']

const DEFAULT_TIMEFRAME: TimeframeId = 'day'

// Longer intraday periods want date+time in the tooltip; daily and up want just
// the date.
const INTRADAY_PERIODS: PeriodType[] = ['second', 'minute', 'hour']

// Long stale/gc windows keep each (symbol, timeframe) chart cached so navigating
// away and back — or re-selecting a preset — renders instantly from cache.
const BARS_STALE_MS = 60_000
const BARS_GC_MS = 30 * 60_000
const QUOTE_STALE_MS = 60_000

// Prices read cleanly at two decimals across every watch instrument (gold, oil,
// BTC, ETFs, yields); volume is whole-number.
const PRICE_PRECISION = 2
const VOLUME_PRECISION = 0

// klinecharts' constant id for the main candle pane — overlay indicators (MA /
// EMA / BOLL) and the currency y-axis override target it directly.
const CANDLE_PANE_ID = 'candle_pane'

// TradingView-style up/down palette — reads over both light and dark themes and
// keeps the MA/close-value coloring consistent with the candles.
const UP_COLOR = '#26a69a'
const DOWN_COLOR = '#ef5350'

// The MA overlay defaults to MA20 + MA30 curves (klinecharts computes them as
// proper moving-average lines).
const MA_CALC_PARAMS = [20, 30]

// Every toggleable indicator. Overlays ride the candle pane; the rest open their
// own sub-pane. VOL + MA start ON.
type IndicatorKey = 'BOLL' | 'EMA' | 'KDJ' | 'MA' | 'MACD' | 'RSI' | 'VOL'

const OVERLAY_INDICATORS: IndicatorKey[] = ['MA', 'EMA', 'BOLL']
const SUBCHART_INDICATORS: IndicatorKey[] = ['VOL', 'MACD', 'RSI', 'KDJ']
const ALL_INDICATORS: IndicatorKey[] = [...OVERLAY_INDICATORS, ...SUBCHART_INDICATORS]

const DEFAULT_INDICATORS: Record<IndicatorKey, boolean> = {
  MA: true,
  EMA: false,
  BOLL: false,
  VOL: true,
  MACD: false,
  RSI: false,
  KDJ: false
}

const isOverlayIndicator = (key: IndicatorKey): boolean => OVERLAY_INDICATORS.includes(key)

// Stable empty reference so a symbol with no bars yet doesn't churn renders.
const NO_BARS: FinanceBar[] = []

export function WatchModulePanel({ enabled, module }: { enabled: boolean; module: WatchModuleId }) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const symbols = WATCH_MODULE_SYMBOLS[module]
  const [selected, setSelected] = useState<string>(symbols[0].symbol)
  const [showAnalysis, setShowAnalysis] = useState(false)

  // Keep the selection valid when the sidebar switches modules (the symbol set
  // changes); default to the first symbol so the single Analyze always targets
  // something concrete.
  const activeSymbol = symbols.some(config => config.symbol === selected) ? selected : symbols[0].symbol
  const activeConfig = symbols.find(config => config.symbol === activeSymbol) ?? symbols[0]

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div className="space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-[0.9375rem] font-semibold tracking-tight text-foreground">{copy.modules[module]}</h3>
            <FinancePill variant="muted">{copy.readOnlyTag}</FinancePill>
          </div>
          <ReadOnlyNote text={copy.readOnlyNote} />
        </div>
        {/* Exactly ONE Analyze button per page, top-right of the content area —
            no icon; it analyzes the currently selected symbol (Loop.md §3:
            read-only, no order/approve path). */}
        <Button className="shrink-0" onClick={() => setShowAnalysis(value => !value)} size="sm" variant="outline">
          {showAnalysis ? copy.hideAnalysis : copy.analyze}
        </Button>
      </header>

      {showAnalysis && (
        <FinanceCard className="space-y-3">
          <div className="flex items-center gap-2">
            <FinanceSectionLabel>{copy.analyze}</FinanceSectionLabel>
            <span className="text-xs font-semibold tracking-tight text-foreground">{activeSymbol}</span>
          </div>
          <AnalyzePanel enabled={enabled} symbol={activeSymbol} />
        </FinanceCard>
      )}

      <WatchChartPanel config={activeConfig} enabled={enabled} onSelect={setSelected} symbols={symbols} />

      <p className="text-[0.62rem] leading-4 text-muted-foreground/70">{copy.delayNote}</p>
    </div>
  )
}

function ReadOnlyNote({ text }: { text: string }) {
  return (
    <div
      className={cn(
        'flex items-start gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) px-3 py-2',
        'text-[0.7rem] leading-5 text-(--ui-text-secondary)'
      )}
    >
      <Info className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
      <div className="min-w-0">{text}</div>
    </div>
  )
}

// Fetches (and, for derived symbols, rescales) the quote + candles for one
// watch symbol at a timeframe. Extracted so the single big chart shares the same
// soft-failing data path the per-card charts used to have.
function useWatchSymbolData(config: WatchSymbolConfig, timeframe: TimeframeId, enabled: boolean) {
  const preset = TIMEFRAME_PRESETS[timeframe]
  const { derived, symbol } = config
  // A derived symbol (AU9999) has no Yahoo endpoint of its own — its price +
  // candles are fetched from the BASE future and rescaled by the FX quote.
  const dataSymbol = derived ? derived.base : symbol

  const quoteQuery = useQuery({
    enabled,
    queryFn: () => financeQuote(dataSymbol),
    queryKey: financeKey('watch', 'quote', dataSymbol),
    retry: false,
    staleTime: QUOTE_STALE_MS
  })

  const barsQuery = useQuery({
    enabled,
    gcTime: BARS_GC_MS,
    placeholderData: keepPreviousData,
    queryFn: () => financeBars(dataSymbol, { limit: preset.limit, timeframe: preset.timeframe }),
    queryKey: financeKey('watch', 'bars', dataSymbol, preset.timeframe),
    retry: false,
    staleTime: BARS_STALE_MS
  })

  // FX quote for the derived rescale (CNY=X). Only fetched for derived symbols.
  const fxQuery = useQuery({
    enabled: enabled && Boolean(derived),
    queryFn: () => financeQuote(derived?.fx ?? ''),
    queryKey: financeKey('watch', 'quote', derived?.fx ?? 'none'),
    retry: false,
    staleTime: QUOTE_STALE_MS
  })

  const baseQuote = quoteQuery.data
  const baseBars = barsQuery.data?.bars ?? NO_BARS
  // ¥/gram factor = CNY-per-USD ÷ grams-per-ounce, applied to the USD/oz base.
  const fxLast = fxQuery.data?.last ?? null
  const factor = derived && fxLast !== null && Number.isFinite(fxLast) ? fxLast / derived.gramsPerOunce : null

  // Synthetic quote: rescale the base last to ¥/gram. A derived value has no
  // real bid/ask/volume, so those are dropped (the header just shows Last).
  const quote = useMemo<FinanceQuote | undefined>(() => {
    if (!derived) {
      return baseQuote
    }

    if (!baseQuote || factor === null || baseQuote.last === null) {
      return undefined
    }

    return { symbol, last: baseQuote.last * factor, bid: null, ask: null, volume: null, as_of: baseQuote.as_of }
  }, [derived, baseQuote, factor, symbol])

  // Synthetic candles: the base's OHLC rescaled by the same factor — identical
  // SHAPE, ¥/gram scale. Volume + ts are carried through unchanged.
  const bars = useMemo<FinanceBar[]>(() => {
    if (!derived) {
      return baseBars
    }

    if (factor === null) {
      return NO_BARS
    }

    return baseBars.map(bar => ({
      ...bar,
      open: bar.open * factor,
      high: bar.high * factor,
      low: bar.low * factor,
      close: bar.close * factor
    }))
  }, [derived, baseBars, factor])

  // A derived symbol is broken when EITHER input quote errors or yields no
  // usable value — surfaced as a distinct no-data note (never a crash).
  const derivedBroken =
    Boolean(derived) &&
    (quoteQuery.isError ||
      fxQuery.isError ||
      (fxQuery.isSuccess && (fxLast === null || !Number.isFinite(fxLast))) ||
      (quoteQuery.isSuccess && (baseQuote?.last === null || baseQuote?.last === undefined)))

  return {
    bars,
    barsFailed: derived ? barsQuery.isError || derivedBroken : barsQuery.isError,
    barsPending: barsQuery.isPending || (Boolean(derived) && fxQuery.isPending),
    derivedBroken,
    quote,
    quoteFailed: derived ? derivedBroken : quoteQuery.isError,
    quotePending: quoteQuery.isPending || (Boolean(derived) && fxQuery.isPending)
  }
}

// The single big-chart page: a symbol dropdown + last price header, an
// indicators menu, the large klinecharts K-line, and the bottom-right timeframe
// switcher. All chart-level UI state (timeframe, active indicators) lives here
// so it survives symbol switches.
function WatchChartPanel({
  config,
  enabled,
  onSelect,
  symbols
}: {
  config: WatchSymbolConfig
  enabled: boolean
  onSelect: (symbol: string) => void
  symbols: readonly WatchSymbolConfig[]
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const [timeframe, setTimeframe] = useState<TimeframeId>(DEFAULT_TIMEFRAME)
  const [indicators, setIndicators] = useState<Record<IndicatorKey, boolean>>(DEFAULT_INDICATORS)

  const { currency, derived, symbol, unit } = config
  const unitWord = unit ? copy.units[unit] : null
  const label = copy.labels[symbol] ?? symbol

  const { bars, barsFailed, barsPending, derivedBroken, quote, quoteFailed, quotePending } = useWatchSymbolData(
    config,
    timeframe,
    enabled
  )

  const toggleIndicator = (key: IndicatorKey) =>
    setIndicators(current => ({ ...current, [key]: !current[key] }))

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <SymbolDropdown labels={copy.labels} onSelect={onSelect} symbols={symbols} value={symbol} />
        <div className="text-right">
          {quotePending ? (
            <span className="text-sm text-muted-foreground">—</span>
          ) : quoteFailed ? (
            <span className="text-[0.7rem] text-muted-foreground">{derived ? copy.derivedNoData : copy.quoteError}</span>
          ) : (
            <>
              <div className="flex items-baseline justify-end gap-1.5">
                <span className="text-[0.6rem] font-medium text-(--ui-text-tertiary)">{copy.last}</span>
                <span className="text-base font-semibold tabular-nums text-foreground">
                  {fmtWatchPrice(quote?.last, currency, unit, unitWord)}
                </span>
              </div>
              {quote?.as_of && (
                <div className="text-[0.6rem] text-muted-foreground/70">{copy.quoteAsOf(fmtTs(quote.as_of))}</div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Provenance for the DERIVED symbol (AU9999 = intl gold × USD/CNY). */}
      {derived && <p className="text-[0.6rem] leading-4 text-muted-foreground/70">{copy.derivedNote}</p>}

      {!quoteFailed && quote && (quote.bid !== null || quote.ask !== null || quote.volume !== null) && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.65rem] tabular-nums text-muted-foreground">
          {quote.bid !== null && (
            <span>
              {copy.bid} <span className="text-foreground">{fmtWatchValue(quote.bid, currency, unit, unitWord)}</span>
            </span>
          )}
          {quote.ask !== null && (
            <span>
              {copy.ask} <span className="text-foreground">{fmtWatchValue(quote.ask, currency, unit, unitWord)}</span>
            </span>
          )}
          {quote.volume !== null && (
            <span>
              {copy.volume} <span className="text-foreground">{fmtQty(quote.volume)}</span>
            </span>
          )}
        </div>
      )}

      <div className="flex items-center justify-between gap-2">
        <IndicatorsMenu indicators={indicators} onToggle={toggleIndicator} />
      </div>

      <div className="h-[480px] w-full overflow-hidden rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary)">
        {barsPending ? (
          <ChartSkeleton label={copy.chartLoading} />
        ) : barsFailed || bars.length === 0 ? (
          <div className="flex h-full items-center justify-center px-4 text-center text-xs text-muted-foreground">
            {derived && derivedBroken ? copy.derivedNoData : barsFailed ? copy.noData : copy.chartEmpty}
          </div>
        ) : (
          <KlineChart
            aria={copy.chartAria(label)}
            bars={bars}
            currency={currency}
            indicators={indicators}
            key={symbol}
            period={TIMEFRAME_PRESETS[timeframe].period}
            symbol={symbol}
            unit={unit}
            unitWord={unitWord}
          />
        )}
      </div>

      {/* BOTTOM-RIGHT timeframe switcher; each preset auto-adjusts the range. */}
      <div className="flex justify-end">
        <TimeframeSwitcher onChange={setTimeframe} value={timeframe} />
      </div>
    </div>
  )
}

// TOP-LEFT symbol picker. Each option shows the display label + its ticker so
// the target reads clearly (e.g. "COMEX Gold · GC=F").
function SymbolDropdown({
  labels,
  onSelect,
  symbols,
  value
}: {
  labels: Record<string, string>
  onSelect: (symbol: string) => void
  symbols: readonly WatchSymbolConfig[]
  value: string
}) {
  const { t } = useI18n()
  const copy = t.finance.watch

  return (
    <Select onValueChange={onSelect} value={value}>
      <SelectTrigger aria-label={copy.symbolSelectAria} className="min-w-52 max-w-full">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {symbols.map(config => (
          <SelectItem key={config.symbol} value={config.symbol}>
            <span className="flex items-baseline gap-2">
              <span className="font-medium text-foreground">{labels[config.symbol] ?? config.symbol}</span>
              <span className="text-[0.7rem] text-muted-foreground">{config.symbol}</span>
            </span>
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// The "Indicators" control: a popover whose rows each toggle a built-in
// klinecharts indicator and carry a short, localized, educational description
// (this is how the user learns to read the chart). Overlays sit on the price
// pane; the rest open their own sub-pane.
function IndicatorsMenu({
  indicators,
  onToggle
}: {
  indicators: Record<IndicatorKey, boolean>
  onToggle: (key: IndicatorKey) => void
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const activeCount = ALL_INDICATORS.filter(key => indicators[key]).length

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button size="sm" variant="outline">
          <SlidersHorizontal className="size-3.5" />
          {copy.indicatorsButton}
          <span className="tabular-nums text-muted-foreground">{activeCount}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80">
        <div className="space-y-2">
          <IndicatorGroup
            group={copy.indicatorsOverlayGroup}
            indicators={indicators}
            keys={OVERLAY_INDICATORS}
            onToggle={onToggle}
          />
          <IndicatorGroup
            group={copy.indicatorsSubchartGroup}
            indicators={indicators}
            keys={SUBCHART_INDICATORS}
            onToggle={onToggle}
          />
        </div>
      </PopoverContent>
    </Popover>
  )
}

function IndicatorGroup({
  group,
  indicators,
  keys,
  onToggle
}: {
  group: string
  indicators: Record<IndicatorKey, boolean>
  keys: IndicatorKey[]
  onToggle: (key: IndicatorKey) => void
}) {
  const { t } = useI18n()
  const copy = t.finance.watch

  return (
    <div className="space-y-1">
      <FinanceSectionLabel className="px-1">{group}</FinanceSectionLabel>
      {keys.map(key => (
        <label
          className="flex cursor-pointer items-start justify-between gap-3 rounded-md px-1 py-1.5 hover:bg-(--ui-bg-tertiary)"
          key={key}
        >
          <span className="min-w-0 space-y-0.5">
            <span className="block text-xs font-semibold tracking-tight text-foreground">
              {copy.indicatorLabels[key]}
            </span>
            <span className="block text-[0.68rem] leading-4 text-muted-foreground">{copy.indicatorDescriptions[key]}</span>
          </span>
          <Switch checked={indicators[key]} className="mt-0.5 shrink-0" onCheckedChange={() => onToggle(key)} />
        </label>
      ))}
    </div>
  )
}

// Compact segmented timeframe presets under the chart (bottom-right). Selecting
// a preset swaps the (timeframe, limit, period) and re-renders; bars are cached
// per (symbol, timeframe) so re-selecting a previously-viewed preset is instant.
function TimeframeSwitcher({ onChange, value }: { onChange: (id: TimeframeId) => void; value: TimeframeId }) {
  const { t } = useI18n()
  const copy = t.finance.watch

  const options: SegmentedControlOption<TimeframeId>[] = TIMEFRAME_ORDER.map(id => ({
    id,
    label: copy.timeframes[id]
  }))

  return (
    <div aria-label={copy.timeframeAria} role="group">
      <SegmentedControl onChange={onChange} options={options} value={value} />
    </div>
  )
}

// First-load placeholder. Reuses the app's shared Skeleton (animate-pulse) so
// the initial fetch reads as a calm shimmer that matches Hermes. Cache hits skip
// this entirely.
function ChartSkeleton({ label }: { label: string }) {
  return (
    <div aria-label={label} className="grid h-full place-items-center p-4" role="status">
      <Skeleton className="h-full w-full rounded-md" />
    </div>
  )
}

// klinecharts theme + formatting styles for the current light/dark mode. Canvas
// can't read CSS vars, so we hand a coherent, theme-reactive palette to
// setStyles: neutral grid/axes, the shared up/down candle palette, and a
// currency-aware, localized OHLCV tooltip legend.
function buildChartStyles(
  isDark: boolean,
  legend: (data: NeighborData<Nullable<KLineData>>) => TooltipLegend[]
): DeepPartial<Styles> {
  const gridColor = isDark ? 'rgba(255,255,255,0.05)' : 'rgba(0,0,0,0.05)'
  const axisColor = isDark ? 'rgba(255,255,255,0.14)' : 'rgba(0,0,0,0.14)'
  const textColor = isDark ? 'rgba(228,228,231,0.6)' : 'rgba(39,39,42,0.6)'
  const tooltipColor = isDark ? 'rgba(228,228,231,0.92)' : 'rgba(24,24,27,0.9)'
  const crossColor = isDark ? 'rgba(228,228,231,0.5)' : 'rgba(39,39,42,0.5)'
  const crossFill = isDark ? '#3f3f46' : '#52525b'

  const crosshairText = { backgroundColor: crossFill, borderColor: crossFill, color: '#ffffff' }

  return {
    candle: {
      bar: {
        downBorderColor: DOWN_COLOR,
        downColor: DOWN_COLOR,
        downWickColor: DOWN_COLOR,
        upBorderColor: UP_COLOR,
        upColor: UP_COLOR,
        upWickColor: UP_COLOR
      },
      priceMark: { last: { text: { color: '#ffffff' } } },
      tooltip: {
        legend: { color: tooltipColor, template: legend },
        title: { color: textColor }
      }
    },
    crosshair: {
      horizontal: { line: { color: crossColor }, text: crosshairText },
      vertical: { line: { color: crossColor }, text: crosshairText }
    },
    grid: { horizontal: { color: gridColor }, vertical: { color: gridColor } },
    indicator: { tooltip: { legend: { color: tooltipColor }, title: { color: tooltipColor } } },
    xAxis: { axisLine: { color: axisColor }, tickLine: { color: axisColor }, tickText: { color: textColor } },
    yAxis: { axisLine: { color: axisColor }, tickLine: { color: axisColor }, tickText: { color: textColor } }
  }
}

// The large, professional K-line chart. klinecharts owns candles, axes,
// crosshair, tooltip, pan and zoom; we feed it OHLCV bars, react to the app
// theme, apply the currency price axis, and toggle its built-in indicators.
function KlineChart({
  aria,
  bars,
  currency,
  indicators,
  period,
  symbol,
  unit,
  unitWord
}: {
  aria: string
  bars: FinanceBar[]
  currency: WatchCurrency | null
  indicators: Record<IndicatorKey, boolean>
  period: Period
  symbol: string
  unit: WatchUnitKey | null
  unitWord: null | string
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const isDark = useIsDark()
  const containerRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<Chart | null>(null)
  const barsRef = useRef<KLineData[]>([])
  const createdRef = useRef<Partial<Record<IndicatorKey, boolean>>>({})

  const klineData = useMemo<KLineData[]>(
    () =>
      bars.map(bar => ({
        close: bar.close,
        high: bar.high,
        low: bar.low,
        open: bar.open,
        timestamp: new Date(bar.ts).getTime(),
        volume: bar.volume
      })),
    [bars]
  )

  const showTime = INTRADAY_PERIODS.includes(period.type)

  // Currency/unit-aware, localized OHLCV tooltip legend (klinecharts renders the
  // rows; we control the labels + formatting). Rebuilt when the formatting
  // inputs change so setStyles hands the chart a fresh closure.
  const legendTemplate = useMemo(
    () =>
      (data: NeighborData<Nullable<KLineData>>): TooltipLegend[] => {
        const bar = data.current

        if (!bar) {
          return []
        }

        const date = new Date(bar.timestamp)
        const timeText = showTime ? fmtDateTime.format(date) : fmtDate.format(date)
        const up = bar.close >= bar.open

        return [
          { title: copy.hoverTime, value: timeText },
          { title: copy.hoverOpen, value: fmtWatchValue(bar.open, currency, unit, unitWord) },
          { title: copy.hoverHigh, value: fmtWatchValue(bar.high, currency, unit, unitWord) },
          { title: copy.hoverLow, value: fmtWatchValue(bar.low, currency, unit, unitWord) },
          {
            title: copy.hoverClose,
            value: { color: up ? UP_COLOR : DOWN_COLOR, text: fmtWatchValue(bar.close, currency, unit, unitWord) }
          },
          { title: copy.hoverVolume, value: fmtQty(bar.volume ?? 0) }
        ]
      },
    [copy, currency, unit, unitWord, showTime]
  )

  // Init once; dispose on unmount (no leak). A symbol change remounts this
  // component (keyed by symbol upstream), so the instance is always fresh.
  useEffect(() => {
    const el = containerRef.current

    if (!el) {
      return
    }

    const chart = init(el)
    chartRef.current = chart

    return () => {
      if (chartRef.current) {
        dispose(el)
        chartRef.current = null
      }
    }
  }, [])

  // Symbol precision → drives the y-axis price formatting; ticker feeds the
  // default candle tooltip title.
  useEffect(() => {
    chartRef.current?.setSymbol({ pricePrecision: PRICE_PRECISION, ticker: symbol, volumePrecision: VOLUME_PRECISION })
  }, [symbol])

  // Timeframe → drives x-axis time granularity (HH:MM vs dates).
  useEffect(() => {
    chartRef.current?.setPeriod(period)
  }, [period])

  // Feed the bars. A fresh data loader forces klinecharts to reload from the ref
  // (more=false disables its scroll-to-load-more, since we hold the full range).
  useEffect(() => {
    const chart = chartRef.current

    if (!chart) {
      return
    }

    barsRef.current = klineData
    chart.setDataLoader({ getBars: ({ callback }) => callback(barsRef.current, false) })
  }, [klineData])

  // Theme + currency: re-style and re-apply the currency y-axis. Neither resets
  // the data, so the user's pan/zoom survives a theme toggle.
  useEffect(() => {
    const chart = chartRef.current

    if (!chart) {
      return
    }

    chart.setStyles(buildChartStyles(isDark, legendTemplate))
    chart.overrideYAxis({
      createTicks: ({ defaultTicks }) =>
        defaultTicks.map(tick => ({
          ...tick,
          text: unit === 'pct' ? `${tick.text}%` : currency ? `${currency}${tick.text}` : tick.text
        })),
      paneId: CANDLE_PANE_ID
    })
  }, [isDark, legendTemplate, currency, unit])

  // Toggle built-in indicators — create/remove only the ones that changed.
  useEffect(() => {
    const chart = chartRef.current

    if (!chart) {
      return
    }

    for (const key of ALL_INDICATORS) {
      const want = indicators[key]
      const have = Boolean(createdRef.current[key])

      if (want && !have) {
        const overlay = isOverlayIndicator(key)

        if (key === 'MA') {
          chart.createIndicator({ calcParams: MA_CALC_PARAMS, name: key, paneId: CANDLE_PANE_ID }, true)
        } else if (overlay) {
          chart.createIndicator({ name: key, paneId: CANDLE_PANE_ID }, true)
        } else {
          chart.createIndicator({ name: key })
        }

        createdRef.current[key] = true
      } else if (!want && have) {
        chart.removeIndicator(isOverlayIndicator(key) ? { name: key, paneId: CANDLE_PANE_ID } : { name: key })
        createdRef.current[key] = false
      }
    }
  }, [indicators])

  return <div aria-label={aria} className="h-full w-full" ref={containerRef} role="img" />
}

const directionTone = (direction: string): StatusTone =>
  /long|buy|bull|up/i.test(direction) ? 'good' : /short|sell|bear|down/i.test(direction) ? 'bad' : 'muted'

function AnalyzePanel({ enabled, symbol }: { enabled: boolean; symbol: string }) {
  const { t } = useI18n()
  const copy = t.finance.watch

  const analyzeQuery = useQuery({
    enabled,
    queryFn: () => financeAnalyze(symbol),
    queryKey: financeKey('watch', 'analyze', symbol),
    retry: false,
    staleTime: 5 * 60_000
  })

  if (analyzeQuery.isPending) {
    return <InlineSpinner label={copy.analyzing} />
  }

  if (analyzeQuery.isError) {
    const parsed = parseFinanceError(analyzeQuery.error)

    return <div className="py-1 text-[0.65rem] text-muted-foreground">{parsed.offline ? copy.noData : copy.analyzeError}</div>
  }

  const analyze: FinanceAnalyze = analyzeQuery.data

  return (
    <div className="space-y-3">
      <VerdictRow analyze={analyze} />
      <AgentSignals signals={analyze.signals ?? []} />
      <Citations items={analyze.news ?? []} title={copy.newsTitle} />
      <Citations items={analyze.research ?? []} title={copy.researchTitle} />
      {analyze.note ? <p className="text-[0.6rem] leading-4 text-muted-foreground/70">{analyze.note}</p> : null}
    </div>
  )
}

function VerdictRow({ analyze }: { analyze: FinanceAnalyze }) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const verdict = analyze.verdict

  return (
    <div className="space-y-1.5">
      <FinanceSectionLabel>{copy.verdictTitle}</FinanceSectionLabel>
      {!verdict ? (
        <div className="py-0.5 text-[0.65rem] text-muted-foreground">{copy.signalsEmpty}</div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          <FinancePill variant={directionTone(verdict.direction) === 'good' ? 'default' : 'warn'}>
            <StatusDot tone={directionTone(verdict.direction)} />
            {enumLabel(t.finance.enums.direction, verdict.direction)}
          </FinancePill>
          <span className="text-[0.65rem] tabular-nums text-muted-foreground">
            {copy.signalConfidence(fmtPct(verdict.confidence * 100, 0))}
          </span>
        </div>
      )}
    </div>
  )
}

function AgentSignals({ signals }: { signals: FinanceAnalyzeSignal[] }) {
  const { t } = useI18n()
  const copy = t.finance.watch

  return (
    <div className="space-y-1.5">
      <FinanceSectionLabel>{copy.signalsTitle}</FinanceSectionLabel>
      {signals.length === 0 ? (
        <div className="py-0.5 text-[0.65rem] text-muted-foreground">{copy.signalsEmpty}</div>
      ) : (
        <div className="space-y-1.5">
          {signals.map((signal, index) => (
            <div className="space-y-0.5" key={`${signal.source_agent ?? 'agent'}-${index}`}>
              <div className="flex flex-wrap items-center gap-2">
                <FinancePill variant={directionTone(signal.direction) === 'good' ? 'default' : 'warn'}>
                  {enumLabel(t.finance.enums.direction, signal.direction)}
                </FinancePill>
                {signal.source_agent && <FinancePill variant="outline">{signal.source_agent}</FinancePill>}
                <span className="text-[0.62rem] tabular-nums text-muted-foreground">
                  {copy.signalConfidence(fmtPct(signal.confidence * 100, 0))}
                </span>
              </div>
              {signal.thesis && <p className="text-[0.68rem] leading-4 text-(--ui-text-secondary)">{signal.thesis}</p>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function Citations({ items, title }: { items: FinanceAnalyzeCitation[]; title: string }) {
  const { t } = useI18n()
  const copy = t.finance.watch

  if (items.length === 0) {
    return null
  }

  return (
    <div className="space-y-1">
      <FinanceSectionLabel>{title}</FinanceSectionLabel>
      <ul className="space-y-0.5 text-[0.68rem] leading-5">
        {items.map((item, index) => {
          const href = item.url || item.source_url
          const text = item.title || item.headline || item.label || item.source || item.publisher || href || copy.citationsEmpty

          return (
            <li key={`${text}-${index}`}>
              {href ? (
                <ExternalLink className="font-normal text-muted-foreground" href={href}>
                  {text}
                </ExternalLink>
              ) : (
                <span className="text-muted-foreground">{text}</span>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}
