import {
  useCallback,
  useEffect,
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
import { directionTone, fmtMoney, fmtTs } from "./format";
import {
  WATCH_MODULES,
  watchModuleName,
  type WatchModuleKey,
  type WatchSymbol,
} from "./constants";

// ── Bars/quote cache + live-update tuning (Loop.md §3: READ-ONLY) ─────
//
// A module-level cache keyed by symbol+timeframe. It persists across desk
// switches (the components unmount, the Map does not), so navigating away and
// back renders the K-line instantly with NO refetch/loading flash. The heavy
// /bars endpoint is refetched only sparingly; live growth of the newest candle
// comes from the lightweight /quote endpoint on a backend-friendly interval.

const BARS_TIMEFRAME = "1d";
const BARS_LIMIT = 120;
/** Lightweight live-price poll → grows the last candle in place. */
const QUOTE_POLL_MS = 4_000;
/** Full K-line refetch — sparing, so we never hammer the heavy endpoint. */
const BARS_REFRESH_MS = 60_000;
/** Cached bars are treated as fresh within this window (no refetch on mount). */
const BARS_STALE_MS = 60_000;

interface BarsCacheEntry {
  bars: FinanceBar[];
  fetchedAt: number;
}
const barsCache = new Map<string, BarsCacheEntry>();
const quoteCache = new Map<string, FinanceQuote>();

function barsKey(symbol: string): string {
  return `${symbol}|${BARS_TIMEFRAME}|${BARS_LIMIT}`;
}

/**
 * Grow/adjust the newest candle from a fresh live price without a full reload:
 * close tracks the live last, and high/low widen to include it. Earlier bars
 * are untouched. Returns a new array so React re-renders (and the last candle's
 * CSS transition animates the move).
 */
function patchLastBar(bars: FinanceBar[], price: number | null): FinanceBar[] {
  if (price === null || bars.length === 0) return bars;
  const last = bars[bars.length - 1];
  const next: FinanceBar = {
    ...last,
    close: price,
    high: Math.max(last.high, price),
    low: Math.min(last.low, price),
  };
  return [...bars.slice(0, -1), next];
}

// ── Per-symbol data hook (cache-first + live last candle) ─────────────

type SymbolStatus = "loading" | "error" | "ready";

interface WatchSymbolData {
  status: SymbolStatus;
  quote: FinanceQuote | null;
  bars: FinanceBar[];
  /** True once a live /quote poll has grown the newest candle this session. */
  live: boolean;
}

function useWatchSymbol(symbol: string): WatchSymbolData {
  const key = barsKey(symbol);
  const cachedBars = barsCache.get(key);
  const cachedQuote = quoteCache.get(symbol) ?? null;
  // Seed from cache so re-selecting a symbol renders instantly — the skeleton
  // shows only on a true first load (cold cache).
  const [bars, setBars] = useState<FinanceBar[]>(cachedBars?.bars ?? []);
  const [quote, setQuote] = useState<FinanceQuote | null>(cachedQuote);
  const [status, setStatus] = useState<SymbolStatus>(
    cachedBars || cachedQuote ? "ready" : "loading",
  );
  const [live, setLive] = useState(false);
  const aliveRef = useRef(true);

  // Full quote + bars fetch (cold/stale cache, and the periodic refresh).
  const loadBars = useCallback(
    (opts?: { force?: boolean }) => {
      const entry = barsCache.get(key);
      const fresh = entry && Date.now() - entry.fetchedAt < BARS_STALE_MS;
      if (fresh && !opts?.force) return; // warm cache — no network, no flash
      // Quote and bars fetched independently so one 404 (common for
      // GC=F/^TNX/518880.SS) never blanks the other.
      Promise.allSettled([
        api.financeQuote(symbol),
        api.financeBars(symbol, {
          timeframe: BARS_TIMEFRAME,
          limit: BARS_LIMIT,
        }),
      ]).then(([q, b]) => {
        if (!aliveRef.current) return;
        const nextQuote = q.status === "fulfilled" ? q.value : null;
        const nextBars = b.status === "fulfilled" ? b.value.bars : [];
        if (
          nextQuote === null &&
          nextBars.length === 0 &&
          barsCache.get(key) === undefined
        ) {
          // Nothing to show and nothing cached — surface the inline error.
          setStatus("error");
          return;
        }
        if (nextBars.length > 0) {
          barsCache.set(key, { bars: nextBars, fetchedAt: Date.now() });
          setBars(nextBars);
          setLive(false); // fresh bars supersede the live-patched candle
        }
        if (nextQuote) {
          quoteCache.set(symbol, nextQuote);
          setQuote(nextQuote);
        }
        setStatus("ready");
      });
    },
    [key, symbol],
  );

  // Lightweight live-price poll — grows the newest candle without a reload.
  const pollQuote = useCallback(() => {
    api.financeQuote(symbol).then(
      (q) => {
        if (!aliveRef.current) return;
        quoteCache.set(symbol, q);
        setQuote(q);
        if (q.last !== null) {
          setBars((prev) => {
            if (prev.length === 0) return prev;
            const patched = patchLastBar(prev, q.last);
            const entry = barsCache.get(key);
            if (entry) barsCache.set(key, { ...entry, bars: patched });
            return patched;
          });
          setLive(true);
        }
      },
      () => {
        /* transient live-poll failure — keep the last good candle */
      },
    );
  }, [key, symbol]);

  // The card is keyed by symbol in the parent, so it mounts fresh per symbol —
  // the useState seeds above already reflect the cache. This effect only wires
  // up the fetch + the two polls (live quote, sparing bars refresh).
  useEffect(() => {
    aliveRef.current = true;
    loadBars(); // fills a miss / refreshes stale; no-op when warm
    const quoteId = window.setInterval(pollQuote, QUOTE_POLL_MS);
    const barsId = window.setInterval(
      () => loadBars({ force: true }),
      BARS_REFRESH_MS,
    );
    return () => {
      aliveRef.current = false;
      window.clearInterval(quoteId);
      window.clearInterval(barsId);
    };
  }, [loadBars, pollQuote]);

  return { status, quote, bars, live };
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
}: {
  bar: FinanceBar;
  hover: HoverState;
  ft: FinanceTranslations;
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
        {row(ft.watch.open, fmtMoney(bar.open))}
        {row(ft.watch.high, fmtMoney(bar.high))}
        {row(ft.watch.low, fmtMoney(bar.low))}
        {row(
          ft.watch.close,
          fmtMoney(bar.close),
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
 * success, down candles destructive. On mouse-move it renders a crosshair
 * (vertical + horizontal guide) and a compact OHLC(V) tooltip sourced from the
 * already-fetched bars — no extra fetch. The newest candle transitions
 * smoothly as the live price grows it.
 */
function CandlestickChart({
  bars,
  ariaLabel,
  live,
  ft,
}: {
  bars: FinanceBar[];
  ariaLabel: string;
  live: boolean;
  ft: FinanceTranslations;
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

  const wrapRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<HoverState | null>(null);
  const lastIndex = bars.length - 1;
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
          const cx = i * slot + slot / 2;
          const up = b.close >= b.open;
          const bodyTop = y(Math.max(b.open, b.close));
          const bodyBottom = y(Math.min(b.open, b.close));
          const bodyH = Math.max(bodyBottom - bodyTop, 0.75);
          const isLast = i === lastIndex;
          const dim = hover !== null && hover.index !== i;
          // Only the newest candle transitions (it's the one that grows live);
          // animating every candle on a full refetch would look jittery.
          const liveStyle: CSSProperties | undefined = isLast
            ? {
                transition:
                  "y 0.4s ease, height 0.4s ease, y1 0.4s ease, y2 0.4s ease",
              }
            : undefined;
          return (
            <g
              key={`${b.ts}-${i}`}
              className={up ? "text-success" : "text-destructive"}
              opacity={dim ? 0.5 : 1}
            >
              <line
                x1={cx}
                x2={cx}
                y1={y(b.high)}
                y2={y(b.low)}
                stroke="currentColor"
                strokeWidth="1"
                vectorEffect="non-scaling-stroke"
                style={liveStyle}
              />
              <rect
                x={cx - bodyW / 2}
                y={bodyTop}
                width={bodyW}
                height={bodyH}
                fill="currentColor"
                style={liveStyle}
              />
            </g>
          );
        })}
      </svg>

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

      {/* Live-updating indicator. */}
      {live && (
        <span className="pointer-events-none absolute right-1 top-1 inline-flex items-center gap-1 border border-success/40 bg-background/80 px-1.5 py-0.5 font-mondwest text-display text-[0.625rem] uppercase tracking-wider text-success backdrop-blur-sm">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-success" />
          {ft.watch.liveBadge}
        </span>
      )}

      {/* Crosshair tooltip. */}
      {hover && hoveredBar && (
        <CrosshairTooltip bar={hoveredBar} hover={hover} ft={ft} />
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
  const { status, quote, bars, live } = useWatchSymbol(entry.symbol);

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
            {entry.label}
          </span>
          {quote && (
            <span className="ml-auto flex items-baseline gap-2">
              <span className="text-xs text-text-tertiary">
                {ft.watch.price}
              </span>
              <span className="font-mono-ui text-base text-foreground">
                {fmtMoney(quote.last)}
              </span>
            </span>
          )}
        </div>

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
                      {fmtMoney(quote.bid)}
                    </span>
                  </span>
                )}
                {quote.ask !== null && (
                  <span>
                    {ft.watch.ask}{" "}
                    <span className="text-foreground">
                      {fmtMoney(quote.ask)}
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

            {/* Price chart. */}
            {bars.length > 1 ? (
              <CandlestickChart
                bars={bars}
                ariaLabel={`${entry.symbol} ${entry.label}`}
                live={live}
                ft={ft}
              />
            ) : (
              <p className="font-mondwest normal-case text-xs text-text-tertiary">
                {ft.watch.chartUnavailable}
              </p>
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
