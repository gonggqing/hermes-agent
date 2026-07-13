import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { type CSSProperties, type MouseEvent as ReactMouseEvent, useMemo, useRef, useState } from 'react'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
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
import { BarChart3, Info } from '@/lib/icons'
import { fmtDate } from '@/lib/time'
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
// financeBars; the backend supports these intervals. Bars are cached per
// (symbol, timeframe) so flipping back to a previously-viewed preset is
// instant — no full-screen reload.
type TimeframeId = 'day' | 'intraday1d' | 'intraday5d' | 'month' | 'week'

const TIMEFRAME_PRESETS: Record<TimeframeId, { limit: number; timeframe: string }> = {
  intraday1d: { limit: 78, timeframe: '5m' }, // one intraday session
  intraday5d: { limit: 70, timeframe: '30m' },
  day: { limit: 120, timeframe: '1d' }, // DEFAULT
  week: { limit: 104, timeframe: '1wk' },
  month: { limit: 60, timeframe: '1mo' }
}

// Rendered left-to-right order of the segmented switcher (intraday → longer).
const TIMEFRAME_ORDER: TimeframeId[] = ['intraday1d', 'intraday5d', 'day', 'week', 'month']

const DEFAULT_TIMEFRAME: TimeframeId = 'day'

// Long stale/gc windows keep each (symbol, timeframe) chart cached so navigating
// away and back — or re-selecting a preset — renders instantly from cache.
const BARS_STALE_MS = 60_000
const BARS_GC_MS = 30 * 60_000
// The quote header (Last/Bid/Ask/Volume) is fetched once and kept warm. The old
// ~4s live-price poll (and the LIVE last-candle it grew) has been removed — the
// chart is now cached candlesticks + MA20/30 + the timeframe switch + hover.
const QUOTE_STALE_MS = 60_000

// MA20 / MA30 overlay accents — vivid mid-tones that read over the green/red
// candles in both light and dark themes.
const MA20_COLOR = '#f59e0b'
const MA30_COLOR = '#3b82f6'

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

      <div className="space-y-4">
        {symbols.map(config => (
          <WatchSymbolCard
            active={config.symbol === activeSymbol}
            config={config}
            enabled={enabled}
            key={config.symbol}
            onSelect={() => setSelected(config.symbol)}
          />
        ))}
      </div>

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

