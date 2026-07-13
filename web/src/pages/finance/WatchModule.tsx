import { useEffect, useMemo, useRef, useState } from "react";
import { BarChart3, Check, ChevronDown, Eye, SlidersHorizontal } from "lucide-react";
import { init, dispose } from "klinecharts";
import type {
  Chart,
  CandleTooltipLegendsCustomCallback,
  DeepPartial,
  IndicatorCreate,
  KLineData,
  Period,
  Styles,
  TooltipLegend,
} from "klinecharts";
import { api } from "@/lib/api";
import type { FinanceAnalyze, FinanceBar, FinanceQuote } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { cn } from "@/lib/utils";
import { useTheme } from "@/themes";
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

// ── ONE large klinecharts K-line per page (TradingView-style) ─────────
//
// The library owns the candles, axes, crosshair, tooltip, pan and zoom — we
// no longer hand-roll SVG candles. This surface stays READ-ONLY (Loop.md §3):
// cached OHLCV bars + built-in indicators + a hover crosshair/legend. There is
// NO live price poll. A module-level bars/quote cache persists across symbol
// and timeframe switches so re-visiting a preset renders instantly.

/** klinecharts' default main (candle) pane id — overlays target this pane. */
const CANDLE_PANE_ID = "candle_pane";

// ── Per-(symbol,timeframe) bars cache + timeframe presets ─────────────

/** Chart timeframe presets. Each maps to a (financeBars timeframe, limit) AND a
 * klinecharts Period. Default = Day. Labels + hints are localized. */
type TimeframePresetKey = "intraday" | "fiveDay" | "day" | "week" | "month";

/**
 * Per-timeframe data + view config. `limit` is a GENEROUS history fetch (the
 * backend /v1/bars caps at 500); `defaultView` is the number of most-recent
 * bars the chart shows by DEFAULT. The rest of the fetched history stays in the
 * chart's dataset so panning/scrolling left reveals earlier bars instantly with
 * no network round-trip (Loop.md §3: READ-ONLY). Switching timeframe resets the
 * zoom/range to this default view (see `applyDefaultView`).
 */
const TIMEFRAME_CONFIG: Record<
  TimeframePresetKey,
  { timeframe: string; limit: number; defaultView: number; period: Period }
> = {
  // fetch ~5 days of 5m bars, default-view ~1 day (~78 bars).
  intraday: {
    timeframe: "5m",
    limit: 390,
    defaultView: 78,
    period: { type: "minute", span: 5 },
  },
  // fetch ~1 month of 30m bars, default-view ~5 days (~66 bars).
  fiveDay: {
    timeframe: "30m",
    limit: 260,
    defaultView: 66,
    period: { type: "minute", span: 30 },
  },
  // fetch ~2 years of daily bars, default-view ~6 months (~126 bars). DEFAULT.
  day: {
    timeframe: "1d",
    limit: 500,
    defaultView: 126,
    period: { type: "day", span: 1 },
  },
  // fetch ~5 years of weekly bars, default-view ~2 years (~104 bars).
  week: {
    timeframe: "1wk",
    limit: 260,
    defaultView: 104,
    period: { type: "week", span: 1 },
  },
  // fetch ~20 years of monthly bars, default-view ~10 years (~120 bars).
  month: {
    timeframe: "1mo",
    limit: 240,
    defaultView: 120,
    period: { type: "month", span: 1 },
  },
};

const TIMEFRAME_ORDER: TimeframePresetKey[] = [
  "intraday",
  "fiveDay",
  "day",
  "week",
  "month",
];

const DEFAULT_PRESET: TimeframePresetKey = "day";

/** Intraday presets get HH:MM in the crosshair tooltip; the rest get a date. */
function isIntradayPreset(key: TimeframePresetKey): boolean {
  return key === "intraday" || key === "fiveDay";
}

function timeframeLabel(
  key: TimeframePresetKey,
  ft: FinanceTranslations,
): string {
  return ft.watch.timeframe[key];
}

