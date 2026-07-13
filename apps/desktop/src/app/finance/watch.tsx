import { useQuery } from '@tanstack/react-query'
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
  financeQuote
} from '@/hermes'
import { useI18n } from '@/i18n'
import { ExternalLink } from '@/lib/external-link'
import { BarChart3, Info } from '@/lib/icons'
import { fmtDate } from '@/lib/time'
import { cn } from '@/lib/utils'

import { enumLabel, financeKey, fmtPct, fmtPrice, fmtQty, fmtTs, parseFinanceError } from './lib'
import { FinanceCard, FinancePill, FinanceSectionLabel, InlineSpinner } from './primitives'

// Read-only cross-asset watch modules (Loop.md §3). These read three data-only
// endpoints — quote / bars / analyze — and carry NO order or approval path.
// Some tickers (GC=F, ^TNX, 518880.SS) return 404 from yfinance intermittently,
// so every per-symbol query fails softly to an inline "no data" note.

export const WATCH_MODULE_IDS = ['gold', 'oil', 'rates', 'crypto'] as const

export type WatchModuleId = (typeof WATCH_MODULE_IDS)[number]

// Symbol universe per module (defined ONCE, shared). Display labels are
// resolved from the i18n `watch.labels` map — tickers themselves never change.
export const WATCH_MODULE_SYMBOLS: Record<WatchModuleId, readonly string[]> = {
  gold: ['GC=F', 'GLD', '518880.SS'],
  oil: ['CL=F', 'BZ=F', 'USO'],
  rates: ['^TNX', 'TLT'],
  crypto: ['BTC-USD', 'ETH-USD']
}

const CHART_TIMEFRAME = '1d'
const BARS_LIMIT = 120
// Full bars are refetched SPARINGLY — daily candles barely move intraday, and
// the last candle grows LIVE from the lightweight quote poll instead. Long
// stale/gc windows keep the chart cached so navigating away and back (or
// re-selecting a symbol) renders instantly with no full-screen reload flash.
const BARS_REFETCH_MS = 60_000
const BARS_STALE_MS = 60_000
const BARS_GC_MS = 30 * 60_000
// Lightweight last-price poll that grows the live candle without touching the
// heavy bars endpoint. Data is ~15-min delayed, so ~4s stays backend-friendly.
const QUOTE_POLL_MS = 4_000

// Stable empty reference so the live-candle memo doesn't recompute every render
// while a symbol has no bars yet (react-hooks/exhaustive-deps).
const NO_BARS: FinanceBar[] = []

