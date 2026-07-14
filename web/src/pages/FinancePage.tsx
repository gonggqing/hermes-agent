import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
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
  FinanceSnapshot,
  FinanceStats,
  FinanceWatchlistItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { CommandBlock } from "@nous-research/ui/ui/components/command-block";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useI18n } from "@/i18n";
import { ApprovalQueue, SessionControls } from "@/pages/finance/ApprovalQueue";
import { HistorySection } from "@/pages/finance/HistorySection";
import { PortfolioManager } from "@/pages/finance/PortfolioManager";
import { ResearchBrief } from "@/pages/finance/ResearchBrief";
import { WatchModule } from "@/pages/finance/WatchModule";
import {
  FinanceBottomBar,
  MasterDetail,
  SidebarButton,
  SidebarGroup,
} from "@/pages/finance/layout";
import {
  ACTIVE_MARKETS,
  PLACEHOLDER_MARKETS,
  WATCH_MODULE_KEYS,
  isActiveMarketDesk,
  isWatchDesk,
  marketName,
  watchModuleName,
  type FinanceDesk,
} from "@/pages/finance/constants";
import { partitionCnBrief } from "@/pages/finance/partition";
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
  ...WATCH_MODULE_KEYS,
];

/** Portfolio sidebar entries that are not per-position rows. */
type PortfolioView = "account" | "orders" | "history" | "market" | "reports";

/** Account numbers shared by the live view and the ledger-fallback snapshot. */
interface AccountNumbers {
  equity: number;
  cash: number;
  upnl: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: string;
}

// ── Account / positions / orders / market / reports sections ──────────

