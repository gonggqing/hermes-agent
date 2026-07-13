import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import { BarChart3, Eye } from "lucide-react";
import { api } from "@/lib/api";
import type { FinanceAnalyze, FinanceBar, FinanceQuote } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { cn } from "@/lib/utils";
import type { FinanceTranslations } from "@/i18n/types";
import { useFinanceT } from "./i18n";
import {
  directionTone,
  fmtTs,
  fmtWatchPrice,
  type WatchPriceDisplay,
} from "./format";
import {
  WATCH_MODULES,
  watchModuleName,
  type WatchModuleKey,
  type WatchSymbol,
} from "./constants";

// ── Per-(symbol,timeframe) bars cache + timeframe presets ─────────────
//
// A module-level cache keyed by symbol+timeframe+limit. It persists across desk
// switches AND timeframe flips (the components unmount, the Map does not), so
// navigating away and back — or flipping back to a previously-viewed preset —
// renders the K-line instantly with NO refetch/loading flash. There is NO
// live-price poll: the chart is cached candlesticks + MA overlays + the
// timeframe switch + the hover crosshair/tooltip (Loop.md §3: READ-ONLY).

/** MA overlay colors — distinct from the red/green candles, and (with a
 * background-colored casing behind each line) legible in light + dark. */
const MA20_COLOR = "#f59e0b"; // amber
const MA30_COLOR = "#a855f7"; // violet

/** Compact chart timeframe presets. Each maps to a (timeframe, limit) passed
 * to the financeBars client. Default = Day. Labels are localized. */
type TimeframePresetKey = "intraday" | "fiveDay" | "day" | "week" | "month";

const TIMEFRAME_CONFIG: Record<
  TimeframePresetKey,
  { timeframe: string; limit: number }
> = {
  intraday: { timeframe: "5m", limit: 78 }, // one intraday session
  fiveDay: { timeframe: "30m", limit: 70 },
  day: { timeframe: "1d", limit: 120 }, // DEFAULT
  week: { timeframe: "1wk", limit: 104 },
  month: { timeframe: "1mo", limit: 60 },
};

const TIMEFRAME_ORDER: TimeframePresetKey[] = [
  "intraday",
  "fiveDay",
  "day",
  "week",
  "month",
];

const DEFAULT_PRESET: TimeframePresetKey = "day";

/** Localized segment label for a timeframe preset. */
function timeframeLabel(
  key: TimeframePresetKey,
  ft: FinanceTranslations,
): string {
  switch (key) {
    case "intraday":
      return ft.watch.timeframe.intraday;
    case "fiveDay":
      return ft.watch.timeframe.fiveDay;
    case "day":
      return ft.watch.timeframe.day;
    case "week":
      return ft.watch.timeframe.week;
    case "month":
      return ft.watch.timeframe.month;
  }
}

interface BarsCacheEntry {
  bars: FinanceBar[];
  fetchedAt: number;
}
const barsCache = new Map<string, BarsCacheEntry>();
const quoteCache = new Map<string, FinanceQuote>();

function barsKey(symbol: string, timeframe: string, limit: number): string {
  return `${symbol}|${timeframe}|${limit}`;
}

/**
 * Simple moving average of the last `period` closes at each bar. Returns one
 * value per bar; `null` for the leading bars where fewer than `period` closes
 * exist (the overlay line starts once enough bars are available). Computed in
 * the frontend from the already-fetched bars — no extra fetch.
 */
function sma(bars: FinanceBar[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(bars.length).fill(null);
  let sum = 0;
  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) sum -= bars[i - period].close;
    if (i >= period - 1) out[i] = sum / period;
  }
  return out;
}

// ── Per-symbol data hook (cache-first, per-timeframe bars) ────────────

type SymbolStatus = "loading" | "error" | "ready";

interface WatchSymbolData {
  status: SymbolStatus;
  quote: FinanceQuote | null;
  bars: FinanceBar[];
  /** True once the bars fetch for the current timeframe has settled. */
  barsDone: boolean;
}