export function WatchModulePanel({ enabled, module }: { enabled: boolean; module: WatchModuleId }) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const symbols = WATCH_MODULE_SYMBOLS[module]
  const [selected, setSelected] = useState<string>(symbols[0])
  const [showAnalysis, setShowAnalysis] = useState(false)

  // Keep the selection valid when the sidebar switches modules (the symbol set
  // changes); default to the first symbol so the single Analyze always targets
  // something concrete.
  const activeSymbol = symbols.includes(selected) ? selected : symbols[0]

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
        {symbols.map(symbol => (
          <WatchSymbolCard
            active={symbol === activeSymbol}
            enabled={enabled}
            key={symbol}
            onSelect={() => setSelected(symbol)}
            symbol={symbol}
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
  enabled,
  onSelect,
  symbol
}: {
  active: boolean
  enabled: boolean
  onSelect: () => void
  symbol: string
}) {
  const { t } = useI18n()
  const copy = t.finance.watch

  const quoteQuery = useQuery({
    enabled,
    queryFn: () => financeQuote(symbol),
    queryKey: financeKey('watch', 'quote', symbol),
    refetchInterval: QUOTE_POLL_MS,
    // 404 from yfinance is an expected per-symbol state, not worth retrying.
    retry: false
  })

  // Bars are keyed by symbol + timeframe and cached long (stale/gc), so the
  // chart renders instantly from cache on revisit — no fresh full-screen load.
  const barsQuery = useQuery({
    enabled,
    gcTime: BARS_GC_MS,
    queryFn: () => financeBars(symbol, { limit: BARS_LIMIT, timeframe: CHART_TIMEFRAME }),
    queryKey: financeKey('watch', 'bars', symbol, CHART_TIMEFRAME),
    refetchInterval: BARS_REFETCH_MS,
    retry: false,
    staleTime: BARS_STALE_MS
  })

  const quote = quoteQuery.data
  const bars = barsQuery.data?.bars ?? NO_BARS
  const label = copy.labels[symbol] ?? symbol
  const quoteFailed = quoteQuery.isError
  const barsFailed = barsQuery.isError
  const livePrice = !quoteFailed && quote?.last != null && Number.isFinite(quote.last) ? quote.last : null

  // Grow the LAST candle in place from the lightweight quote poll — no bars
  // refetch: move the close to the live price and extend the running high/low.
  const liveBars = useMemo(() => {
    if (bars.length === 0 || livePrice === null) {
      return bars
    }

    const last = bars[bars.length - 1]

    const merged: FinanceBar = {
      ...last,
      close: livePrice,
      high: Math.max(last.high, livePrice),
      low: Math.min(last.low, livePrice)
    }

    return [...bars.slice(0, -1), merged]
  }, [bars, livePrice])

  const isLive = livePrice !== null && !barsFailed && bars.length > 0

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
        if (event.key === 'Enter' || event.key === ' ') {
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
          {quoteQuery.isPending ? (
            <span className="text-xs text-muted-foreground">—</span>
          ) : quoteFailed ? (
            <span className="text-[0.65rem] text-muted-foreground">{copy.quoteError}</span>
          ) : (
            <>
              <span className="text-[0.6rem] font-medium text-(--ui-text-tertiary)">{copy.last}</span>
              <span className="text-sm font-semibold text-foreground transition-colors duration-300">
                {fmtPrice(quote?.last)}
              </span>
            </>
          )}
        </div>
      </div>

      {!quoteFailed && quote && (
        <div className="flex flex-wrap gap-x-4 gap-y-1 text-[0.65rem] tabular-nums text-muted-foreground">
          {quote.bid !== null && (
            <span>
              {copy.bid} <span className="text-foreground">{fmtPrice(quote.bid)}</span>
            </span>
          )}
          {quote.ask !== null && (
            <span>
              {copy.ask} <span className="text-foreground">{fmtPrice(quote.ask)}</span>
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
          {isLive && (
            <span className="inline-flex items-center gap-1 text-[0.55rem] font-semibold uppercase tracking-wide text-primary">
              <span className="size-1.5 animate-pulse rounded-full bg-primary" />
              {copy.liveTag}
            </span>
          )}
        </div>
        {barsQuery.isPending ? (
          <ChartSkeleton label={copy.chartLoading} />
        ) : barsFailed || bars.length === 0 ? (
          <div className="py-1 text-[0.65rem] text-muted-foreground">{barsFailed ? copy.noData : copy.chartEmpty}</div>
        ) : (
          <CandlestickChart aria={copy.chartAria(symbol)} bars={liveBars} />
        )}
      </div>
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

// Dependency-free candlestick chart over daily OHLCV bars. Vertical scale is
// price (shared min-low / max-high); horizontal is evenly spaced sessions. Up
// candles use the primary tone, down candles the destructive tone. A
// TradingView-style crosshair + OHLC tooltip tracks the cursor (values come
// straight from the already-fetched bars — no extra fetch), and the live last
// candle transitions smoothly as the quote poll grows it.
function CandlestickChart({ aria, bars }: { aria: string; bars: FinanceBar[] }) {
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
  const lastIndex = bars.length - 1

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
          const cx = index * slot + slot / 2
          const up = bar.close >= bar.open
          const bodyTop = y(Math.max(bar.open, bar.close))
          const bodyBottom = y(Math.min(bar.open, bar.close))

          // Only the live last candle animates as the quote poll updates it.
          const liveStyle: CSSProperties | undefined =
            index === lastIndex ? { transition: 'y 300ms ease-out, height 300ms ease-out' } : undefined

          return (
            <g className={up ? 'text-primary' : 'text-destructive'} key={`${bar.ts}-${index}`}>
              <line
                stroke="currentColor"
                strokeWidth="0.6"
                vectorEffect="non-scaling-stroke"
                x1={cx}
                x2={cx}
                y1={y(bar.high)}
                y2={y(bar.low)}
              />
              <rect
                fill="currentColor"
                height={Math.max(bodyBottom - bodyTop, 0.4)}
                style={liveStyle}
                width={bodyWidth}
                x={cx - bodyWidth / 2}
                y={bodyTop}
              />
            </g>
          )
        })}
      </svg>

      {hover && hoveredBar && (
        <>
          <div
            className="pointer-events-none absolute inset-y-0 w-px bg-foreground/25"
            style={{ left: `${crosshairLeft}%` }}
          />
          <div className="pointer-events-none absolute inset-x-0 h-px bg-foreground/25" style={{ top: hover.y }} />
          <ChartTooltip bar={hoveredBar} hover={hover} />
        </>
      )}
    </div>
  )
}

// Compact OHLCV readout for the hovered candle. Follows the cursor and flips to
// whichever quadrant keeps it inside the chart. Readable in light + dark via
// the elevated popover surface; disappears with the crosshair on mouse-leave.
function ChartTooltip({ bar, hover }: { bar: FinanceBar; hover: ChartHover }) {
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
        <TooltipCell label={copy.hoverOpen} value={fmtPrice(bar.open)} />
        <TooltipCell label={copy.hoverHigh} value={fmtPrice(bar.high)} />
        <TooltipCell label={copy.hoverLow} value={fmtPrice(bar.low)} />
        <TooltipCell
          label={copy.hoverClose}
          tone={up ? 'text-primary' : 'text-destructive'}
          value={fmtPrice(bar.close)}
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