function EquitySparkline({ snapshots }: { snapshots: FinanceSnapshot[] }) {
  const ft = useFinanceT();
  if (snapshots.length < 2) {
    return (
      <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
        {ft.account.notEnoughSnapshots}
      </p>
    );
  }
  const values = snapshots.map((s) => s.equity);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const W = 600;
  const H = 80;
  const PAD = 4;
  const points = values
    .map((v, i) => {
      const x = PAD + (i / (values.length - 1)) * (W - PAD * 2);
      const y = PAD + (1 - (v - min) / range) * (H - PAD * 2);
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
  const delta = values[values.length - 1] - values[0];
  return (
    <div className="flex flex-col gap-1">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="h-20 w-full text-primary"
        role="img"
        aria-label={ft.account.equityCurve}
      >
        <polyline
          points={points}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="flex justify-between font-mondwest normal-case text-xs text-text-tertiary">
        <span>{fmtTs(snapshots[0].ts)}</span>
        <span className={pnlClass(delta)}>{fmtSigned(delta)}</span>
        <span>{fmtTs(snapshots[snapshots.length - 1].ts)}</span>
      </div>
    </div>
  );
}

function AccountSection({
  numbers,
  stats,
  snapshots,
  ledgerFallback,
}: {
  numbers: AccountNumbers | null;
  stats: FinanceStats | null;
  snapshots: FinanceSnapshot[];
  ledgerFallback: boolean;
}) {
  const ft = useFinanceT();
  return (
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
            <Stats
              items={[
                { label: ft.account.equity, value: fmtMoney(numbers.equity) },
                { label: ft.account.cash, value: fmtMoney(numbers.cash) },
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
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{ft.account.equityCurve}</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          <EquitySparkline snapshots={snapshots} />
        </CardContent>
      </Card>
    </div>
  );
}

function PositionsCard({ view }: { view: FinanceAccountView | null }) {
  const ft = useFinanceT();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{ft.positions.title}</CardTitle>
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
                    {ft.positions.pool}
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
                    <td className="text-right py-2 px-4">{fmtQty(p.qty)}</td>
                    <td className="text-right py-2 px-4">{fmtMoney(p.avg_px)}</td>
                    <td className="text-right py-2 px-4">{fmtMoney(p.mkt_px)}</td>
                    <td className={cn("text-right py-2 px-4", pnlClass(p.upnl))}>
                      {fmtSigned(p.upnl)}
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
                    <td className="text-right py-2 px-4">{fmtMoney(o.limit)}</td>
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
  briefs,
  ft,
}: {
  desk: FinanceDesk;
  briefs: {
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
    kr: FinanceResearchBriefData | null;
  };
  ft: FinanceTranslations;
}) {
  if (isWatchDesk(desk)) {
    // Keyed by desk so switching watch modules remounts with a fresh selection
    // + analysis state (no reset effect needed).
    return <WatchModule key={desk} moduleKey={desk} />;
  }
  if (desk === "us") {
    return <ResearchBrief brief={briefs.us} market="us" />;
  }
  if (desk === "korea") {
    // KR is its own single-region semiconductor brief (no CN-style partition).
    return <ResearchBrief brief={briefs.kr} market="cn" />;
  }
  // China / HK both derive from the ONE CN brief, partitioned by symbol
  // suffix. Regime/news/themes/freshness are shared. Research-only.
  const region = desk === "hk" ? "hk" : "china";
  const partitioned =
    briefs.cn !== null ? partitionCnBrief(briefs.cn, region) : null;
  return (
    <div className="flex flex-col gap-3">
      <p className="border border-border/60 bg-secondary/20 px-3 py-2 font-mondwest normal-case text-xs text-muted-foreground">
        {ft.layout.perRegionNote}
      </p>
      <ResearchBrief brief={partitioned} market="cn" />
    </div>
  );
}

function ResearchView({
  desk,
  onDeskChange,
  briefs,
  ft,
  onRunResearch,
  researchRunning,
}: {
  desk: FinanceDesk;
  onDeskChange: (desk: FinanceDesk) => void;
  briefs: {
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
    kr: FinanceResearchBriefData | null;
  };
  ft: FinanceTranslations;
  onRunResearch: () => void;
  researchRunning: boolean;
}) {
  // The manual "run research now" button only applies to markets with their
  // own research session (China/HK → CN, Korea → KR); US research is driven by
  // the trading loop and watch modules are cross-asset, so no button there.
  const canRun = desk === "china" || desk === "hk" || desk === "korea";
  const sidebar = (
    <>
      <SidebarGroup label={ft.layout.marketsGroup}>
        {ACTIVE_MARKETS.map((m) => (
          <SidebarButton
            key={m}
            active={desk === m}
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
      <SidebarGroup label={ft.layout.watchGroup}>
        {WATCH_MODULE_KEYS.map((k) => (
          <SidebarButton
            key={k}
            active={desk === k}
            onClick={() => onDeskChange(k)}
          >
            {watchModuleName(k, ft)}
          </SidebarButton>
        ))}
      </SidebarGroup>
    </>
  );
  return (
    <MasterDetail sidebar={sidebar}>
      <div className="flex flex-col gap-3">
        {canRun && (
          <div className="flex justify-end">
            <Button
              type="button"
              size="sm"
              disabled={researchRunning}
              onClick={onRunResearch}
            >
              {researchRunning
                ? ft.layout.runningResearch
                : ft.layout.runResearch}
            </Button>
          </div>
        )}
        <ResearchDetail desk={desk} briefs={briefs} ft={ft} />
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
    kr: FinanceResearchBriefData | null;
  }>({ us: null, cn: null, kr: null });
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
  const researchDesk: FinanceDesk =
    DESKS.includes(deskParam as FinanceDesk) &&
    // Disabled placeholders are never a valid selection.
    (isActiveMarketDesk(deskParam as FinanceDesk) ||
      isWatchDesk(deskParam as FinanceDesk))
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
    (desk: FinanceDesk) => setParam("desk", desk),
    [setParam],
  );

  // The brief the Research desk needs: US, the shared CN brief, or none
  // (watch modules). Threaded into the loader so switching desks refetches.
  const briefMarket: "us" | "cn" | "kr" | null =
    researchDesk === "us"
      ? "us"
      : researchDesk === "china" || researchDesk === "hk"
        ? "cn"
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

  // Manual "run research now" for the current desk: force the backend to
  // RE-RUN the market's research session (fresh data), then refetch. Only
  // meaningful for markets with their own session (CN via china/hk, KR).
  const [researchRunning, setResearchRunning] = useState(false);
  const runResearch = useCallback(async () => {
    if (briefMarket !== "cn" && briefMarket !== "kr") return;
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
    setModeOverride(effectiveMode === "paper" ? "live" : "paper");

  const bottomBar = (
    <FinanceBottomBar
      health={health}
      offline={offline}
      lastUpdated={lastUpdated}
      loading={loading}
      onRefresh={refresh}
      mode={effectiveMode}
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
          onDeskChange={setResearchDesk}
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

      {activeTab === "holdings" && <PortfolioManager />}

      {bottomBar}

      <Toast toast={toast} />
    </div>
  );
}