function timeframeHint(
  key: TimeframePresetKey,
  ft: FinanceTranslations,
): string {
  return ft.watch.timeframeHint[key];
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

// ── Per-symbol data hooks (cache-first, per-timeframe bars) ────────────

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
 */
function useWatchSymbol(
  entry: WatchSymbol,
  preset: TimeframePresetKey,
): WatchSymbolData {
  const derived = entry.derived ?? null;
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

// ── Indicators (klinecharts built-ins) with educational descriptions ──

type IndicatorKey = "MA" | "EMA" | "BOLL" | "VOL" | "MACD" | "RSI" | "KDJ";

interface IndicatorDef {
  key: IndicatorKey;
  /** Overlay (drawn on the candle pane) vs. its own sub-pane below. */
  overlay: boolean;
  /** Calculation periods; omitted → klinecharts default. */
  calcParams?: number[];
  name: (ft: FinanceTranslations) => string;
  desc: (ft: FinanceTranslations) => string;
}

const INDICATORS: IndicatorDef[] = [
  {
    key: "MA",
    overlay: true,
    calcParams: [20, 30, 60],
    name: (ft) => ft.watch.indicators.ma,
    desc: (ft) => ft.watch.indicators.maDesc,
  },
  {
    key: "EMA",
    overlay: true,
    calcParams: [12, 26],
    name: (ft) => ft.watch.indicators.ema,
    desc: (ft) => ft.watch.indicators.emaDesc,
  },
  {
    key: "BOLL",
    overlay: true,
    name: (ft) => ft.watch.indicators.boll,
    desc: (ft) => ft.watch.indicators.bollDesc,
  },
  {
    key: "VOL",
    overlay: false,
    name: (ft) => ft.watch.indicators.vol,
    desc: (ft) => ft.watch.indicators.volDesc,
  },
  {
    key: "MACD",
    overlay: false,
    name: (ft) => ft.watch.indicators.macd,
    desc: (ft) => ft.watch.indicators.macdDesc,
  },
  {
    key: "RSI",
    overlay: false,
    name: (ft) => ft.watch.indicators.rsi,
    desc: (ft) => ft.watch.indicators.rsiDesc,
  },
  {
    key: "KDJ",
    overlay: false,
    name: (ft) => ft.watch.indicators.kdj,
    desc: (ft) => ft.watch.indicators.kdjDesc,
  },
];

/** DEFAULT ON: moving averages (overlay) + volume (sub-pane). */
const DEFAULT_INDICATORS: IndicatorKey[] = ["MA", "VOL"];

// ── Theme → klinecharts styles (reacts to the app's light/dark theme) ──

interface ChartTheme {
  up: string;
  down: string;
  text: string;
  grid: string;
  axisLine: string;
  axisText: string;
  cross: string;
  crossBg: string;
  crossText: string;
}

function hexToRgb(hex: string): { r: number; g: number; b: number } | null {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  let h = m[1];
  if (h.length === 3) h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2];
  const n = parseInt(h, 16);
  return { r: (n >> 16) & 255, g: (n >> 8) & 255, b: n & 255 };
}

/** Resolve chart colors from the app's CSS custom properties so the K-line
 * matches whichever Hermes theme (light or dark) is active. */
function readChartTheme(): ChartTheme {
  const cs = getComputedStyle(document.documentElement);
  const readVar = (name: string, fallback: string) =>
    cs.getPropertyValue(name).trim() || fallback;
  const bgHex = readVar("--background-base", "#0b0f14");
  const textHex = readVar("--midground-base", "#e8e8e8");
  const up = readVar("--color-success", "#22c55e");
  const down = readVar("--color-destructive", "#ef4444");
  const rgb = hexToRgb(textHex) ?? { r: 232, g: 232, b: 232 };
  const rgba = (a: number) => `rgba(${rgb.r},${rgb.g},${rgb.b},${a})`;
  return {
    up,
    down,
    text: textHex,
    grid: rgba(0.08),
    axisLine: rgba(0.18),
    axisText: rgba(0.62),
    cross: rgba(0.5),
    // High-contrast crosshair label: solid foreground bg + background-colored text.
    crossBg: textHex,
    crossText: bgHex,
  };
}

/** Format a bar timestamp for the crosshair legend (intraday adds HH:MM). */
function fmtBarTime(ts: number, intraday: boolean): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString(
    undefined,
    intraday
      ? { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { year: "numeric", month: "short", day: "numeric" },
  );
}

/** Build the full klinecharts style patch: candle/axis/grid/crosshair colors +
 * a custom tooltip legend that formats OHLC(V) with the symbol's currency. */
function buildChartStyles(
  ft: FinanceTranslations,
  display: WatchPriceDisplay,
  intraday: boolean,
): DeepPartial<Styles> {
  const t = readChartTheme();
  const fmt = (v: number | null | undefined) =>
    fmtWatchPrice(v, display, ft, { withUnit: false });

  const legend: CandleTooltipLegendsCustomCallback = (data, styles) => {
    const k = data.current;
    if (k === null || k === undefined) return [];
    const up = (k.close ?? 0) >= (k.open ?? 0);
    const closeColor = up ? styles.bar.upColor : styles.bar.downColor;
    const rows: TooltipLegend[] = [
      { title: ft.watch.date, value: fmtBarTime(k.timestamp, intraday) },
      { title: ft.watch.open, value: fmt(k.open) },
      { title: ft.watch.high, value: fmt(k.high) },
      { title: ft.watch.low, value: fmt(k.low) },
      { title: ft.watch.close, value: { text: fmt(k.close), color: closeColor } },
    ];
    if (typeof k.volume === "number" && k.volume > 0) {
      rows.push({ title: ft.watch.volume, value: k.volume.toLocaleString() });
    }
    return rows;
  };

  return {
    grid: {
      horizontal: { color: t.grid },
      vertical: { color: t.grid },
    },
    candle: {
      bar: {
        upColor: t.up,
        downColor: t.down,
        noChangeColor: t.axisText,
        upBorderColor: t.up,
        downBorderColor: t.down,
        noChangeBorderColor: t.axisText,
        upWickColor: t.up,
        downWickColor: t.down,
        noChangeWickColor: t.axisText,
      },
      priceMark: {
        high: { color: t.axisText },
        low: { color: t.axisText },
        last: {
          upColor: t.up,
          downColor: t.down,
          text: { color: "#ffffff" },
        },
      },
      tooltip: {
        title: { show: false },
        legend: { color: t.text, template: legend },
      },
    },
    indicator: {
      tooltip: {
        title: { color: t.axisText },
        legend: { color: t.axisText },
      },
    },
    xAxis: {
      axisLine: { color: t.axisLine },
      tickLine: { color: t.axisLine },
      tickText: { color: t.axisText },
    },
    yAxis: {
      axisLine: { color: t.axisLine },
      tickLine: { color: t.axisLine },
      tickText: { color: t.axisText },
    },
    crosshair: {
      horizontal: {
        line: { color: t.cross },
        text: { backgroundColor: t.crossBg, borderColor: t.crossBg, color: t.crossText },
      },
      vertical: {
        line: { color: t.cross },
        text: { backgroundColor: t.crossBg, borderColor: t.crossBg, color: t.crossText },
      },
    },
    separator: { color: t.axisLine },
  };
}

/** Map fetched OHLCV bars to klinecharts KLineData (ms timestamp), dropping
 * any bar with an unparseable date. */
function toKLineData(bars: FinanceBar[]): KLineData[] {
  const out: KLineData[] = [];
  for (const b of bars) {
    const ts = new Date(b.ts).getTime();
    if (Number.isNaN(ts)) continue;
    out.push({
      timestamp: ts,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
      volume: b.volume,
    });
  }
  return out;
}

// ── The big K-line chart (klinecharts instance owner) ─────────────────

function KLineChart({
  entry,
  preset,
  active,
  ft,
}: {
  entry: WatchSymbol;
  preset: TimeframePresetKey;
  active: Set<IndicatorKey>;
  ft: FinanceTranslations;
}) {
  const { themeName } = useTheme();
  const { status, bars } = useWatchSymbol(entry, preset);

  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<Chart | null>(null);
  const appliedRef = useRef<Set<IndicatorKey>>(new Set());
  const [ready, setReady] = useState(false);

  const display = useMemo<WatchPriceDisplay>(
    () => ({ currency: entry.currency, unit: entry.unit }),
    [entry.currency, entry.unit],
  );
  const intraday = isIntradayPreset(preset);
  const klineData = useMemo(() => toKLineData(bars), [bars]);
  const { period, defaultView } = TIMEFRAME_CONFIG[preset];

  // Init once. Styles are applied synchronously here (mount vars are already
  // in place) to avoid a flash of klinecharts' defaults, then re-applied by the
  // theme effect on subsequent theme/symbol/locale changes.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const chart = init(el);
    if (!chart) return;
    chartRef.current = chart;
    chart.setStyles(buildChartStyles(ft, display, intraday));
    setReady(true);
    return () => {
      dispose(chart);
      chartRef.current = null;
      appliedRef.current = new Set();
      setReady(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Re-apply styles when the theme, symbol currency/unit, locale, or preset
  // changes. Deferred to rAF so the ThemeProvider (an ancestor) has committed
  // its CSS-var updates before we read them.
  useEffect(() => {
    if (!ready) return;
    const id = requestAnimationFrame(() => {
      chartRef.current?.setStyles(buildChartStyles(ft, display, intraday));
    });
    return () => cancelAnimationFrame(id);
  }, [ready, themeName, ft, display, intraday]);

  // Feed data: set the symbol (precision), the period, then a data loader that
  // serves the ENTIRE pre-fetched history at once (no lazy paging). Runs on
  // every symbol/preset/bars change so async-derived bars (AU9999) load once
  // the FX quote settles.
  //
  // After the data lands we RESET the zoom/range to this timeframe's default
  // view: bar spacing is sized so ~defaultView of the most-recent bars fill the
  // width, then we scroll to the latest bar. This discards any prior
  // drag/stretch when the timeframe changes (Loop.md §3 UX). The earlier bars
  // stay in the dataset, so scrolling/panning left reveals previous sessions
  // instantly without a network round-trip. Deferred to rAF so klinecharts has
  // applied the fresh init data + laid out the container first.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !ready) return;
    chart.setSymbol({
      ticker: entry.symbol,
      pricePrecision: 2,
      volumePrecision: 0,
    });
    chart.setPeriod(period);
    chart.setDataLoader({
      getBars: ({ type, callback }) => {
        // Fixed window — no lazy backward/forward loading (more = false).
        callback(type === "init" ? klineData : [], false);
      },
    });
    const raf = requestAnimationFrame(() => {
      if (chartRef.current !== chart) return;
      const width = containerRef.current?.clientWidth ?? 0;
      if (width > 0 && defaultView > 0 && klineData.length > 0) {
        // Bar spacing so ~defaultView bars fill the visible width, clamped to
        // klinecharts' sane px range. Reset every reload → no carried-over zoom.
        chart.setBarSpace(Math.min(50, Math.max(2, width / defaultView)));
      }
      chart.scrollToRealTime(0);
    });
    return () => cancelAnimationFrame(raf);
  }, [ready, entry.symbol, period, defaultView, klineData]);

  // Keep the chart filling its container: resize with the pane (ResizeObserver)
  // and on window resize so it always re-fits when the layout changes.
  useEffect(() => {
    const chart = chartRef.current;
    const el = containerRef.current;
    if (!chart || !ready || !el) return;
    const onResize = () => chartRef.current?.resize();
    const ro =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(onResize) : null;
    ro?.observe(el);
    window.addEventListener("resize", onResize);
    return () => {
      ro?.disconnect();
      window.removeEventListener("resize", onResize);
    };
  }, [ready]);

  // Reconcile indicators: add/remove built-ins as the active set changes.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !ready) return;
    const applied = appliedRef.current;
    for (const def of INDICATORS) {
      const want = active.has(def.key);
      const has = applied.has(def.key);
      if (want && !has) {
        const create: IndicatorCreate = { name: def.key };
        if (def.calcParams) create.calcParams = def.calcParams;
        if (def.overlay) create.paneId = CANDLE_PANE_ID;
        chart.createIndicator(create, def.overlay);
        applied.add(def.key);
      } else if (!want && has) {
        chart.removeIndicator(
          def.overlay ? { paneId: CANDLE_PANE_ID, name: def.key } : { name: def.key },
        );
        applied.delete(def.key);
      }
    }
  }, [ready, active]);

  const showEmpty =
    status === "error" ||
    (status !== "loading" && klineData.length < 2 && ready);
  const showLoading = status === "loading" && klineData.length === 0;

  return (
    <div className="relative w-full">
      <div
        ref={containerRef}
        className="h-[460px] w-full sm:h-[500px]"
        role="img"
        aria-label={`${entry.symbol} ${timeframeLabel(preset, ft)}`}
      />
      {(showLoading || showEmpty) && (
        <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {showLoading ? ft.watch.chartLoading : ft.watch.noData}
          </p>
        </div>
      )}
    </div>
  );
}