function WatchSymbolCard({
  active,
  config,
  enabled,
  onSelect
}: {
  active: boolean
  config: WatchSymbolConfig
  enabled: boolean
  onSelect: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const [timeframe, setTimeframe] = useState<TimeframeId>(DEFAULT_TIMEFRAME)
  const preset = TIMEFRAME_PRESETS[timeframe]

  const { currency, derived, symbol, unit } = config
  const unitWord = unit ? copy.units[unit] : null
  // A derived symbol (AU9999) has no Yahoo endpoint of its own — its price +
  // candles are fetched from the BASE future and rescaled by the FX quote. A
  // bare symbol fetches itself. Fetching the base under its own cache key means
  // the derived card and the base's own card share one request.
  const dataSymbol = derived ? derived.base : symbol

  const quoteQuery = useQuery({
    enabled,
    queryFn: () => financeQuote(dataSymbol),
    queryKey: financeKey('watch', 'quote', dataSymbol),
    // 404 from yfinance is an expected per-symbol state, not worth retrying.
    retry: false,
    staleTime: QUOTE_STALE_MS
  })

  // Bars are keyed by symbol + timeframe and cached long (stale/gc), so a
  // revisit — or flipping back to a previously-viewed preset — renders instantly
  // from cache. keepPreviousData holds the current candles while a new preset
  // loads, so switching timeframe never flashes a full-screen skeleton.
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

  const factor =
    derived && fxLast !== null && Number.isFinite(fxLast) ? fxLast / derived.gramsPerOunce : null

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

  const label = copy.labels[symbol] ?? symbol

  // A derived symbol is broken when EITHER input quote errors or yields no
  // usable value — surfaced as a distinct no-data note (never a crash).
  const derivedBroken =
    Boolean(derived) &&
    (quoteQuery.isError ||
      fxQuery.isError ||
      (fxQuery.isSuccess && (fxLast === null || !Number.isFinite(fxLast))) ||
      (quoteQuery.isSuccess && (baseQuote?.last === null || baseQuote?.last === undefined)))

  const quotePending = quoteQuery.isPending || (Boolean(derived) && fxQuery.isPending)
  const barsPending = barsQuery.isPending || (Boolean(derived) && fxQuery.isPending)
  const quoteFailed = derived ? derivedBroken : quoteQuery.isError
  const barsFailed = derived ? barsQuery.isError || derivedBroken : barsQuery.isError

  return (
    <div
      aria-pressed={active}
      className={cn(
        'w-full space-y-3 rounded-lg border bg-(--ui-bg-quinary) p-3 text-left transition-colors',
        active
          ? 'border-primary/40 ring-1 ring-inset ring-primary/25'
          : 'cursor-pointer border-(--ui-stroke-tertiary) hover:border-(--ui-stroke-secondary)'
      )}
      onClick={onSelect}
      onKeyDown={event => {
        // Only select on keys aimed at the card itself — not at the nested
        // timeframe buttons, whose Enter/Space belong to them.
        if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault()
          onSelect()
        }
      }}
      role="button"
      tabIndex={0}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="flex min-w-0 items-baseline gap-2">
          <span className="text-sm font-semibold tracking-tight text-foreground">{symbol}</span>
          <span className="truncate text-[0.68rem] text-muted-foreground">{label}</span>
        </div>
        <div className="flex items-baseline gap-2 tabular-nums">
          {quotePending ? (
            <span className="text-xs text-muted-foreground">—</span>
          ) : quoteFailed ? (
            <span className="text-[0.65rem] text-muted-foreground">{derived ? copy.derivedNoData : copy.quoteError}</span>
          ) : (
            <>
              <span className="text-[0.6rem] font-medium text-(--ui-text-tertiary)">{copy.last}</span>
              <span className="text-sm font-semibold text-foreground transition-colors duration-300">
                {fmtWatchPrice(quote?.last, currency, unit, unitWord)}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Provenance for the DERIVED symbol (AU9999 = intl gold × USD/CNY). */}
      {derived && <p className="text-[0.6rem] leading-4 text-muted-foreground/70">{copy.derivedNote}</p>}

      {!quoteFailed && quote && (
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
          {quote.as_of && <span className="text-muted-foreground/70">{copy.quoteAsOf(fmtTs(quote.as_of))}</span>}
        </div>
      )}

      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-1.5 text-[0.6rem] font-medium text-(--ui-text-tertiary)">
            <BarChart3 className="size-3" />
            {copy.chartTitle}
          </div>
          <TimeframeSwitcher onChange={setTimeframe} value={timeframe} />
        </div>
        {barsPending ? (
          <ChartSkeleton label={copy.chartLoading} />
        ) : barsFailed || bars.length === 0 ? (
          <div className="py-1 text-[0.65rem] text-muted-foreground">
            {derived && derivedBroken ? copy.derivedNoData : barsFailed ? copy.noData : copy.chartEmpty}
          </div>
        ) : (
          <CandlestickChart
            aria={copy.chartAria(symbol)}
            bars={bars}
            currency={currency}
            unit={unit}
            unitWord={unitWord}
          />
        )}
      </div>
    </div>
  )
}

// Compact segmented timeframe presets over the chart. Selecting a preset swaps
// the (timeframe, limit) fed to financeBars and re-renders; bars are cached per
// (symbol, timeframe) so re-selecting a previously-viewed preset is instant.
function TimeframeSwitcher({ onChange, value }: { onChange: (id: TimeframeId) => void; value: TimeframeId }) {
  const { t } = useI18n()
  const copy = t.finance.watch

  return (
    <div
      aria-label={copy.timeframeAria}
      className="inline-flex items-center gap-0.5 rounded-md bg-(--ui-bg-tertiary) p-0.5"
      role="group"
    >
      {TIMEFRAME_ORDER.map(id => {
        const selected = id === value

        return (
          <button
            aria-pressed={selected}
            className={cn(
              'rounded px-1.5 py-0.5 text-[0.6rem] font-medium tabular-nums transition-colors',
              selected
                ? 'bg-(--ui-bg-elevated) text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            )}
            key={id}
            // Isolate the preset click from the card's onSelect so switching a
            // chart's timeframe never re-selects the whole symbol row.
            onClick={event => {
              event.stopPropagation()
              onChange(id)
            }}
            type="button"
          >
            {copy.timeframes[id]}
          </button>
        )
      })}
    </div>
  )
}

// First-load placeholder for the chart. Reuses the app's shared Skeleton
// (animate-pulse) so the initial fetch reads as a calm shimmer that matches
// Hermes — never a spinner-then-flash. Cache hits skip this entirely.
function ChartSkeleton({ label }: { label: string }) {
  return (
    <div aria-label={label} className="py-0.5" role="status">
      <Skeleton className="h-24 w-full rounded-md" />
    </div>
  )
}

// One hovered session, in container-local pixels, captured on mouse-move so the
// crosshair + tooltip can be laid out over the (aspect-distorted) SVG.
interface ChartHover {
  height: number
  index: number
  width: number
  x: number
  y: number
}

// Simple moving average of the last `period` closes at each bar; null until
// enough bars exist, so the overlay line only starts once its window fills.
function simpleMovingAverage(bars: FinanceBar[], period: number): (null | number)[] {
  const out: (null | number)[] = new Array(bars.length).fill(null)
  let sum = 0

  for (let index = 0; index < bars.length; index++) {
    sum += bars[index].close

    if (index >= period) {
      sum -= bars[index - period].close
    }

    if (index >= period - 1) {
      out[index] = sum / period
    }
  }

  return out
}

// Dependency-free candlestick chart over OHLCV bars for the selected timeframe.
// Vertical scale is price (shared min-low / max-high); horizontal is evenly
// spaced sessions. Up candles use the primary tone, down candles the
// destructive tone. MA20 / MA30 overlays (均线) — computed from the fetched
// closes, no extra fetch — ride the SAME coordinate space in two distinct,
// theme-safe colors, with a small legend. A TradingView-style crosshair + OHLC
// tooltip tracks the cursor (values straight from the already-fetched bars).
function CandlestickChart({
  aria,
  bars,
  currency,
  unit,
  unitWord
}: {
  aria: string
  bars: FinanceBar[]
  currency: WatchCurrency | null
  unit: WatchUnitKey | null
  unitWord: null | string
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const containerRef = useRef<HTMLDivElement>(null)
  const [hover, setHover] = useState<ChartHover | null>(null)

  const width = 100
  const height = 40
  const lows = bars.map(bar => bar.low)
  const highs = bars.map(bar => bar.high)
  const min = Math.min(...lows)
  const max = Math.max(...highs)
  const span = max - min || 1
  const slot = width / bars.length
  const bodyWidth = Math.max(slot * 0.6, 0.4)
  const y = (value: number) => height - ((value - min) / span) * height
  const cx = (index: number) => index * slot + slot / 2

  // MA polylines in the same coordinate space as the candles. Each MA close is
  // between its bar's low and high, so every point stays on the shared scale.
  const maPolyline = (period: number) =>
    simpleMovingAverage(bars, period)
      .map((value, index) => (value === null ? null : `${cx(index)},${y(value)}`))
      .filter((point): point is string => point !== null)
      .join(' ')

  const ma20Points = maPolyline(20)
  const ma30Points = maPolyline(30)

  const handleMove = (event: ReactMouseEvent<HTMLDivElement>) => {
    const el = containerRef.current

    if (!el) {
      return
    }

    const rect = el.getBoundingClientRect()
    const localX = event.clientX - rect.left
    const index = Math.min(bars.length - 1, Math.max(0, Math.floor((localX / rect.width) * bars.length)))

    setHover({ height: rect.height, index, width: rect.width, x: localX, y: event.clientY - rect.top })
  }

  const hoveredBar = hover ? bars[hover.index] : null
  // Snap the vertical guide to the hovered session's center (fraction of width).
  const crosshairLeft = hover ? ((hover.index + 0.5) / bars.length) * 100 : 0

  return (
    <div
      className="relative select-none"
      onMouseLeave={() => setHover(null)}
      onMouseMove={handleMove}
      ref={containerRef}
    >
      <svg
        aria-label={aria}
        className="h-24 w-full"
        preserveAspectRatio="none"
        role="img"
        viewBox={`0 0 ${width} ${height}`}
      >
        {bars.map((bar, index) => {
          const barCx = cx(index)
          const up = bar.close >= bar.open
          const bodyTop = y(Math.max(bar.open, bar.close))
          const bodyBottom = y(Math.min(bar.open, bar.close))

          return (
            <g className={up ? 'text-primary' : 'text-destructive'} key={`${bar.ts}-${index}`}>
              <line
                stroke="currentColor"
                strokeWidth="0.6"
                vectorEffect="non-scaling-stroke"
                x1={barCx}
                x2={barCx}
                y1={y(bar.high)}
                y2={y(bar.low)}
              />
              <rect
                fill="currentColor"
                height={Math.max(bodyBottom - bodyTop, 0.4)}
                width={bodyWidth}
                x={barCx - bodyWidth / 2}
                y={bodyTop}
              />
            </g>
          )
        })}

        {/* MA overlays drawn ON TOP of the candles so they read clearly. */}
        {ma20Points && (
          <polyline
            fill="none"
            points={ma20Points}
            stroke={MA20_COLOR}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.1"
            vectorEffect="non-scaling-stroke"
          />
        )}
        {ma30Points && (
          <polyline
            fill="none"
            points={ma30Points}
            stroke={MA30_COLOR}
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth="1.1"
            vectorEffect="non-scaling-stroke"
          />
        )}
      </svg>

      <div className="pointer-events-none absolute left-1 top-0.5 flex items-center gap-2 text-[0.5rem] font-semibold leading-none tabular-nums">
        <MaLegendItem color={MA20_COLOR} label={copy.ma20} />
        <MaLegendItem color={MA30_COLOR} label={copy.ma30} />
      </div>

      {hover && hoveredBar && (
        <>
          <div
            className="pointer-events-none absolute inset-y-0 w-px bg-foreground/25"
            style={{ left: `${crosshairLeft}%` }}
          />
          <div className="pointer-events-none absolute inset-x-0 h-px bg-foreground/25" style={{ top: hover.y }} />
          <ChartTooltip bar={hoveredBar} currency={currency} hover={hover} unit={unit} unitWord={unitWord} />
        </>
      )}
    </div>
  )
}

// One MA legend entry: a short colored rule + its label, painted in the line's
// own color so the pairing reads at a glance over the candles.
function MaLegendItem({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1" style={{ color }}>
      <span className="inline-block h-0.5 w-2.5 rounded-full" style={{ backgroundColor: color }} />
      {label}
    </span>
  )
}

// Compact OHLCV readout for the hovered candle. Follows the cursor and flips to
// whichever quadrant keeps it inside the chart. Readable in light + dark via
// the elevated popover surface; disappears with the crosshair on mouse-leave.
function ChartTooltip({
  bar,
  currency,
  hover,
  unit,
  unitWord
}: {
  bar: FinanceBar
  currency: WatchCurrency | null
  hover: ChartHover
  unit: WatchUnitKey | null
  unitWord: null | string
}) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const up = bar.close >= bar.open
  const parsed = new Date(bar.ts)
  const dateLabel = Number.isNaN(parsed.valueOf()) ? bar.ts : fmtDate.format(parsed)
  const flipX = hover.x > hover.width / 2
  const flipY = hover.y > hover.height / 2

  const style: CSSProperties = {
    ...(flipX ? { right: hover.width - hover.x + 12 } : { left: hover.x + 12 }),
    ...(flipY ? { bottom: hover.height - hover.y + 12 } : { top: hover.y + 12 })
  }

  return (
    <div
      className="pointer-events-none absolute z-10 min-w-max rounded-md border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) px-2 py-1.5 text-[0.6rem] leading-4 shadow-md"
      style={style}
    >
      <div className="mb-1 font-medium text-foreground">{dateLabel}</div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 tabular-nums">
        <TooltipCell label={copy.hoverOpen} value={fmtWatchValue(bar.open, currency, unit, unitWord)} />
        <TooltipCell label={copy.hoverHigh} value={fmtWatchValue(bar.high, currency, unit, unitWord)} />
        <TooltipCell label={copy.hoverLow} value={fmtWatchValue(bar.low, currency, unit, unitWord)} />
        <TooltipCell
          label={copy.hoverClose}
          tone={up ? 'text-primary' : 'text-destructive'}
          value={fmtWatchValue(bar.close, currency, unit, unitWord)}
        />
      </div>
      {bar.volume > 0 && (
        <div className="mt-1 flex justify-between gap-3 tabular-nums text-muted-foreground">
          <span>{copy.hoverVolume}</span>
          <span className="text-foreground">{fmtQty(bar.volume)}</span>
        </div>
      )}
    </div>
  )
}

function TooltipCell({ label, tone, value }: { label: string; tone?: string; value: string }) {
  return (
    <span className="flex justify-between gap-2">
      <span className="text-muted-foreground">{label}</span>
      <span className={cn('font-medium text-foreground', tone)}>{value}</span>
    </span>
  )
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
