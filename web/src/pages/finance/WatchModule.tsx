import { useEffect, useRef, useState } from "react";
import { BarChart3, Eye, LineChart, Sparkles } from "lucide-react";
import { api } from "@/lib/api";
import type { FinanceAnalyze, FinanceBar, FinanceQuote } from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import type { FinanceTranslations } from "@/i18n/types";
import { useFinanceT } from "./i18n";
import { directionTone, fmtMoney, fmtTs } from "./format";
import {
  WATCH_MODULES,
  watchModuleName,
  type WatchModuleKey,
  type WatchSymbol,
} from "./constants";

// ── Inline SVG candlestick chart (no charting dependency, Loop.md §3) ──

/**
 * Candlestick chart drawn as inline SVG from OHLCV bars. `preserveAspectRatio
 * ="none"` lets it fill the card width; wicks use a non-scaling stroke so they
 * stay crisp. Up candles tint success, down candles destructive.
 */
function CandlestickChart({
  bars,
  ariaLabel,
}: {
  bars: FinanceBar[];
  ariaLabel: string;
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

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      preserveAspectRatio="none"
      className="h-32 w-full"
      role="img"
      aria-label={ariaLabel}
    >
      {bars.map((b, i) => {
        const cx = i * slot + slot / 2;
        const up = b.close >= b.open;
        const bodyTop = y(Math.max(b.open, b.close));
        const bodyBottom = y(Math.min(b.open, b.close));
        const bodyH = Math.max(bodyBottom - bodyTop, 0.75);
        return (
          <g
            key={`${b.ts}-${i}`}
            className={up ? "text-success" : "text-destructive"}
          >
            <line
              x1={cx}
              x2={cx}
              y1={y(b.high)}
              y2={y(b.low)}
              stroke="currentColor"
              strokeWidth="1"
              vectorEffect="non-scaling-stroke"
            />
            <rect
              x={cx - bodyW / 2}
              y={bodyTop}
              width={bodyW}
              height={bodyH}
              fill="currentColor"
            />
          </g>
        );
      })}
    </svg>
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
  ft,
}: {
  data: FinanceAnalyze;
  ft: FinanceTranslations;
}) {
  const v = data.verdict;
  return (
    <div className="flex flex-col gap-3 border-t border-border pt-3">
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

// ── Per-symbol card (quote + chart + analyze) ─────────────────────────

type QuoteState =
  | { status: "loading" }
  | { status: "error" }
  | { status: "done"; quote: FinanceQuote; bars: FinanceBar[] };

function WatchSymbolCard({
  entry,
  ft,
}: {
  entry: WatchSymbol;
  ft: FinanceTranslations;
}) {
  const [state, setState] = useState<QuoteState>({ status: "loading" });
  const [analyze, setAnalyze] = useState<AnalyzeState>({ status: "idle" });
  // Cancel stale async writes if the card unmounts / symbol changes.
  const aliveRef = useRef(true);

  useEffect(() => {
    // The card is keyed by symbol in the parent, so it mounts fresh with the
    // initial loading/idle state — no synchronous reset needed here.
    aliveRef.current = true;
    // Quote and bars are fetched independently so one 404 (common for
    // GC=F/^TNX/518880.SS) never blanks the other.
    Promise.allSettled([
      api.financeQuote(entry.symbol),
      api.financeBars(entry.symbol, { timeframe: "1d", limit: 120 }),
    ]).then(([q, b]) => {
      if (!aliveRef.current) return;
      const quote = q.status === "fulfilled" ? q.value : null;
      const bars = b.status === "fulfilled" ? b.value.bars : [];
      if (quote === null && bars.length === 0) {
        setState({ status: "error" });
        return;
      }
      setState({
        status: "done",
        quote:
          quote ??
          ({
            symbol: entry.symbol,
            last: null,
            bid: null,
            ask: null,
            volume: null,
            as_of: "",
            note: "",
          } satisfies FinanceQuote),
        bars,
      });
    });
    return () => {
      aliveRef.current = false;
    };
  }, [entry.symbol]);

  const runAnalyze = () => {
    setAnalyze({ status: "loading" });
    api
      .financeAnalyze(entry.symbol)
      .then((data) => {
        if (aliveRef.current) setAnalyze({ status: "done", data });
      })
      .catch(() => {
        if (aliveRef.current) setAnalyze({ status: "error" });
      });
  };

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex flex-wrap items-baseline gap-2">
          <span className="font-mono-ui text-base font-semibold text-foreground">
            {entry.symbol}
          </span>
          <span className="font-mondwest normal-case text-sm text-muted-foreground">
            {entry.label}
          </span>
          {state.status === "done" && (
            <span className="ml-auto flex items-baseline gap-2">
              <span className="text-xs text-text-tertiary">
                {ft.watch.price}
              </span>
              <span className="font-mono-ui text-base text-foreground">
                {fmtMoney(state.quote.last)}
              </span>
            </span>
          )}
        </div>

        {state.status === "loading" && (
          <div className="flex items-center gap-2 py-3 font-mondwest normal-case text-sm text-muted-foreground">
            <Spinner /> {ft.watch.loadingQuote}
          </div>
        )}

        {state.status === "error" && (
          <p className="font-mondwest normal-case py-3 text-sm text-muted-foreground">
            {ft.watch.noData}
          </p>
        )}

        {state.status === "done" && (
          <>
            {/* Quote meta line. */}
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mondwest normal-case text-xs text-muted-foreground">
              {state.quote.bid !== null && (
                <span>
                  {ft.watch.bid}{" "}
                  <span className="text-foreground">
                    {fmtMoney(state.quote.bid)}
                  </span>
                </span>
              )}
              {state.quote.ask !== null && (
                <span>
                  {ft.watch.ask}{" "}
                  <span className="text-foreground">
                    {fmtMoney(state.quote.ask)}
                  </span>
                </span>
              )}
              {state.quote.volume !== null && (
                <span>
                  {ft.watch.volume}{" "}
                  <span className="text-foreground">
                    {state.quote.volume.toLocaleString()}
                  </span>
                </span>
              )}
              {state.quote.as_of && (
                <span className="text-text-tertiary">
                  {ft.watch.asOf.replace("{time}", fmtTs(state.quote.as_of))}
                </span>
              )}
            </div>

            {/* Price chart. */}
            {state.bars.length > 1 ? (
              <CandlestickChart
                bars={state.bars}
                ariaLabel={`${entry.symbol} ${entry.label}`}
              />
            ) : (
              <p className="flex items-center gap-2 font-mondwest normal-case text-xs text-text-tertiary">
                <LineChart className="h-3.5 w-3.5" />
                {ft.watch.chartUnavailable}
              </p>
            )}

            {/* Analyze affordance (read-only thesis). */}
            <div className="flex items-center gap-2">
              {analyze.status === "done" ? (
                <Button
                  type="button"
                  size="sm"
                  ghost
                  onClick={() => setAnalyze({ status: "idle" })}
                  prefix={<Sparkles className="h-4 w-4" />}
                >
                  {ft.watch.hideAnalysis}
                </Button>
              ) : (
                <Button
                  type="button"
                  size="sm"
                  outlined
                  disabled={analyze.status === "loading"}
                  onClick={runAnalyze}
                  prefix={
                    analyze.status === "loading" ? (
                      <Spinner />
                    ) : (
                      <Sparkles className="h-4 w-4" />
                    )
                  }
                >
                  {analyze.status === "loading"
                    ? ft.watch.analyzing
                    : ft.watch.analyze}
                </Button>
              )}
            </div>

            {analyze.status === "error" && (
              <p className="font-mondwest normal-case text-sm text-muted-foreground">
                {ft.watch.noData}
              </p>
            )}
            {analyze.status === "done" && (
              <AnalyzePanel data={analyze.data} ft={ft} />
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
 * configured symbol it shows the current price, an inline-SVG price chart,
 * and an on-demand multi-agent analysis. READ-ONLY — no order or approval
 * control exists here (Loop.md §3).
 */
export function WatchModule({ moduleKey }: { moduleKey: WatchModuleKey }) {
  const ft = useFinanceT();
  const symbols = WATCH_MODULES[moduleKey];
  return (
    <section className="flex flex-col gap-4" aria-label={watchModuleName(moduleKey, ft)}>
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
      </div>
      <p className="font-mondwest normal-case text-xs text-text-tertiary">
        {ft.watch.dataDelay}
      </p>
      {symbols.map((entry) => (
        <WatchSymbolCard key={entry.symbol} entry={entry} ft={ft} />
      ))}
    </section>
  );
}