// ── Symbol dropdown (top-left) ────────────────────────────────────────

function symbolOptionLabel(entry: WatchSymbol, ft: FinanceTranslations): string {
  const label = entry.derived ? ft.watch.au9999Label : entry.label;
  return `${label} · ${entry.symbol}`;
}

function SymbolDropdown({
  symbols,
  selected,
  onSelect,
  ft,
}: {
  symbols: WatchSymbol[];
  selected: string;
  onSelect: (symbol: string) => void;
  ft: FinanceTranslations;
}) {
  return (
    <div className="relative inline-flex">
      <select
        aria-label={ft.watch.selectSymbol}
        value={selected}
        onChange={(e) => onSelect(e.target.value)}
        className="appearance-none rounded-md border border-border bg-background py-1.5 pl-2.5 pr-8 font-mono-ui text-sm text-foreground outline-none transition-colors hover:border-foreground/25 focus-visible:ring-1 focus-visible:ring-primary/60"
      >
        {symbols.map((s) => (
          <option key={s.symbol} value={s.symbol}>
            {symbolOptionLabel(s, ft)}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 text-text-tertiary" />
    </div>
  );
}

// ── Indicators menu (toggle analysis tools + descriptions) ────────────

function IndicatorsMenu({
  active,
  onToggle,
  ft,
}: {
  active: Set<IndicatorKey>;
  onToggle: (key: IndicatorKey) => void;
  ft: FinanceTranslations;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <Button
        type="button"
        size="sm"
        outlined
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        prefix={<SlidersHorizontal className="h-3.5 w-3.5" />}
      >
        {ft.watch.indicators.title}
      </Button>
      {open && (
        <div className="absolute right-0 z-30 mt-1 w-80 max-w-[85vw] overflow-hidden rounded-md border border-border bg-background/95 shadow-md backdrop-blur-sm">
          <p className="border-b border-border px-3 py-2 font-mondwest normal-case text-xs text-text-tertiary">
            {ft.watch.indicators.hint}
          </p>
          <ul className="max-h-[60vh] overflow-y-auto py-1">
            {INDICATORS.map((def) => {
              const on = active.has(def.key);
              return (
                <li key={def.key}>
                  <button
                    type="button"
                    aria-pressed={on}
                    onClick={() => onToggle(def.key)}
                    className="flex w-full items-start gap-2.5 px-3 py-2 text-left outline-none transition-colors hover:bg-secondary/60 focus-visible:bg-secondary/60"
                  >
                    <span
                      className={cn(
                        "mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded border",
                        on
                          ? "border-primary bg-primary text-primary-foreground"
                          : "border-border text-transparent",
                      )}
                    >
                      <Check className="h-3 w-3" />
                    </span>
                    <span className="flex min-w-0 flex-col gap-0.5">
                      <span className="flex items-center gap-2">
                        <span className="font-mono-ui text-sm text-foreground">
                          {def.name(ft)}
                        </span>
                        <Badge tone="secondary">
                          {def.overlay
                            ? ft.watch.indicators.overlay
                            : ft.watch.indicators.pane}
                        </Badge>
                      </span>
                      <span className="font-mondwest normal-case text-xs text-muted-foreground">
                        {def.desc(ft)}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── Timeframe switcher (bottom-right) ─────────────────────────────────

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
    >
      {TIMEFRAME_ORDER.map((key, i) => {
        const activeSeg = key === preset;
        return (
          <button
            key={key}
            type="button"
            aria-pressed={activeSeg}
            title={timeframeHint(key, ft)}
            onClick={() => onPreset(key)}
            className={cn(
              "px-2.5 py-1 font-mondwest text-display text-[0.6875rem] uppercase tracking-wider outline-none transition-colors focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-primary/60",
              i > 0 && "border-l border-border",
              activeSeg
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

// ── Chart card (dropdown + last price + indicators + chart + timeframe) ─

function ChartCard({
  symbols,
  selected,
  onSelect,
  ft,
}: {
  symbols: WatchSymbol[];
  selected: string;
  onSelect: (symbol: string) => void;
  ft: FinanceTranslations;
}) {
  const entry = useMemo(
    () => symbols.find((s) => s.symbol === selected) ?? symbols[0],
    [symbols, selected],
  );
  const [preset, setPreset] = useState<TimeframePresetKey>(DEFAULT_PRESET);
  const [active, setActive] = useState<Set<IndicatorKey>>(
    () => new Set(DEFAULT_INDICATORS),
  );

  const toggleIndicator = (key: IndicatorKey) => {
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const display = useMemo<WatchPriceDisplay>(
    () => ({ currency: entry.currency, unit: entry.unit }),
    [entry.currency, entry.unit],
  );
  const { quote } = useWatchSymbol(entry, preset);
  const unitCaption =
    entry.unit === "pct"
      ? "%"
      : entry.currency
        ? entry.unit
          ? `${entry.currency} / ${ft.watch.units[entry.unit]}`
          : entry.currency
        : entry.unit
          ? ft.watch.units[entry.unit]
          : "";

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        {/* Top row: symbol dropdown (left) + last price + indicators (right). */}
        <div className="flex flex-wrap items-center gap-3">
          <SymbolDropdown
            symbols={symbols}
            selected={selected}
            onSelect={onSelect}
            ft={ft}
          />
          {quote && quote.last !== null && (
            <span className="flex items-baseline gap-2">
              <span className="text-xs text-text-tertiary">{ft.watch.price}</span>
              <span className="font-mono-ui text-lg text-foreground">
                {fmtWatchPrice(quote.last, display, ft)}
              </span>
              {quote.as_of && (
                <span className="font-mondwest normal-case text-xs text-text-tertiary">
                  {ft.watch.asOf.replace("{time}", fmtTs(quote.as_of))}
                </span>
              )}
            </span>
          )}
          <div className="ml-auto">
            <IndicatorsMenu active={active} onToggle={toggleIndicator} ft={ft} />
          </div>
        </div>

        {/* Derived-symbol provenance (AU9999 = international gold × CNY). */}
        {entry.derived && (
          <p className="font-mondwest normal-case text-[0.6875rem] text-text-tertiary">
            {ft.watch.au9999Note}
          </p>
        )}

        {/* The one large K-line chart. `entry.symbol` keys it so switching
            instruments rebuilds cleanly (fresh indicators + data). */}
        <KLineChart
          key={entry.symbol}
          entry={entry}
          preset={preset}
          active={active}
          ft={ft}
        />

        {/* Bottom row: price-scale unit (left) + timeframe switcher (right). */}
        <div className="flex flex-wrap items-center justify-between gap-2">
          {unitCaption ? (
            <span className="font-mondwest normal-case text-[0.6875rem] text-text-tertiary">
              {ft.watch.priceScale}: {unitCaption}
            </span>
          ) : (
            <span />
          )}
          <TimeframeSwitcher preset={preset} onPreset={setPreset} ft={ft} />
        </div>
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

// ── The module panel ──────────────────────────────────────────────────

/**
 * Read-only cross-asset watch module (Gold/Oil/Rates/Crypto). ONE large,
 * professional klinecharts K-line per page: a symbol dropdown (top-left)
 * changes the target, a timeframe switcher (bottom-right) changes the range,
 * and an Indicators menu toggles built-in studies (each with a short
 * educational note). Exactly ONE Analyze control (top-right) runs the
 * multi-agent analysis for the selected symbol. READ-ONLY — no order or
 * approval control exists here (Loop.md §3).
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

      {/* The one large K-line chart + its controls. */}
      <ChartCard
        symbols={symbols}
        selected={selected}
        onSelect={selectSymbol}
        ft={ft}
      />

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
    </section>
  );
}
