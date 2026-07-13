import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'

import { StatusDot, type StatusTone } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
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
import { BarChart3, Info, Zap } from '@/lib/icons'
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

const CHART_POLL_MS = 5 * 60_000
const QUOTE_POLL_MS = 60_000

export function WatchModulePanel({ enabled, module }: { enabled: boolean; module: WatchModuleId }) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const symbols = WATCH_MODULE_SYMBOLS[module]

  return (
    <div className="space-y-5">
      <header className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-[0.9375rem] font-semibold tracking-tight text-foreground">{copy.modules[module]}</h3>
          <FinancePill variant="muted">{copy.readOnlyTag}</FinancePill>
        </div>
        <ReadOnlyNote text={copy.readOnlyNote} />
      </header>

      <div className="space-y-4">
        {symbols.map(symbol => (
          <WatchSymbolCard enabled={enabled} key={symbol} symbol={symbol} />
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

function WatchSymbolCard({ enabled, symbol }: { enabled: boolean; symbol: string }) {
  const { t } = useI18n()
  const copy = t.finance.watch
  const [showAnalysis, setShowAnalysis] = useState(false)

  const quoteQuery = useQuery({
    enabled,
    queryFn: () => financeQuote(symbol),
    queryKey: financeKey('watch', 'quote', symbol),
    refetchInterval: QUOTE_POLL_MS,
    // 404 from yfinance is an expected per-symbol state, not worth retrying.
    retry: false
  })

  const barsQuery = useQuery({
    enabled,
    queryFn: () => financeBars(symbol, { limit: 120, timeframe: '1d' }),
    queryKey: financeKey('watch', 'bars', symbol),
    refetchInterval: CHART_POLL_MS,
    retry: false
  })

  const quote = quoteQuery.data
  const bars = barsQuery.data?.bars ?? []
  const label = copy.labels[symbol] ?? symbol
  const quoteFailed = quoteQuery.isError
  const barsFailed = barsQuery.isError

  return (
    <FinanceCard className="space-y-3">
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
              <span className="text-sm font-semibold text-foreground">{fmtPrice(quote?.last)}</span>
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
        <div className="flex items-center gap-1.5 text-[0.6rem] font-medium text-(--ui-text-tertiary)">
          <BarChart3 className="size-3" />
          {copy.chartTitle}
        </div>
        {barsQuery.isPending ? (
          <InlineSpinner />
        ) : barsFailed || bars.length === 0 ? (
          <div className="py-1 text-[0.65rem] text-muted-foreground">{barsFailed ? copy.noData : copy.chartEmpty}</div>
        ) : (
          <CandlestickChart aria={copy.chartAria(symbol)} bars={bars} />
        )}
      </div>

      <div>
        <Button onClick={() => setShowAnalysis(value => !value)} size="xs" variant="outline">
          <Zap className="size-3" />
          {showAnalysis ? copy.hideAnalysis : copy.analyze}
        </Button>
      </div>

      {showAnalysis && <AnalyzePanel enabled={enabled} symbol={symbol} />}
    </FinanceCard>
  )
}

// Dependency-free candlestick chart over daily OHLCV bars. Vertical scale is
// price (shared min-low / max-high); horizontal is evenly spaced sessions.
// Up candles use the primary tone, down candles the destructive tone.
function CandlestickChart({ aria, bars }: { aria: string; bars: FinanceBar[] }) {
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

  return (
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
              width={bodyWidth}
              x={cx - bodyWidth / 2}
              y={bodyTop}
            />
          </g>
        )
      })}
    </svg>
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
    <div className="space-y-3 border-t border-(--ui-stroke-tertiary) pt-3">
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
