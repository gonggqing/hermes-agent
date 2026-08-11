import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router";
import {
  AlertTriangle,
  Globe,
  Newspaper,
  PlugZap,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { api } from "@/lib/api";
import type {
  FinanceAccountResponse,
  FinanceAccountView,
  FinanceCandidate,
  FinanceFill,
  FinanceHealth,
  FinanceMarketSnapshot,
  FinanceMode,
  FinanceOpenOrder,
  FinancePendingCandidate,
  FinancePosition,
  FinanceReports,
  FinanceResearchBrief as FinanceResearchBriefData,
  FinanceMarketPerformance,
  FinanceSnapshot,
  FinanceStats,
  FinanceWatchlistItem,
  FinanceResearchWatchlist,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { CommandBlock } from "@nous-research/ui/ui/components/command-block";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { Input } from "@nous-research/ui/ui/components/input";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useI18n } from "@/i18n";
import { ApprovalQueue, SessionControls } from "@/pages/finance/ApprovalQueue";
import { HistorySection } from "@/pages/finance/HistorySection";
import { PortfolioManager } from "@/pages/finance/PortfolioManager";
import { PortfolioControls } from "@/pages/finance/PortfolioControls";
import { ResearchBrief } from "@/pages/finance/ResearchBrief";
import {
  PredictionReview,
  StrategyBacktests,
} from "@/pages/finance/PredictionReview";
import { WatchModule } from "@/pages/finance/WatchModule";
import { ResearchWatchlistDetail } from "@/pages/finance/ResearchWatchlistDetail";
import {
  FinanceBottomBar,
  MasterDetail,
  SidebarButton,
  SidebarGroup,
} from "@/pages/finance/layout";
import {
  ACTIVE_MARKETS,
  PLACEHOLDER_MARKETS,
  RESEARCH_TOOL_DESKS,
  WATCH_MODULE_KEYS,
  isActiveMarketDesk,
  isWatchDesk,
  marketName,
  watchModuleName,
  type FinanceDesk,
} from "@/pages/finance/constants";
import { useFinanceT } from "@/pages/finance/i18n";
import type { FinanceTranslations } from "@/i18n/types";
import {
  fmtMoney,
  fmtPct,
  fmtQty,
  fmtSigned,
  fmtTs,
  pnlClass,
  regimeTone,
  sideTone,
} from "@/pages/finance/format";

const REFRESH_INTERVAL_MS = 30_000;
const SNAPSHOT_LIMIT = 120;

const SERVICE_START_COMMAND =
  "cd trader && uv run python -m swing_trader serve";

type FinanceTab = "research" | "queue" | "portfolio" | "holdings";

const TABS: FinanceTab[] = ["research", "queue", "portfolio", "holdings"];
const DESKS: FinanceDesk[] = [
  ...ACTIVE_MARKETS,
  ...PLACEHOLDER_MARKETS,
  ...RESEARCH_TOOL_DESKS,
  ...WATCH_MODULE_KEYS,
];

/** Portfolio sidebar entries that are not per-position rows. */
type PortfolioView =
  | "account"
  | "controls"
  | "orders"
  | "history"
  | "market"
  | "reports";

/** Account numbers shared by the live view and the ledger-fallback snapshot. */
interface AccountNumbers {
  equity: number;
  cash: number;
  upnl: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: string;
  base_currency: string;
  cash_by_currency: Record<string, number>;
  equity_by_currency: Record<string, number>;
  ts?: string;
  marks_live?: boolean;
  marks_as_of?: string | null;
}

// ── Account / positions / orders / market / reports sections ──────────

// Equity curve: a single-series line chart of account equity over time with
// real x (time) / y (value) axes, recessive gridlines and a hover readout.
// Hand-rolled SVG in the house style (currentColor driven by text-* classes,
// uniform viewBox scaling — no preserveAspectRatio distortion).
type CurvePoint = { ts: string; value: number };

// Per-currency equity: different currencies are different units/scales, so they
// are shown ONE AT A TIME behind a US/HK toggle (different currencies are
// different units — never one FX-blended base line, never a dual-axis chart).
// The live account value is appended as the latest point so the curve ends at
// the CURRENT (live-overlaid) equity, aligned with the account panel — not at
// the last daily-close snapshot.
function EquityCurve({
  snapshots,
  live,
}: {
  snapshots: FinanceSnapshot[];
  live: AccountNumbers | null;
}) {
  const ft = useFinanceT();
  const currencies = Array.from(
    new Set([
      ...snapshots.flatMap((s) => Object.keys(s.equity_by_currency ?? {})),
      ...Object.keys(live?.equity_by_currency ?? {}),
    ]),
  ).sort();
  const options = (currencies.length > 0 ? currencies : ["USD"]).map((c) => ({
    value: c,
    label: `${marketForCurrency(c)} · ${c}`,
  }));
  const [sel, setSel] = useState(options[0].value);
  const cur = options.some((o) => o.value === sel) ? sel : options[0].value;

  const points: CurvePoint[] = snapshots.map((s) => ({
    ts: s.ts,
    value:
      currencies.length > 0 ? (s.equity_by_currency?.[cur] ?? 0) : s.equity,
  }));
  // append the live "now" point so the curve ends at the CURRENT value (aligned
  // with the account panel), only when a live overlay is actually active — when
  // the loop is idle the latest snapshot is already the last point.
  const liveVal = live?.equity_by_currency?.[cur];
  const liveTs = live?.marks_as_of ?? live?.ts;
  if (live?.marks_live && liveVal != null && liveTs) {
    points.push({ ts: liveTs, value: liveVal });
  }

  return (
    <div className="flex flex-col gap-2">
      {options.length > 1 && (
        <Segmented<string> value={cur} onChange={setSel} options={options} />
      )}
      {points.length < 2 ? (
        <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
          {ft.account.notEnoughSnapshots}
        </p>
      ) : (
        <CurveChart points={points} currency={cur} />
      )}
    </div>
  );
}

// Client-side mirror of the backend currency→market map (kept tiny + local).
function marketForCurrency(currency: string): string {
  return (
    { USD: "US", HKD: "HK", CNY: "CN", KRW: "KR" }[currency] ?? currency
  );
}

function CurveChart({
  points,
  currency,
}: {
  points: CurvePoint[];
  currency: string;
}) {
  const ft = useFinanceT();
  const svgRef = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);

  const W = 720;
  const H = 200;
  const M = { top: 16, right: 16, bottom: 30, left: 64 };
  const innerW = W - M.left - M.right;
  const innerH = H - M.top - M.bottom;

  const values = points.map((p) => p.value);
  const rawMax = Math.max(...values, 0);
  // y-axis anchored at 0 for absolute perspective; headroom above the peak.
  const yMin = Math.min(0, ...values);
  const yMax = rawMax + (rawMax || 1) * 0.08;
  const yRange = yMax - yMin || 1;

  const px = (i: number) => M.left + (i / (values.length - 1)) * innerW;
  const py = (v: number) => M.top + (1 - (v - yMin) / yRange) * innerH;

  const linePts = values
    .map((v, i) => `${px(i).toFixed(1)},${py(v).toFixed(1)}`)
    .join(" ");
  const base = (M.top + innerH).toFixed(1);
  const areaPts = `${px(0).toFixed(1)},${base} ${linePts} ${px(
    values.length - 1,
  ).toFixed(1)},${base}`;

  const N_Y = 4;
  const yTicks = Array.from(
    { length: N_Y + 1 },
    (_, k) => yMin + (k / N_Y) * yRange,
  );
  const last = values.length - 1;
  const xIdx = Array.from(
    new Set([0, Math.round(last / 3), Math.round((2 * last) / 3), last]),
  );

  const delta = values[last] - values[0];

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const localX = ((e.clientX - rect.left) / rect.width) * W;
    const frac = (localX - M.left) / innerW;
    const idx = Math.max(0, Math.min(last, Math.round(frac * last)));
    setHover(idx);
  };

  const hv = hover !== null ? points[hover] : null;
  const fillId = `equity-fill-${currency}`;

  return (
    <div className="flex flex-col gap-2">
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        className="w-full text-primary"
        style={{ aspectRatio: `${W} / ${H}` }}
        role="img"
        aria-label={`${ft.account.equityCurve} · ${currency}`}
        onMouseMove={onMove}
        onMouseLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={fillId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="currentColor" stopOpacity="0.16" />
            <stop offset="100%" stopColor="currentColor" stopOpacity="0" />
          </linearGradient>
        </defs>

        {/* horizontal gridlines + y-axis (value) labels */}
        {yTicks.map((tv, k) => (
          <g key={`y${k}`} className="text-muted-foreground">
            <line
              x1={M.left}
              x2={W - M.right}
              y1={py(tv)}
              y2={py(tv)}
              stroke="currentColor"
              strokeOpacity={0.18}
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
            <text
              x={M.left - 10}
              y={py(tv)}
              dy="0.32em"
              textAnchor="end"
              fill="currentColor"
              className="font-mono-ui"
              fontSize={11}
            >
              {fmtMoney(tv)}
            </text>
          </g>
        ))}

        {/* x-axis (time) labels */}
        {xIdx.map((i) => (
          <text
            key={`x${i}`}
            x={px(i)}
            y={H - 10}
            textAnchor={i === 0 ? "start" : i === last ? "end" : "middle"}
            fill="currentColor"
            className="font-mondwest text-muted-foreground"
            fontSize={11}
          >
            {fmtTs(points[i].ts)}
          </text>
        ))}

        {/* area + equity line */}
        <polygon points={areaPts} fill={`url(#${fillId})`} stroke="none" />
        <polyline
          points={linePts}
          fill="none"
          stroke="currentColor"
          strokeWidth={2}
          strokeLinejoin="round"
          strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
        />

        {/* hover crosshair + marker */}
        {hv && hover !== null && (
          <g>
            <line
              x1={px(hover)}
              x2={px(hover)}
              y1={M.top}
              y2={M.top + innerH}
              stroke="currentColor"
              strokeOpacity={0.35}
              strokeWidth={1}
              vectorEffect="non-scaling-stroke"
            />
            <circle cx={px(hover)} cy={py(hv.value)} r={5} fill="none"
              stroke="currentColor" strokeWidth={2} />
            <circle cx={px(hover)} cy={py(hv.value)} r={2} fill="currentColor" />
          </g>
        )}
      </svg>

      {/* readout: the hovered point, else start → end with the net delta */}
      <div className="flex justify-between font-mondwest normal-case text-xs text-text-tertiary">
        {hv ? (
          <>
            <span>{fmtTs(hv.ts)}</span>
            <span className="text-foreground">
              {ft.account.equity}: {fmtMoney(hv.value)} {currency}
            </span>
          </>
        ) : (
          <>
            <span>{fmtTs(points[0].ts)}</span>
            <span className={pnlClass(delta)}>
              {fmtSigned(delta)} {currency}
            </span>
            <span>{fmtTs(points[last].ts)}</span>
          </>
        )}
      </div>
    </div>
  );
}