/**
 * Cache-first quote for one symbol (timeframe-independent). Seeded from the
 * module cache; only fetches on a miss. Pass `symbol = null` to opt out (used
 * by non-derived symbols for the unused FX slot) — it settles done with a null
 * quote and never fetches. All setState happens in the async callback, so one
 * 404 (common for GC=F/^TNX/518880.SS) never blanks a sibling.
 */
function useSymbolQuote(symbol: string | null): {
  quote: FinanceQuote | null;
  done: boolean;
} {
  const [quote, setQuote] = useState<FinanceQuote | null>(() =>
    symbol ? (quoteCache.get(symbol) ?? null) : null,
  );
  const [done, setDone] = useState<boolean>(
    () => symbol === null || quoteCache.has(symbol),
  );
  // Re-sync synchronously if the symbol identity changes (e.g. a derived
  // entry's FX slot toggling in/out) — React's "adjust state on prop change".
  const [renderedSymbol, setRenderedSymbol] = useState(symbol);
  if (renderedSymbol !== symbol) {
    setRenderedSymbol(symbol);
    setQuote(symbol ? (quoteCache.get(symbol) ?? null) : null);
    setDone(symbol === null || quoteCache.has(symbol));
  }

  useEffect(() => {
    if (symbol === null || quoteCache.has(symbol)) return;
    let alive = true;
    api.financeQuote(symbol).then(
      (q) => {
        if (!alive) return;
        quoteCache.set(symbol, q);
        setQuote(q);
        setDone(true);
      },
      () => {
        if (alive) setDone(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [symbol]);

  return { quote, done };
}

/**
 * Cache-first OHLCV bars for one (symbol, timeframe). Seeded from cache and
 * re-synced synchronously when the preset flips so we never flash the prior
 * timeframe's candles. Only fetches on a miss, so flipping back to a
 * previously-viewed preset is instant (Loop.md §3: READ-ONLY, no live poll).
 */
function useSymbolBars(
  symbol: string,
  preset: TimeframePresetKey,
): { bars: FinanceBar[]; done: boolean } {
  const { timeframe, limit } = TIMEFRAME_CONFIG[preset];
  const key = barsKey(symbol, timeframe, limit);

  const [bars, setBars] = useState<FinanceBar[]>(
    () => barsCache.get(key)?.bars ?? [],
  );
  const [done, setDone] = useState<boolean>(() => barsCache.has(key));
  const [renderedKey, setRenderedKey] = useState(key);
  if (renderedKey !== key) {
    setRenderedKey(key);
    setBars(barsCache.get(key)?.bars ?? []);
    setDone(barsCache.has(key));
  }

  useEffect(() => {
    if (barsCache.has(key)) return;
    let alive = true;
    api.financeBars(symbol, { timeframe, limit }).then(
      (res) => {
        if (!alive) return;
        barsCache.set(key, { bars: res.bars, fetchedAt: Date.now() });
        setBars(res.bars);
        setDone(true);
      },
      () => {
        if (!alive) return;
        setBars([]);
        setDone(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [key, symbol, timeframe, limit]);

  return { bars, done };
}

/**
 * Cache-first data for one watch symbol at one timeframe preset.
 *
 * For a plain symbol this is just its quote + bars. For a DERIVED symbol
 * (e.g. AU9999, which is not on Yahoo) it fetches the base price series
 * (`derived.base`, e.g. GC=F in USD/oz), the base bars, and the FX quote
 * (`derived.fx`, e.g. CNY=X in CNY/USD), then rescales the base quote's last
 * AND every base bar's O/H/L/C by `factor = fxLast / gramsPerOunce` — the
 * candle SHAPE is the base series, just rescaled to ¥/gram. A missing base or
 * FX quote degrades to "error" (a graceful no-data note) — it never crashes.
 * All three symbols share the same module caches, so GC=F/CNY=X are fetched
 * once even though the Gold module shows both GC=F and AU9999.
 */
function useWatchSymbol(
  entry: WatchSymbol,
  preset: TimeframePresetKey,
): WatchSymbolData {
  const derived = entry.derived ?? null;
  // Hooks must run unconditionally: the "primary" series is the base symbol
  // for a derived entry, otherwise the entry's own symbol. The FX slot is
  // null (a no-op) for non-derived entries.
  const primarySymbol = derived ? derived.base : entry.symbol;
  const { quote: primaryQuote, done: quoteDone } = useSymbolQuote(primarySymbol);
  const { bars: primaryBars, done: barsDone } = useSymbolBars(
    primarySymbol,
    preset,
  );
  const { quote: fxQuote, done: fxDone } = useSymbolQuote(
    derived ? derived.fx : null,
  );

  const { quote, bars } = useMemo<{
    quote: FinanceQuote | null;
    bars: FinanceBar[];
  }>(() => {
    if (!derived) return { quote: primaryQuote, bars: primaryBars };
    const fxLast = fxQuote?.last ?? null;
    // Cannot derive without the FX rate — degrade to no-data (never crash).
    if (fxLast === null) return { quote: null, bars: [] };
    const factor = fxLast / derived.gramsPerOunce;
    const scale = (v: number | null): number | null =>
      v === null ? null : v * factor;
    const dq: FinanceQuote | null = primaryQuote
      ? {
          ...primaryQuote,
          symbol: entry.symbol,
          last: scale(primaryQuote.last),
          bid: scale(primaryQuote.bid),
          ask: scale(primaryQuote.ask),
        }
      : null;
    const db: FinanceBar[] = primaryBars.map((b) => ({
      ts: b.ts,
      open: b.open * factor,
      high: b.high * factor,
      low: b.low * factor,
      close: b.close * factor,
      volume: b.volume,
    }));
    return { quote: dq, bars: db };
  }, [derived, primaryQuote, primaryBars, fxQuote, entry.symbol]);

  // A derived entry only settles once its FX quote has settled too.
  const settledQuote = derived ? quoteDone && fxDone : quoteDone;
  const settledBars = derived ? barsDone && fxDone : barsDone;
  const status: SymbolStatus =
    quote === null && bars.length === 0
      ? settledQuote && settledBars
        ? "error"
        : "loading"
      : "ready";

  return { status, quote, bars, barsDone: settledBars };
}

// ── Inline SVG candlestick chart with TradingView-style crosshair ─────

interface HoverState {
  index: number;
  xPct: number; // hovered bar-center, 0..1 of the chart width
  xPx: number;
  yPx: number;
  width: number;
  height: number;
}

/** Crosshair tooltip: date + O/H/L/C (+ volume). Follows the cursor and flips
 * side near the right edge; readable in light and dark. */
function CrosshairTooltip({
  bar,
  hover,
  ft,
  formatValue,
}: {
  bar: FinanceBar;
  hover: HoverState;
  ft: FinanceTranslations;
  /** Compact per-symbol price formatter (currency, no unit suffix). */
  formatValue: (v: number) => string;
}) {
  const onRight = hover.xPx < hover.width / 2;
  const style: CSSProperties = {
    top: Math.max(0, Math.min(hover.yPx + 8, hover.height - 4)),
    ...(onRight
      ? { left: hover.xPx + 12 }
      : { right: hover.width - hover.xPx + 12 }),
  };
  const up = bar.close >= bar.open;
  const row = (label: string, value: string, valueClass?: string) => (
    <>
      <span className="text-text-tertiary">{label}</span>
      <span className={cn("text-right text-foreground", valueClass)}>
        {value}
      </span>
    </>
  );
  return (
    <div
      className="pointer-events-none absolute z-10 min-w-[8.5rem] border border-border bg-background/95 px-2 py-1.5 font-mondwest normal-case text-xs shadow-md backdrop-blur-sm"
      style={style}
    >
      <div className="mb-1 text-text-tertiary">{fmtTs(bar.ts)}</div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5">
        {row(ft.watch.open, formatValue(bar.open))}
        {row(ft.watch.high, formatValue(bar.high))}
        {row(ft.watch.low, formatValue(bar.low))}
        {row(
          ft.watch.close,
          formatValue(bar.close),
          up ? "text-success" : "text-destructive",
        )}
      </div>
      {bar.volume > 0 && (
        <div className="mt-1 flex justify-between gap-3">
          <span className="text-text-tertiary">{ft.watch.volume}</span>
          <span className="text-foreground">
            {bar.volume.toLocaleString()}
          </span>
        </div>
      )}
    </div>
  );
}

/**
 * Candlestick chart drawn as inline SVG from OHLCV bars (no charting
 * dependency, Loop.md §3). `preserveAspectRatio="none"` lets it fill the card
 * width; wicks use a non-scaling stroke so they stay crisp. Up candles tint
 * success, down candles destructive. MA20/MA30 are overlaid as smooth
 * polylines in the same coordinate space. On mouse-move it renders a crosshair
 * (vertical + horizontal guide) and a compact OHLC(V) tooltip sourced from the
 * already-fetched bars — no extra fetch.
 */
function CandlestickChart({
  bars,
  ma20,
  ma30,
  ariaLabel,
  ft,
  formatValue,
}: {
  bars: FinanceBar[];
  ma20: (number | null)[];
  ma30: (number | null)[];
  ariaLabel: string;
  ft: FinanceTranslations;
  /** Compact per-symbol price formatter for the axis labels + hover tooltip. */
  formatValue: (v: number) => string;
}) {
  const slot = 6; // viewBox units per candle
  const bodyW = 4;
  const H = 100;
  const PAD = 4;
  const W = Math.max(bars.length * slot, slot);
  const highs = bars.map((b) => b.high);
  const lows = bars.map((b) => b.low);
  const max = Math.max(...highs);
  const min = Math.min(...lows);
  const range = max - min || 1;
  const y = (v: number) => PAD + (1 - (v - min) / range) * (H - PAD * 2);
  const cx = (i: number) => i * slot + slot / 2;

  // MA overlays share the candle coordinate space; the polyline skips the
  // leading bars where the SMA is still undefined (fewer than N closes).
  const maPoints = (values: (number | null)[]): string =>
    values
      .map((v, i) => (v === null ? null : `${cx(i)},${y(v)}`))
      .filter((p): p is string => p !== null)
      .join(" ");
  const ma20Points = maPoints(ma20);
  const ma30Points = maPoints(ma30);

  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<HoverState | null>(null);
  const hoveredBar = hover ? (bars[hover.index] ?? null) : null;

  const onMove = (e: React.MouseEvent<HTMLDivElement>) => {
    const el = wrapRef.current;
    if (!el || bars.length === 0) return;
    const rect = el.getBoundingClientRect();
    const relX = e.clientX - rect.left;
    const relY = e.clientY - rect.top;
    // Bars occupy equal horizontal space, so pixel-x maps linearly to index.
    let index = Math.floor((relX / rect.width) * bars.length);
    index = Math.max(0, Math.min(bars.length - 1, index));
    setHover({
      index,
      xPct: (index + 0.5) / bars.length,
      xPx: relX,
      yPx: relY,
      width: rect.width,
      height: rect.height,
    });
  };

  return (
    <div
      ref={wrapRef}
      className="relative h-32 w-full cursor-crosshair select-none"
      onMouseMove={onMove}
      onMouseLeave={() => setHover(null)}
    >
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-full w-full"
        role="img"
        aria-label={ariaLabel}
      >
        {bars.map((b, i) => {
          const x = cx(i);
          const up = b.close >= b.open;
          const bodyTop = y(Math.max(b.open, b.close));
          const bodyBottom = y(Math.min(b.open, b.close));
          const bodyH = Math.max(bodyBottom - bodyTop, 0.75);
          const dim = hover !== null && hover.index !== i;
          return (
            <g
              key={`${b.ts}-${i}`}
              className={up ? "text-success" : "text-destructive"}
              opacity={dim ? 0.5 : 1}
            >
              <line
                x1={x}
                x2={x}
                y1={y(b.high)}
                y2={y(b.low)}
                stroke="currentColor"
                strokeWidth="1"
                vectorEffect="non-scaling-stroke"
              />
              <rect
                x={x - bodyW / 2}
                y={bodyTop}
                width={bodyW}
                height={bodyH}
                fill="currentColor"
              />
            </g>
          );
        })}

        {/* Moving-average overlays: drawn over the candles (under the HTML
            crosshair). Each line carries a background-colored casing so it
            stays legible over red/green candles in light + dark. */}
        {ma20Points && (
          <g fill="none" strokeLinejoin="round" strokeLinecap="round">
            <polyline
              points={ma20Points}
              className="text-background"
              stroke="currentColor"
              strokeWidth={3}
              strokeOpacity={0.55}
              vectorEffect="non-scaling-stroke"
            />
            <polyline
              points={ma20Points}
              stroke={MA20_COLOR}
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
            />
          </g>
        )}
        {ma30Points && (
          <g fill="none" strokeLinejoin="round" strokeLinecap="round">
            <polyline
              points={ma30Points}
              className="text-background"
              stroke="currentColor"
              strokeWidth={3}
              strokeOpacity={0.55}
              vectorEffect="non-scaling-stroke"
            />
            <polyline
              points={ma30Points}
              stroke={MA30_COLOR}
              strokeWidth={1.5}
              vectorEffect="non-scaling-stroke"
            />
          </g>
        )}
      </svg>

      {/* Price (y) axis: high at the top, low at the bottom — currency-aware
          so the candle scale is legible without hovering. */}
      <div className="pointer-events-none absolute right-1 top-0.5 font-mondwest normal-case text-[0.625rem] leading-none text-text-tertiary">
        {formatValue(max)}
      </div>
      <div className="pointer-events-none absolute bottom-0.5 right-1 font-mondwest normal-case text-[0.625rem] leading-none text-text-tertiary">
        {formatValue(min)}
      </div>

      {/* Crosshair guides. */}
      {hover && (
        <>
          <div
            className="pointer-events-none absolute inset-y-0 w-px bg-foreground/30"
            style={{ left: `${hover.xPct * 100}%` }}
          />
          <div
            className="pointer-events-none absolute inset-x-0 h-px bg-foreground/30"
            style={{ top: hover.yPx }}
          />
        </>
      )}

      {/* Crosshair tooltip. */}
      {hover && hoveredBar && (
        <CrosshairTooltip
          bar={hoveredBar}
          hover={hover}
          ft={ft}
          formatValue={formatValue}
        />
      )}
    </div>
  );
}

// ── First-load skeleton (calm shimmer, no spinner-then-flash) ─────────

const SKELETON_BARS = [42, 60, 48, 72, 55, 80, 46, 66, 52, 76, 50, 64, 58, 70];

function ChartSkeleton({ label }: { label: string }) {
  return (
    <div
      className="flex h-32 w-full items-end gap-1"
      role="img"
      aria-label={label}
    >
      {SKELETON_BARS.map((h, i) => (
        <div
          key={i}
          className="flex-1 animate-pulse rounded-sm bg-secondary/50"
          style={{ height: `${h}%`, animationDelay: `${i * 60}ms` }}
        />
      ))}
    </div>
  );
}

function WatchSymbolSkeleton({ label }: { label: string }) {
  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex items-center gap-2">
          <div className="h-4 w-16 animate-pulse rounded bg-secondary/60" />
          <div className="h-3 w-24 animate-pulse rounded bg-secondary/40" />
          <div className="ml-auto h-4 w-20 animate-pulse rounded bg-secondary/60" />
        </div>
        <ChartSkeleton label={label} />
      </CardContent>
    </Card>
  );
}

// ── Analyze panel (verdict + per-agent signals + cited sources) ────────

type AnalyzeState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error" }
  | { status: "done"; data: FinanceAnalyze };

function AnalyzePanel({
  data,
  symbol,
  ft,
}: {
  data: FinanceAnalyze;
  symbol: string;
  ft: FinanceTranslations;
}) {
  const v = data.verdict;
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono-ui text-sm font-semibold text-foreground">
          {ft.watch.analysisFor.replace("{symbol}", symbol)}
        </span>
      </div>

      {/* Verdict */}
      <div className="flex flex-col gap-1">
        <span className="text-xs uppercase text-text-tertiary">
          {ft.watch.verdict}
        </span>
        {v === null ? (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.watch.noVerdict}
          </p>
        ) : (
          <div className="flex flex-col gap-1">
            <div className="flex flex-wrap items-center gap-2">
              <Badge tone={directionTone(v.direction)}>{v.direction}</Badge>
              <span className="font-mondwest normal-case text-xs text-muted-foreground">
                {ft.watch.confidence.replace(
                  "{pct}",
                  (v.confidence * 100).toFixed(0),
                )}
              </span>
            </div>
            {v.thesis && (
              <p className="font-mondwest normal-case text-sm text-muted-foreground">
                {v.thesis}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Per-agent signals */}
      {data.signals.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase text-text-tertiary">
            {ft.watch.signals}
          </span>
          <ul className="flex flex-col gap-2">
            {data.signals.map((s, i) => (
              <li
                key={`${s.source_agent}-${i}`}
                className="flex flex-col gap-0.5"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <Badge
                    tone={s.source_agent === "debate" ? "default" : "outline"}
                  >
                    {s.source_agent}
                  </Badge>
                  <Badge tone={directionTone(s.direction)}>{s.direction}</Badge>
                  <span className="font-mondwest normal-case text-xs text-muted-foreground">
                    {ft.watch.confidence.replace(
                      "{pct}",
                      (s.confidence * 100).toFixed(0),
                    )}
                  </span>
                </div>
                {s.thesis && (
                  <p className="font-mondwest normal-case text-xs text-muted-foreground">
                    {s.thesis}
                  </p>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Cited sources */}
      {data.research.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs uppercase text-text-tertiary">
            {ft.watch.sources}
          </span>
          <ul className="flex flex-col gap-1">
            {data.research.map((r, i) => (
              <li key={`${r.url || r.title}-${i}`}>
                {r.url ? (
                  <a
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                    className="font-mondwest normal-case text-xs text-foreground hover:underline"
                  >
                    {r.title || r.url}
                  </a>
                ) : (
                  <span className="font-mondwest normal-case text-xs text-foreground">
                    {r.title}
                  </span>
                )}
                {(r.publisher || r.trading_date) && (
                  <span className="ml-2 font-mondwest normal-case text-xs text-text-tertiary">
                    {[r.publisher, r.trading_date].filter(Boolean).join(" · ")}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Honest data-delay note. */}
      <p className="font-mondwest normal-case text-xs text-text-tertiary">
        {data.note || ft.watch.dataDelay}
      </p>
    </div>
  );
}

// ── Timeframe switcher + MA legend (attached to each chart) ───────────

/**
 * Compact segmented row of timeframe presets. The chart lives inside a
 * clickable card (click = focus the symbol for Analyze), so this swallows
 * pointer/keyboard events — changing the timeframe is a chart interaction,
 * not a symbol selection.
 */
function TimeframeSwitcher({
  preset,
  onPreset,
  ft,
}: {
  preset: TimeframePresetKey;
  onPreset: (p: TimeframePresetKey) => void;
  ft: FinanceTranslations;
}) {
  return (
    <div
      role="group"
      aria-label={ft.watch.timeframe.label}
      className="inline-flex overflow-hidden rounded-md border border-border"
      onClick={(e) => e.stopPropagation()}
      onKeyDown={(e) => e.stopPropagation()}
    >
      {TIMEFRAME_ORDER.map((key, i) => {
        const active = key === preset;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={active}
            onClick={() => onPreset(key)}
            className={cn(
              "px-2 py-1 font-mondwest text-display text-[0.6875rem] uppercase tracking-wider outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-primary/60",
              i > 0 && "border-l border-border",
              active
                ? "bg-primary text-primary-foreground"
                : "text-text-tertiary hover:bg-secondary/60 hover:text-foreground",
            )}
          >
            {timeframeLabel(key, ft)}
          </button>
        );
      })}
    </div>
  );
}

/** Small MA20/MA30 legend with color swatches matching the overlay lines. */
function MaLegend({
  hasMa20,
  hasMa30,
  ft,
}: {
  hasMa20: boolean;
  hasMa30: boolean;
  ft: FinanceTranslations;
}) {
  const chip = (color: string, label: string) => (
    <span className="inline-flex items-center gap-1.5">
      <span
        className="inline-block h-0.5 w-3.5 rounded-full"
        style={{ backgroundColor: color }}
      />
      {label}
    </span>
  );
  return (
    <div className="flex items-center gap-3 font-mondwest normal-case text-[0.6875rem] text-text-tertiary">
      {hasMa20 && chip(MA20_COLOR, ft.watch.ma20)}
      {hasMa30 && chip(MA30_COLOR, ft.watch.ma30)}
    </div>
  );
}

// ── Per-symbol card (quote + chart, selectable) ───────────────────────

function WatchSymbolCard({
  entry,
  ft,
  selected,
  onSelect,
}: {
  entry: WatchSymbol;
  ft: FinanceTranslations;
  selected: boolean;
  onSelect: () => void;
}) {
  const [preset, setPreset] = useState<TimeframePresetKey>(DEFAULT_PRESET);
  const { status, quote, bars, barsDone } = useWatchSymbol(entry, preset);

  // Per-symbol currency + unit for price rendering. `display` drives the full
  // "value currency / unit" readout; `formatValue` is the compact variant
  // (currency-prefixed, no unit) reused by the axis labels + hover tooltip.
  const display = useMemo<WatchPriceDisplay>(
    () => ({ currency: entry.currency, unit: entry.unit }),
    [entry.currency, entry.unit],
  );
  const formatValue = useMemo(
    () => (v: number) => fmtWatchPrice(v, display, ft, { withUnit: false }),
    [display, ft],
  );
  // Derived symbols (AU9999) carry a localized descriptor + provenance note.
  const label = entry.derived ? ft.watch.au9999Label : entry.label;

  // MA20/MA30 computed once per bars change (frontend-only, no extra fetch).
  const ma20 = useMemo(() => sma(bars, 20), [bars]);
  const ma30 = useMemo(() => sma(bars, 30), [bars]);
  const hasMa20 = ma20.some((v) => v !== null);
  const hasMa30 = ma30.some((v) => v !== null);

  if (status === "loading") {
    return <WatchSymbolSkeleton label={ft.watch.chartLoading} />;
  }

  return (
    <Card
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onSelect}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
      className={cn(
        "cursor-pointer outline-none transition-colors focus-visible:ring-1 focus-visible:ring-primary/60",
        selected
          ? "border-primary ring-1 ring-primary"
          : "hover:border-foreground/25",
      )}
    >
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="font-mono-ui text-base font-semibold text-foreground">
            {entry.symbol}
          </span>
          <span className="font-mondwest normal-case text-sm text-muted-foreground">
            {label}
          </span>
          {quote && (
            <span className="ml-auto flex items-baseline gap-2">
              <span className="text-xs text-text-tertiary">
                {ft.watch.price}
              </span>
              <span className="font-mono-ui text-base text-foreground">
                {fmtWatchPrice(quote.last, display, ft)}
              </span>
            </span>
          )}
        </div>

        {/* Derived-symbol provenance (AU9999 = international gold × CNY). */}
        {entry.derived && (
          <p className="-mt-2 font-mondwest normal-case text-[0.6875rem] text-text-tertiary">
            {ft.watch.au9999Note}
          </p>
        )}

        {status === "error" ? (
          <p className="font-mondwest normal-case py-3 text-sm text-muted-foreground">
            {ft.watch.noData}
          </p>
        ) : (
          <>
            {/* Quote meta line. */}
            {quote && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mondwest normal-case text-xs text-muted-foreground">
                {quote.bid !== null && (
                  <span>
                    {ft.watch.bid}{" "}
                    <span className="text-foreground">
                      {formatValue(quote.bid)}
                    </span>
                  </span>
                )}
                {quote.ask !== null && (
                  <span>
                    {ft.watch.ask}{" "}
                    <span className="text-foreground">
                      {formatValue(quote.ask)}
                    </span>
                  </span>
                )}
                {quote.volume !== null && (
                  <span>
                    {ft.watch.volume}{" "}
                    <span className="text-foreground">
                      {quote.volume.toLocaleString()}
                    </span>
                  </span>
                )}
                {quote.as_of && (
                  <span className="text-text-tertiary">
                    {ft.watch.asOf.replace("{time}", fmtTs(quote.as_of))}
                  </span>
                )}
              </div>
            )}

            {/* Timeframe switcher + MA legend — attached to the chart. */}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <TimeframeSwitcher preset={preset} onPreset={setPreset} ft={ft} />
              {(hasMa20 || hasMa30) && (
                <MaLegend hasMa20={hasMa20} hasMa30={hasMa30} ft={ft} />
              )}
            </div>

            {/* Price chart (candles + MA20/MA30 overlays + hover crosshair). */}
            {bars.length > 1 ? (
              <CandlestickChart
                bars={bars}
                ma20={ma20}
                ma30={ma30}
                ariaLabel={`${entry.symbol} ${label}`}
                ft={ft}
                formatValue={formatValue}
              />
            ) : barsDone ? (
              <p className="font-mondwest normal-case text-xs text-text-tertiary">
                {ft.watch.chartUnavailable}
              </p>
            ) : (
              <ChartSkeleton label={ft.watch.chartLoading} />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── The module panel ──────────────────────────────────────────────────

/**
 * Read-only cross-asset watch module (Gold/Oil/Rates/Crypto). For each
 * configured symbol it shows the current price and an inline-SVG K-line with
 * TradingView-style hover details. Exactly ONE Analyze control (top-right)
 * runs the multi-agent analysis for the currently-selected symbol. READ-ONLY —
 * no order or approval control exists here (Loop.md §3).
 */
export function WatchModule({ moduleKey }: { moduleKey: WatchModuleKey }) {
  const ft = useFinanceT();
  const symbols = WATCH_MODULES[moduleKey];
  const [selected, setSelected] = useState<string>(symbols[0]?.symbol ?? "");
  const [analyze, setAnalyze] = useState<AnalyzeState>({ status: "idle" });
  const aliveRef = useRef(true);

  useEffect(() => {
    aliveRef.current = true;
    return () => {
      aliveRef.current = false;
    };
  }, []);

  const selectSymbol = (symbol: string) => {
    if (symbol === selected) return;
    setSelected(symbol);
    // Analysis is symbol-specific — drop it when the focus changes.
    setAnalyze({ status: "idle" });
  };

  const runAnalyze = () => {
    if (!selected) return;
    setAnalyze({ status: "loading" });
    api.financeAnalyze(selected).then(
      (data) => {
        if (aliveRef.current) setAnalyze({ status: "done", data });
      },
      () => {
        if (aliveRef.current) setAnalyze({ status: "error" });
      },
    );
  };

  return (
    <section
      className="flex flex-col gap-4"
      aria-label={watchModuleName(moduleKey, ft)}
    >
      {/* Header: title + read-only badge (left), single Analyze (right). */}
      <div className="flex flex-wrap items-center gap-2">
        <BarChart3 className="h-5 w-5 text-muted-foreground" />
        <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
          {watchModuleName(moduleKey, ft)}
        </h2>
        <Badge tone="secondary">
          <span className="inline-flex items-center gap-1">
            <Eye className="h-3 w-3" />
            {ft.watch.readOnlyNote}
          </span>
        </Badge>
        <div className="ml-auto">
          {analyze.status === "done" ? (
            <Button
              type="button"
              size="sm"
              ghost
              onClick={() => setAnalyze({ status: "idle" })}
            >
              {ft.watch.hideAnalysis}
            </Button>
          ) : (
            <Button
              type="button"
              size="sm"
              outlined
              disabled={analyze.status === "loading" || !selected}
              onClick={runAnalyze}
            >
              {analyze.status === "loading"
                ? ft.watch.analyzing
                : ft.watch.analyze}
            </Button>
          )}
        </div>
      </div>

      <p className="font-mondwest normal-case text-xs text-text-tertiary">
        {ft.watch.dataDelay}
      </p>

      {/* Analysis panel for the selected symbol (single, page-level). */}
      {analyze.status === "error" && (
        <Card>
          <CardContent className="py-4">
            <p className="font-mondwest normal-case text-sm text-muted-foreground">
              {ft.watch.noData}
            </p>
          </CardContent>
        </Card>
      )}
      {analyze.status === "done" && (
        <Card>
          <CardContent className="py-4">
            <AnalyzePanel data={analyze.data} symbol={selected} ft={ft} />
          </CardContent>
        </Card>
      )}

      {/* Symbol cards (click/enter to focus the Analyze target). */}
      {symbols.map((entry) => (
        <WatchSymbolCard
          key={entry.symbol}
          entry={entry}
          ft={ft}
          selected={selected === entry.symbol}
          onSelect={() => selectSymbol(entry.symbol)}
        />
      ))}
    </section>
  );
}