// Per-market trade performance — each market in its OWN currency, never a
// single blended win rate/P&L (Loop.md §5.10). Self-contained fetch (server
// defaults to its mode), refreshed with the surrounding account view.
function MarketPerformanceCard({ reloadToken }: { reloadToken: number }) {
  const ft = useFinanceT();
  const a = ft.account;
  const [rows, setRows] = useState<FinanceMarketPerformance[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .financeStatsByMarket()
      .then((r) => {
        if (!cancelled) setRows(r);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadToken]);

  const withTrades = (rows ?? []).filter(
    (r) => r.stats.n_closed > 0 || r.n_open > 0,
  );

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <TrendingUp className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{a.perMarketTitle}</CardTitle>
        </div>
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {a.perMarketHint}
        </p>
      </CardHeader>
      <CardContent>
        {withTrades.length === 0 ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {a.perMarketEmpty}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mondwest normal-case text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-4 font-medium">{a.colMarket}</th>
                  <th className="text-right py-2 px-4 font-medium">{a.colWinRate}</th>
                  <th className="text-right py-2 px-4 font-medium">{a.colClosed}</th>
                  <th className="text-right py-2 px-4 font-medium">{a.colPnl}</th>
                  <th className="text-right py-2 px-4 font-medium">{a.colCash}</th>
                  <th className="text-right py-2 pl-4 font-medium">{a.colOpen}</th>
                </tr>
              </thead>
              <tbody>
                {withTrades.map((r) => (
                  <tr
                    key={r.market}
                    className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                  >
                    <td className="py-2 pr-4">
                      <span className="text-foreground">{r.market}</span>{" "}
                      <span className="text-xs text-muted-foreground">
                        {r.currency}
                      </span>
                    </td>
                    <td className="text-right py-2 px-4">
                      {r.stats.n_closed > 0
                        ? `${Math.round(r.stats.win_rate * 100)}%`
                        : "—"}
                    </td>
                    <td className="text-right py-2 px-4">{r.stats.n_closed}</td>
                    <td
                      className={cn(
                        "text-right py-2 px-4",
                        pnlClass(r.stats.total_pnl),
                      )}
                    >
                      {r.stats.n_closed > 0
                        ? `${fmtSigned(r.stats.total_pnl)} ${r.currency}`
                        : "—"}
                    </td>
                    <td className="text-right py-2 px-4 text-muted-foreground">
                      {fmtMoney(r.cash)} {r.currency}
                    </td>
                    <td className="text-right py-2 pl-4">{r.n_open}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AccountSection({
  numbers,
  stats,
  snapshots,
  ledgerFallback,
  reloadToken,
}: {
  numbers: AccountNumbers | null;
  stats: FinanceStats | null;
  snapshots: FinanceSnapshot[];
  ledgerFallback: boolean;
  reloadToken: number;
}) {
  const ft = useFinanceT();
  return (
    <div className="flex flex-col gap-6">
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Wallet className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{ft.account.title}</CardTitle>
            {ledgerFallback && (
              <Badge tone="outline">{ft.account.ledgerFallback}</Badge>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {numbers === null ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              {stats !== null && stats.n_closed > 0
                ? ft.account.emptyWithStats
                : ft.account.empty}
            </p>
          ) : (
            <div className="space-y-4">
              {/* Per-currency balances — each sleeve on its own row, never
                  blended into one base-currency number. */}
              <div className="flex flex-col gap-2">
                {Array.from(
                  new Set([
                    ...Object.keys(numbers.equity_by_currency ?? {}),
                    ...Object.keys(numbers.cash_by_currency ?? {}),
                  ]),
                )
                  .sort()
                  .map((cur) => (
                    <div
                      key={cur}
                      className="flex items-center justify-between gap-4 border-b border-border/40 pb-2 text-sm"
                    >
                      <span className="font-mono-ui text-xs text-muted-foreground">
                        {cur}
                      </span>
                      <div className="flex gap-6">
                        <span className="text-muted-foreground">
                          {ft.account.equity}{" "}
                          <span className="text-foreground">
                            {fmtMoney(numbers.equity_by_currency?.[cur] ?? 0)}
                          </span>
                        </span>
                        <span className="text-muted-foreground">
                          {ft.account.cash}{" "}
                          <span className="text-foreground">
                            {fmtMoney(numbers.cash_by_currency?.[cur] ?? 0)}
                          </span>
                        </span>
                      </div>
                    </div>
                  ))}
              </div>
              {/* Portfolio-level risk metrics (base-currency aggregate). */}
              <Stats
                items={[
                  {
                    label: ft.account.upnl,
                    value: {
                      key: "upnl",
                      node: (
                        <span className={pnlClass(numbers.upnl)}>
                          {fmtSigned(numbers.upnl)}
                        </span>
                      ),
                    },
                  },
                  {
                    label: ft.account.dayPnl,
                    value: {
                      key: "day_pnl",
                      node: (
                        <span className={pnlClass(numbers.day_pnl)}>
                          {fmtSigned(numbers.day_pnl)}
                        </span>
                      ),
                    },
                  },
                  {
                    label: ft.account.drawdown,
                    value: fmtPct(numbers.drawdown_pct),
                  },
                ]}
              />
              <p className="font-mondwest normal-case text-xs text-text-tertiary">
                {ft.account.baseTotal}: {fmtMoney(numbers.equity)}{" "}
                {numbers.base_currency}
              </p>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {ft.account.equityCurve}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <EquityCurve snapshots={snapshots} live={numbers} />
        </CardContent>
      </Card>
    </div>
    <MarketPerformanceCard reloadToken={reloadToken} />
    </div>
  );
}

function PositionsCard({ view }: { view: FinanceAccountView | null }) {
  const ft = useFinanceT();
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">{ft.positions.title}</CardTitle>
          {view && view.positions.length > 0 && (
            <Badge tone={view.marks_live ? "success" : "secondary"}>
              {view.marks_live
                ? ft.positions.marksLive
                : ft.positions.marksClose.replace(
                    "{time}",
                    view.marks_as_of ? fmtTs(view.marks_as_of) : "—",
                  )}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {view === null ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.positions.loopOnly}
          </p>
        ) : view.positions.length === 0 ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.positions.empty}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mondwest normal-case text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-4 font-medium">
                    {ft.positions.symbol}
                  </th>
                  <th className="text-left py-2 px-4 font-medium">
                    {ft.positions.market}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.positions.qty}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.positions.avgPx}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.positions.mktPx}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.positions.upnl}
                  </th>
                  <th className="text-left py-2 pl-4 font-medium">
                    {ft.positions.role}
                  </th>
                </tr>
              </thead>
              <tbody>
                {view.positions.map((p) => (
                  <tr
                    key={p.symbol}
                    className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                  >
                    <td className="py-2 pr-4">
                      <span className="font-mono-ui text-xs">{p.symbol}</span>
                    </td>
                    <td className="py-2 px-4">
                      <Badge tone="outline">{p.market || p.currency}</Badge>
                    </td>
                    <td className="text-right py-2 px-4">{fmtQty(p.qty)}</td>
                    <td className="text-right py-2 px-4">
                      {fmtMoney(p.avg_px)}
                    </td>
                    <td className="text-right py-2 px-4">
                      {fmtMoney(p.mkt_px)}
                    </td>
                    <td
                      className={cn("text-right py-2 px-4", pnlClass(p.upnl))}
                    >
                      {fmtSigned(p.upnl)}{" "}
                      <span className="text-xs text-muted-foreground">
                        {p.currency}
                      </span>
                    </td>
                    <td className="py-2 pl-4">
                      <Badge tone="secondary">{p.pool}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function OrdersCard({ view }: { view: FinanceAccountView | null }) {
  const ft = useFinanceT();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{ft.orders.title}</CardTitle>
      </CardHeader>
      <CardContent>
        {view === null ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.orders.loopOnly}
          </p>
        ) : view.open_orders.length === 0 ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.orders.empty}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mondwest normal-case text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-4 font-medium">
                    {ft.orders.symbol}
                  </th>
                  <th className="text-left py-2 pr-4 font-medium">
                    {ft.orders.side}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.orders.qty}
                  </th>
                  <th className="text-left py-2 px-4 font-medium">
                    {ft.orders.type}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.orders.limit}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {ft.orders.stop}
                  </th>
                  <th className="text-left py-2 pl-4 font-medium">
                    {ft.orders.status}
                  </th>
                </tr>
              </thead>
              <tbody>
                {view.open_orders.map((o: FinanceOpenOrder, i) => (
                  <tr
                    key={`${o.symbol}-${o.side}-${i}`}
                    className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                  >
                    <td className="py-2 pr-4">
                      <span className="font-mono-ui text-xs">{o.symbol}</span>
                    </td>
                    <td className="py-2 pr-4">
                      <Badge tone={sideTone(o.side)}>{o.side}</Badge>
                    </td>
                    <td className="text-right py-2 px-4">{fmtQty(o.qty)}</td>
                    <td className="py-2 px-4 text-muted-foreground">
                      {o.order_type}
                    </td>
                    <td className="text-right py-2 px-4">
                      {fmtMoney(o.limit)}
                    </td>
                    <td className="text-right py-2 px-4">{fmtMoney(o.stop)}</td>
                    <td className="py-2 pl-4">
                      <Badge tone="outline">{o.status}</Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PositionDetail({
  position,
  ft,
}: {
  position: FinancePosition | null;
  ft: FinanceTranslations;
}) {
  if (position === null) {
    return (
      <Card>
        <CardContent className="py-8">
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.layout.selectPositionHint}
          </p>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base font-mono-ui">
            {position.symbol}
          </CardTitle>
          <Badge tone="secondary">{position.pool}</Badge>
        </div>
      </CardHeader>
      <CardContent>
        <Stats
          items={[
            { label: ft.positions.qty, value: fmtQty(position.qty) },
            { label: ft.positions.avgPx, value: fmtMoney(position.avg_px) },
            { label: ft.positions.mktPx, value: fmtMoney(position.mkt_px) },
            {
              label: ft.positions.upnl,
              value: {
                key: "upnl",
                node: (
                  <span className={pnlClass(position.upnl)}>
                    {fmtSigned(position.upnl)}
                  </span>
                ),
              },
            },
          ]}
        />
      </CardContent>
    </Card>
  );
}

function MarketStrip({
  market,
  watchlist,
}: {
  market: FinanceMarketSnapshot | null;
  watchlist: FinanceWatchlistItem[];
}) {
  const ft = useFinanceT();
  const { t } = useI18n();
  const byRole = useMemo(() => {
    const groups = new Map<string, FinanceWatchlistItem[]>();
    for (const item of watchlist) {
      const arr = groups.get(item.role) ?? [];
      arr.push(item);
      groups.set(item.role, arr);
    }
    return [...groups.entries()];
  }, [watchlist]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Globe className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.market.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {market !== null && market.status === undefined ? (
          <div className="flex flex-wrap items-center gap-3 font-mondwest normal-case text-sm">
            <Badge tone={regimeTone(market.risk_on_off)}>
              {market.risk_on_off ?? "—"}
            </Badge>
            {market.source === "research_brief" && (
              <Badge tone="outline">{ft.market.persistedBrief}</Badge>
            )}
            <span className="text-muted-foreground">
              {ft.market.vix}{" "}
              <span className="text-foreground">
                {market.vix === null || market.vix === undefined
                  ? "—"
                  : market.vix.toFixed(1)}
              </span>
            </span>
            <span className="text-muted-foreground">
              {ft.market.breadth}{" "}
              <span className="text-foreground">
                {fmtPct(market.breadth_pct_above_50dma, 0)}
              </span>
            </span>
            {market.ts && (
              <span className="text-xs text-text-tertiary">
                {ft.market.asOf.replace("{time}", fmtTs(market.ts))}
              </span>
            )}
          </div>
        ) : (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.market.noSnapshot}
          </p>
        )}

        {byRole.length > 0 && (
          <div className="flex flex-col gap-2">
            {byRole.map(([role, items]) => (
              <div key={role} className="flex flex-wrap items-center gap-1.5">
                <span className="w-20 shrink-0 text-xs uppercase text-text-tertiary">
                  {role}
                </span>
                {items.map((item) => (
                  <span
                    key={item.symbol}
                    title={`${item.theme} · ${item.ai_phase}${item.enabled ? "" : ` · ${t.common.disabled}`}`}
                    className={cn(
                      "border border-border px-1.5 py-0.5 font-mono-ui text-xs text-foreground",
                      !item.enabled && "opacity-40",
                    )}
                  >
                    {item.symbol}
                  </span>
                ))}
              </div>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReportsCard({ reports }: { reports: FinanceReports }) {
  const ft = useFinanceT();
  // Prefer the morning report; fall back to whatever the loop produced last.
  const entry =
    reports.morning !== undefined
      ? (["morning", reports.morning] as const)
      : Object.entries(reports)[0];
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Newspaper className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {ft.reports.title}
            {entry ? ` — ${entry[0]}` : ""}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {entry === undefined ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            {ft.reports.empty}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <pre className="whitespace-pre-wrap font-mono-ui text-xs text-foreground">
              {entry[1]}
            </pre>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function OfflinePanel() {
  const ft = useFinanceT();
  return (
    <Card>
      <CardContent className="py-12">
        <div className="mx-auto flex max-w-xl flex-col items-center gap-4 text-center">
          <PlugZap className="h-8 w-8 text-muted-foreground opacity-40" />
          <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
            {ft.page.serviceOfflineTitle}
          </h2>
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.page.serviceOfflineBody}
          </p>
          <CommandBlock
            label={ft.page.serviceOfflineStartLabel}
            code={SERVICE_START_COMMAND}
          />
        </div>
      </CardContent>
    </Card>
  );
}

// ── Research master-detail ────────────────────────────────────────────

function ResearchDetail({
  desk,
  customGroup,
  onCustomChange,
  onCustomDelete,
  briefs,
  onRunResearch,
  researchRunning,
}: {
  desk: FinanceDesk;
  customGroup: FinanceResearchWatchlist | null;
  onCustomChange: (group: FinanceResearchWatchlist) => void;
  onCustomDelete: (id: string) => void;
  briefs: {
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
    hk: FinanceResearchBriefData | null;
    kr: FinanceResearchBriefData | null;
  };
  onRunResearch?: () => void;
  researchRunning?: boolean;
}) {
  if (customGroup) {
    return (
      <ResearchWatchlistDetail
        key={customGroup.id}
        group={customGroup}
        onChange={onCustomChange}
        onDelete={onCustomDelete}
      />
    );
  }
  if (isWatchDesk(desk)) {
    // Keyed by desk so switching watch modules remounts with a fresh selection
    // + analysis state (no reset effect needed).
    return <WatchModule key={desk} moduleKey={desk} />;
  }
  if (desk === "predictions") {
    return <PredictionReview />;
  }
  if (desk === "backtests") {
    return <StrategyBacktests />;
  }
  if (desk === "us") {
    return (
      <ResearchBrief
        brief={briefs.us}
        market="us"
        onRunResearch={onRunResearch}
        researchRunning={researchRunning}
      />
    );
  }
  if (desk === "korea") {
    // KR is its own single-region semiconductor brief (no CN-style partition).
    return (
      <ResearchBrief
        brief={briefs.kr}
        market="kr"
        onRunResearch={onRunResearch}
        researchRunning={researchRunning}
      />
    );
  }
  const regionalBrief = desk === "hk" ? briefs.hk : briefs.cn;
  return (
    <ResearchBrief
      brief={regionalBrief}
      market={desk === "hk" ? "hk" : "cn"}
      onRunResearch={onRunResearch}
      researchRunning={researchRunning}
    />
  );
}

function ResearchView({
  desk,
  customGroupId,
  onDeskChange,
  onCustomGroupChange,
  briefs,
  ft,
  onRunResearch,
  researchRunning,
}: {
  desk: FinanceDesk;
  customGroupId: string | null;
  onDeskChange: (desk: FinanceDesk) => void;
  onCustomGroupChange: (id: string | null) => void;
  briefs: {
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
    hk: FinanceResearchBriefData | null;
    kr: FinanceResearchBriefData | null;
  };
  ft: FinanceTranslations;
  onRunResearch: () => void;
  researchRunning: boolean;
}) {
  const { t } = useI18n();
  const [customGroups, setCustomGroups] = useState<FinanceResearchWatchlist[]>(
    [],
  );
  const [creatingGroup, setCreatingGroup] = useState(false);
  const [newGroupName, setNewGroupName] = useState("");
  const [groupBusy, setGroupBusy] = useState(false);
  const selectedCustomGroup =
    customGroups.find((group) => group.id === customGroupId) ?? null;

  useEffect(() => {
    api.financeResearchWatchlists().then(setCustomGroups, () => {});
  }, []);

  const createCustomGroup = async () => {
    if (!newGroupName.trim()) return;
    setGroupBusy(true);
    try {
      const created = await api.financeCreateResearchWatchlist(newGroupName);
      setCustomGroups((groups) => [...groups, created]);
      setNewGroupName("");
      setCreatingGroup(false);
      onCustomGroupChange(created.id);
    } finally {
      setGroupBusy(false);
    }
  };

  const updateCustomGroup = (updated: FinanceResearchWatchlist) =>
    setCustomGroups((groups) =>
      groups.map((group) => (group.id === updated.id ? updated : group)),
    );
  const deleteCustomGroup = (id: string) => {
    setCustomGroups((groups) => groups.filter((group) => group.id !== id));
    onCustomGroupChange(null);
  };

  // Every active market has a registered read-only research session, including
  // US. Watch/custom/review desks do not expose the refresh action.
  const canRun = !selectedCustomGroup && isActiveMarketDesk(desk);
  const sidebar = (
    <>
      <SidebarGroup label={ft.layout.marketsGroup}>
        {ACTIVE_MARKETS.map((m) => (
          <SidebarButton
            key={m}
            active={!customGroupId && desk === m}
            onClick={() => onDeskChange(m)}
          >
            {marketName(m, ft)}
          </SidebarButton>
        ))}
        {PLACEHOLDER_MARKETS.map((m) => (
          <SidebarButton
            key={m}
            active={false}
            disabled
            trailing={<Badge tone="outline">{ft.layout.comingSoon}</Badge>}
          >
            {marketName(m, ft)}
          </SidebarButton>
        ))}
      </SidebarGroup>
      <SidebarGroup label={ft.prediction.groupLabel}>
        <SidebarButton
          active={!customGroupId && desk === "predictions"}
          onClick={() => onDeskChange("predictions")}
        >
          {ft.prediction.navLabel}
        </SidebarButton>
        <SidebarButton
          active={!customGroupId && desk === "backtests"}
          onClick={() => onDeskChange("backtests")}
        >
          {ft.prediction.backtests}
        </SidebarButton>
      </SidebarGroup>
      <SidebarGroup label={ft.layout.watchGroup}>
        {WATCH_MODULE_KEYS.map((k) => (
          <SidebarButton
            key={k}
            active={!customGroupId && desk === k}
            onClick={() => onDeskChange(k)}
          >
            {watchModuleName(k, ft)}
          </SidebarButton>
        ))}
      </SidebarGroup>
      <SidebarGroup label={ft.watch.customGroup}>
        {customGroups.map((group) => (
          <SidebarButton
            key={group.id}
            active={customGroupId === group.id}
            onClick={() => onCustomGroupChange(group.id)}
          >
            {group.name}
          </SidebarButton>
        ))}
        {creatingGroup ? (
          <div className="flex flex-col gap-2 border-t border-border p-2">
            <Input
              autoFocus
              value={newGroupName}
              placeholder={ft.watch.groupNamePlaceholder}
              onChange={(event) => setNewGroupName(event.target.value)}
              onKeyDown={(event) =>
                event.key === "Enter" && void createCustomGroup()
              }
            />
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={groupBusy || !newGroupName.trim()}
                onClick={() => void createCustomGroup()}
              >
                {t.common.create}
              </Button>
              <Button size="sm" ghost onClick={() => setCreatingGroup(false)}>
                {t.common.cancel}
              </Button>
            </div>
          </div>
        ) : (
          <SidebarButton
            active={false}
            onClick={() => {
              setNewGroupName(ft.watch.newCustomGroup);
              setCreatingGroup(true);
            }}
          >
            {ft.watch.newCustomGroup}
          </SidebarButton>
        )}
      </SidebarGroup>
    </>
  );
  return (
    <MasterDetail sidebar={sidebar}>
      <div className="flex flex-col gap-3">
        <ResearchDetail
          desk={desk}
          customGroup={selectedCustomGroup}
          onCustomChange={updateCustomGroup}
          onCustomDelete={deleteCustomGroup}
          briefs={briefs}
          onRunResearch={canRun ? onRunResearch : undefined}
          researchRunning={researchRunning}
        />
      </div>
    </MasterDetail>
  );
}

// ── Action-queue master-detail ────────────────────────────────────────

function QueueView({
  pending,
  selectedId,
  onSelect,
  onActed,
  showToast,
  ft,
  emptyForMode,
  modeLabel,
}: {
  pending: FinancePendingCandidate[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onActed: () => void;
  showToast: (message: string, type: "error" | "success") => void;
  ft: FinanceTranslations;
  emptyForMode: boolean;
  modeLabel: string;
}) {
  const selected = pending.find((p) => p.candidate.id === selectedId) ?? null;
  const emptyNote = emptyForMode
    ? ft.layout.queueNoneForMode.replace("{mode}", modeLabel)
    : ft.queue.noPending;

  const sidebar = (
    <SidebarGroup label={ft.layout.queueSidebarTitle}>
      {pending.length === 0 ? (
        <p className="px-3 py-3 font-mondwest normal-case text-sm text-muted-foreground">
          {emptyNote}
        </p>
      ) : (
        pending.map((pc) => (
          <SidebarButton
            key={pc.candidate.id}
            active={selectedId === pc.candidate.id}
            onClick={() => onSelect(pc.candidate.id)}
            trailing={
              <Badge tone={sideTone(pc.candidate.side)}>
                {pc.candidate.side}
              </Badge>
            }
            subtitle={`${fmtQty(pc.candidate.qty)} · ${pc.candidate.status}`}
          >
            <span className="font-mono-ui">{pc.candidate.symbol}</span>
          </SidebarButton>
        ))
      )}
    </SidebarGroup>
  );

  return (
    <MasterDetail sidebar={sidebar}>
      <div className="flex flex-col gap-4">
        {/* Manual catch-up for a missed session — run monitor→decide→push now,
            then finalize the human-approved candidates (Loop.md §5.6). */}
        <SessionControls onRan={onActed} showToast={showToast} />
        {selected !== null ? (
          <ApprovalQueue
            pending={[selected]}
            onActed={onActed}
            showToast={showToast}
          />
        ) : (
          <Card>
            <CardContent className="py-8">
              <p className="font-mondwest normal-case text-sm text-muted-foreground">
                {pending.length === 0 ? emptyNote : ft.layout.queueSelectHint}
              </p>
            </CardContent>
          </Card>
        )}
      </div>
    </MasterDetail>
  );
}

// ── Portfolio master-detail ───────────────────────────────────────────

function PortfolioView({
  selected,
  onSelect,
  numbers,
  stats,
  snapshots,
  ledgerFallback,
  liveView,
  market,
  watchlist,
  reports,
  candidates,
  fills,
  ft,
}: {
  selected: string;
  onSelect: (view: string) => void;
  numbers: AccountNumbers | null;
  stats: FinanceStats | null;
  snapshots: FinanceSnapshot[];
  ledgerFallback: boolean;
  liveView: FinanceAccountView | null;
  market: FinanceMarketSnapshot | null;
  watchlist: FinanceWatchlistItem[];
  reports: FinanceReports;
  candidates: FinanceCandidate[];
  fills: FinanceFill[];
  ft: FinanceTranslations;
}) {
  const positions = liveView?.positions ?? [];
  const accountRows: { view: PortfolioView; label: string }[] = [
    { view: "account", label: ft.layout.rowAccount },
    { view: "controls", label: ft.layout.rowControls },
    { view: "orders", label: ft.layout.rowOrders },
    { view: "history", label: ft.layout.rowHistory },
    { view: "market", label: ft.layout.rowMarket },
    { view: "reports", label: ft.layout.rowReports },
  ];

  const sidebar = (
    <>
      <SidebarGroup label={ft.layout.portfolioAccountGroup}>
        {accountRows.map((row) => (
          <SidebarButton
            key={row.view}
            active={selected === row.view}
            onClick={() => onSelect(row.view)}
          >
            {row.label}
          </SidebarButton>
        ))}
      </SidebarGroup>
      <SidebarGroup label={ft.layout.portfolioPositionsGroup}>
        {positions.length === 0 ? (
          <p className="px-3 py-3 font-mondwest normal-case text-sm text-muted-foreground">
            {ft.layout.positionsEmpty}
          </p>
        ) : (
          positions.map((p) => (
            <SidebarButton
              key={p.symbol}
              active={selected === `pos:${p.symbol}`}
              onClick={() => onSelect(`pos:${p.symbol}`)}
              trailing={
                <span className={cn("text-xs", pnlClass(p.upnl))}>
                  {fmtSigned(p.upnl)}
                </span>
              }
            >
              <span className="font-mono-ui">{p.symbol}</span>
            </SidebarButton>
          ))
        )}
      </SidebarGroup>
    </>
  );

  let detail: React.ReactNode;
  if (selected.startsWith("pos:")) {
    const symbol = selected.slice(4);
    const position = positions.find((p) => p.symbol === symbol) ?? null;
    detail = <PositionDetail position={position} ft={ft} />;
  } else {
    switch (selected as PortfolioView) {
      case "orders":
        detail = <OrdersCard view={liveView} />;
        break;
      case "controls":
        detail = <PortfolioControls />;
        break;
      case "history":
        detail = (
          <HistorySection candidates={candidates} fills={fills} stats={stats} />
        );
        break;
      case "market":
        detail = <MarketStrip market={market} watchlist={watchlist} />;
        break;
      case "reports":
        detail = <ReportsCard reports={reports} />;
        break;
      case "account":
      default:
        detail = (
          <div className="flex flex-col gap-6">
            <AccountSection
              numbers={numbers}
              stats={stats}
              snapshots={snapshots}
              ledgerFallback={ledgerFallback}
              reloadToken={snapshots.length}
            />
            <PositionsCard view={liveView} />
          </div>
        );
        break;
    }
  }

  return <MasterDetail sidebar={sidebar}>{detail}</MasterDetail>;
}

// ── Page ──────────────────────────────────────────────────────────────

export default function FinancePage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [health, setHealth] = useState<FinanceHealth | null>(null);
  const [offline, setOffline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [briefs, setBriefs] = useState<{
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
    hk: FinanceResearchBriefData | null;
    kr: FinanceResearchBriefData | null;
  }>({ us: null, cn: null, hk: null, kr: null });
  const [account, setAccount] = useState<FinanceAccountResponse | null>(null);
  const [snapshots, setSnapshots] = useState<FinanceSnapshot[]>([]);
  const [market, setMarket] = useState<FinanceMarketSnapshot | null>(null);
  const [watchlist, setWatchlist] = useState<FinanceWatchlistItem[]>([]);
  const [reports, setReports] = useState<FinanceReports>({});
  const [pending, setPending] = useState<FinancePendingCandidate[]>([]);
  const [candidates, setCandidates] = useState<FinanceCandidate[]>([]);
  const [fills, setFills] = useState<FinanceFill[]>([]);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  // Paper/live override. `null` follows the service /health.mode.
  const [modeOverride, setModeOverride] = useState<FinanceMode | null>(null);
  // Holdings account scope is not the trading service mode. Default to live
  // money so the page never opens on a simulation by accident.
  const [holdingsEnvironment, setHoldingsEnvironment] =
    useState<FinanceMode>("live");
  // Local (non-URL) selections for the Queue and Portfolio views.
  const [queueSel, setQueueSel] = useState<string | null>(null);
  const [portfolioSel, setPortfolioSel] = useState<string>("account");
  const { toast, showToast } = useToast();
  const ft = useFinanceT();

  // ── URL-persisted top tab + research desk ──
  const tabParam = searchParams.get("tab");
  const activeTab: FinanceTab = TABS.includes(tabParam as FinanceTab)
    ? (tabParam as FinanceTab)
    : "research";
  const deskParam = searchParams.get("desk");
  const customGroupId = searchParams.get("watch_group");
  const researchDesk: FinanceDesk =
    DESKS.includes(deskParam as FinanceDesk) &&
    // Disabled placeholders are never a valid selection.
    (isActiveMarketDesk(deskParam as FinanceDesk) ||
      isWatchDesk(deskParam as FinanceDesk) ||
      RESEARCH_TOOL_DESKS.includes(deskParam as FinanceDesk))
      ? (deskParam as FinanceDesk)
      : "us";

  const setParam = useCallback(
    (key: string, value: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set(key, value);
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const setActiveTab = useCallback(
    (tab: FinanceTab) => setParam("tab", tab),
    [setParam],
  );
  const setResearchDesk = useCallback(
    (desk: FinanceDesk) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          next.set("desk", desk);
          next.delete("watch_group");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );
  const setCustomGroupId = useCallback(
    (id: string | null) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev);
          if (id) next.set("watch_group", id);
          else next.delete("watch_group");
          return next;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // The brief the Research desk needs: US, the shared CN brief, or none
  // (watch modules). Threaded into the loader so switching desks refetches.
  const briefMarket: "us" | "cn" | "hk" | "kr" | null =
    researchDesk === "us"
      ? "us"
      : researchDesk === "china"
        ? "cn"
        : researchDesk === "hk"
          ? "hk"
          : researchDesk === "korea"
            ? "kr"
            : null;
  // Mode threaded into the mode-scoped read endpoints. `undefined` lets the
  // service pick (follows /health.mode); an explicit override forces one.
  const modeParam: FinanceMode | undefined = modeOverride ?? undefined;

  // No synchronous setState in load itself: `loading` starts true and the
  // manual refresh button flips it back on before invoking load(), so the
  // 30s background refresh never flashes the whole-page spinner.
  const load = useCallback(() => {
    api
      .financeHealth()
      .then(async (h) => {
        setHealth(h);
        setOffline(false);
        const [rb, rest] = await Promise.all([
          briefMarket === null
            ? Promise.resolve(null)
            : api.financeResearchBrief(briefMarket).then(
                (v) => ({ ok: true as const, market: briefMarket, v }),
                () => ({ ok: false as const, market: briefMarket }),
              ),
          // Account / queue / portfolio material, mode-scoped. allSettled so
          // one degraded endpoint never blanks the rest of the page.
          Promise.allSettled([
            api.financeAccount(modeParam),
            api.financeSnapshots(SNAPSHOT_LIMIT, modeParam),
            api.financeMarket(),
            api.financeWatchlist(),
            api.financeLatestReports(),
            api.financePendingCandidates(),
            api.financeCandidates(undefined, modeParam),
            api.financeFills(modeParam),
          ]),
        ]);
        if (rb !== null && rb.ok) {
          setBriefs((b) => ({ ...b, [rb.market]: rb.v }));
        }
        const [acct, snaps, mkt, wl, rep, pend, cands, fl] = rest;
        if (acct.status === "fulfilled") setAccount(acct.value);
        if (snaps.status === "fulfilled") setSnapshots(snaps.value);
        if (mkt.status === "fulfilled") setMarket(mkt.value);
        if (wl.status === "fulfilled") setWatchlist(wl.value);
        if (rep.status === "fulfilled") setReports(rep.value);
        if (pend.status === "fulfilled") setPending(pend.value);
        if (cands.status === "fulfilled") setCandidates(cands.value);
        if (fl.status === "fulfilled") setFills(fl.value);
        setLastUpdated(new Date());
      })
      .catch(() => setOffline(true))
      .finally(() => setLoading(false));
  }, [briefMarket, modeParam]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  // Manual refresh, shared by the bottom utility bar. Flips `loading` on so the
  // refresh control shows its spinner; the 30s background refresh never does
  // (it leaves `loading` false), so the whole-page spinner never flashes.
  const refresh = useCallback(() => {
    setLoading(true);
    load();
  }, [load]);

  // Manual "run research now" for the current market desk. Every active
  // market, including US, has a registered read-only research callback.
  const [researchRunning, setResearchRunning] = useState(false);
  const runResearch = useCallback(async () => {
    if (briefMarket === null) return;
    setResearchRunning(true);
    try {
      // Fires a BACKGROUND refresh (a full run does slow yfinance calls, ~1
      // min for KR — far over the proxy's 15s timeout); returns immediately.
      // The brief updates via the ongoing 30s poll when the run completes.
      await api.financeRunResearch(briefMarket);
      showToast(ft.layout.runResearchDone, "success");
    } catch (err) {
      showToast(
        ft.layout.runResearchFailed.replace("{error}", String(err)),
        "error",
      );
    } finally {
      setResearchRunning(false);
    }
  }, [briefMarket, showToast, ft]);

  const ledgerFallback = account !== null && "source" in account;
  const liveView = account !== null && !("source" in account) ? account : null;
  const accountNumbers: AccountNumbers | null =
    account === null ? null : "source" in account ? account.snapshot : account;
  const stats = account?.stats ?? null;

  // Effective mode (for the switcher + the queue filter) and the service
  // mode it defaults to.
  const serviceMode: FinanceMode | null = health?.mode ?? null;
  const effectiveMode: FinanceMode = modeOverride ?? serviceMode ?? "paper";
  const modeLabelText =
    effectiveMode === "live" ? ft.layout.modeLive : ft.layout.modePaper;
  // Pending candidates belong to the service's mode (they carry none of their
  // own). The bottom switcher only filters/labels: viewing the other mode
  // shows an empty queue, it never re-modes an action.
  const modeMatchesService =
    serviceMode === null || effectiveMode === serviceMode;
  const queuePending = modeMatchesService ? pending : [];

  // Single paper/live toggle: flip to the other mode as an explicit override.
  // Filter-only — it re-scopes reads, never re-modes an action (Loop.md §3).
  const toggleMode = () =>
    activeTab === "holdings"
      ? setHoldingsEnvironment(
          holdingsEnvironment === "paper" ? "live" : "paper",
        )
      : setModeOverride(effectiveMode === "paper" ? "live" : "paper");

  const displayedMode =
    activeTab === "holdings" ? holdingsEnvironment : effectiveMode;

  const bottomBar = (
    <FinanceBottomBar
      health={health}
      offline={offline}
      lastUpdated={lastUpdated}
      loading={loading}
      onRefresh={refresh}
      mode={displayedMode}
      serviceMode={serviceMode}
      onToggleMode={toggleMode}
    />
  );

  if (loading && health === null && !offline) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  if (offline) {
    return (
      <div className="flex flex-col gap-6">
        <OfflinePanel />
        {bottomBar}
        <Toast toast={toast} />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Account-risk breaker banner (Loop.md §3). */}
      {health?.breaker === "TRIPPED" && (
        <div className="flex items-center gap-3 border border-destructive bg-destructive/10 px-4 py-3 text-destructive">
          <AlertTriangle className="h-5 w-5 shrink-0" />
          <div className="font-mondwest normal-case text-sm">
            <span className="font-semibold">{ft.page.breakerTrippedTitle}</span>{" "}
            {ft.page.breakerTrippedBody}
          </div>
        </div>
      )}

      {/* Top-level tabs. */}
      <Segmented<FinanceTab>
        value={activeTab}
        onChange={setActiveTab}
        options={[
          { value: "research", label: ft.layout.tabResearch },
          { value: "queue", label: ft.layout.tabQueue },
          { value: "portfolio", label: ft.layout.tabPortfolio },
          { value: "holdings", label: ft.layout.tabHoldings },
        ]}
      />

      {activeTab === "research" && (
        <ResearchView
          desk={researchDesk}
          customGroupId={customGroupId}
          onDeskChange={setResearchDesk}
          onCustomGroupChange={setCustomGroupId}
          briefs={briefs}
          ft={ft}
          onRunResearch={runResearch}
          researchRunning={researchRunning}
        />
      )}

      {activeTab === "queue" && (
        <QueueView
          pending={queuePending}
          selectedId={queueSel}
          onSelect={setQueueSel}
          onActed={load}
          showToast={showToast}
          ft={ft}
          emptyForMode={!modeMatchesService}
          modeLabel={modeLabelText}
        />
      )}

      {activeTab === "portfolio" && (
        <PortfolioView
          selected={portfolioSel}
          onSelect={setPortfolioSel}
          numbers={accountNumbers}
          stats={stats}
          snapshots={snapshots}
          ledgerFallback={ledgerFallback}
          liveView={liveView}
          market={market}
          watchlist={watchlist}
          reports={reports}
          candidates={candidates}
          fills={fills}
          ft={ft}
        />
      )}

      {activeTab === "holdings" && (
        <PortfolioManager
          key={holdingsEnvironment}
          environment={holdingsEnvironment}
        />
      )}

      {bottomBar}

      <Toast toast={toast} />
    </div>
  );
}
