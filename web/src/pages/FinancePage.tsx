import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Globe,
  ListChecks,
  Newspaper,
  PlugZap,
  RefreshCw,
  TrendingUp,
  Wallet,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  FinanceAccountResponse,
  FinanceAccountView,
  FinanceCandidate,
  FinanceFill,
  FinanceHealth,
  FinanceMarketSnapshot,
  FinancePendingCandidate,
  FinanceReports,
  FinanceResearchBrief as FinanceResearchBriefData,
  FinanceResearchMarket,
  FinanceSnapshot,
  FinanceStats,
  FinanceWatchlistItem,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { CommandBlock } from "@nous-research/ui/ui/components/command-block";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useI18n } from "@/i18n";
import { ApprovalQueue } from "@/pages/finance/ApprovalQueue";
import { HistorySection } from "@/pages/finance/HistorySection";
import { ResearchBrief } from "@/pages/finance/ResearchBrief";
import { useFinanceT } from "@/pages/finance/i18n";
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

/** Account numbers shared by the live view and the ledger-fallback snapshot. */
interface AccountNumbers {
  equity: number;
  cash: number;
  upnl: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: string;
}

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

function PositionsAndOrders({ view }: { view: FinanceAccountView | null }) {
  const ft = useFinanceT();
  return (
    <div className="grid gap-6 lg:grid-cols-2">
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
                  {view.open_orders.map((o, i) => (
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
    </div>
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

/**
 * Compact "actions requiring attention" strip (Loop.md §7 Phase 0.5: the
 * Queue is a badged action area, NOT the primary canvas). Hidden entirely
 * when nothing is pending; a collapsed summary (count + earliest expiry)
 * expands to the existing ApprovalQueue, which is still the ONLY execution
 * surface and only relays approve/edit/reject (Loop.md §3/§5.6).
 */
function ActionsStrip({
  pending,
  onActed,
  showToast,
}: {
  pending: FinancePendingCandidate[];
  onActed: () => void;
  showToast: (message: string, type: "error" | "success") => void;
}) {
  const ft = useFinanceT();
  const [open, setOpen] = useState(false);
  if (pending.length === 0) return null;
  // Earliest expiry among pending candidates (ISO strings sort correctly).
  const earliest = pending
    .map((p) => p.candidate.valid_until)
    .filter((v): v is string => v !== null)
    .sort()[0];
  return (
    <section className="flex flex-col gap-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full flex-wrap items-center gap-2 border border-warning/60 bg-warning/10 px-4 py-2.5 text-left transition-colors hover:bg-warning/20"
      >
        <ListChecks className="h-4 w-4 shrink-0 text-warning" />
        <span className="font-mondwest normal-case text-sm font-semibold text-foreground">
          {ft.queue.title}
        </span>
        <Badge tone="warning">
          {ft.queue.pendingCount.replace("{count}", String(pending.length))}
        </Badge>
        {earliest !== undefined && (
          <span className="font-mondwest normal-case text-xs text-muted-foreground">
            {ft.queue.earliestExpiry.replace("{time}", fmtTs(earliest))}
          </span>
        )}
        <span className="ml-auto flex items-center gap-1 font-mondwest normal-case text-xs text-muted-foreground">
          {open ? ft.queue.collapse : ft.queue.expand}
          {open ? (
            <ChevronDown className="h-3.5 w-3.5" />
          ) : (
            <ChevronRight className="h-3.5 w-3.5" />
          )}
        </span>
      </button>
      {open && (
        <ApprovalQueue pending={pending} onActed={onActed} showToast={showToast} />
      )}
    </section>
  );
}

export default function FinancePage() {
  const [health, setHealth] = useState<FinanceHealth | null>(null);
  const [offline, setOffline] = useState(false);
  const [loading, setLoading] = useState(true);
  // Selected research desk (US default, or the China/HK research-only desk).
  // Briefs are cached per desk so toggling back is instant and never shows
  // the other desk's numbers.
  const [researchMarket, setResearchMarket] =
    useState<FinanceResearchMarket>("us");
  const [briefs, setBriefs] = useState<{
    us: FinanceResearchBriefData | null;
    cn: FinanceResearchBriefData | null;
  }>({ us: null, cn: null });
  const [account, setAccount] = useState<FinanceAccountResponse | null>(null);
  const [snapshots, setSnapshots] = useState<FinanceSnapshot[]>([]);
  const [market, setMarket] = useState<FinanceMarketSnapshot | null>(null);
  const [watchlist, setWatchlist] = useState<FinanceWatchlistItem[]>([]);
  const [reports, setReports] = useState<FinanceReports>({});
  const [pending, setPending] = useState<FinancePendingCandidate[]>([]);
  const [candidates, setCandidates] = useState<FinanceCandidate[]>([]);
  const [fills, setFills] = useState<FinanceFill[]>([]);
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const { toast, showToast } = useToast();
  const { setAfterTitle, setEnd } = usePageHeader();
  const ft = useFinanceT();
  const { t } = useI18n();

  // No synchronous setState in load itself: `loading` starts true and the
  // manual refresh button flips it back on before invoking load(), so the
  // 30s background refresh never flashes the whole-page spinner.
  const load = useCallback(() => {
    // Health first: it decides offline vs. data view. The section fetches
    // use allSettled so one degraded endpoint (e.g. idle loop) never
    // blanks the whole page.
    api
      .financeHealth()
      .then(async (h) => {
        setHealth(h);
        setOffline(false);
        // Research brief for the selected desk, fetched on its own so a
        // degraded brief never blanks the rest of the page.
        const [rb, rest] = await Promise.all([
          api.financeResearchBrief(researchMarket).then(
            (v) => ({ ok: true as const, v }),
            () => ({ ok: false as const }),
          ),
          // US-account reference material (account/positions/orders/queue/
          // history). The China/HK desk is research-only (no account), so
          // skip these fetches entirely in CN mode.
          researchMarket === "us"
            ? Promise.allSettled([
                api.financeAccount(),
                api.financeSnapshots(SNAPSHOT_LIMIT),
                api.financeMarket(),
                api.financeWatchlist(),
                api.financeLatestReports(),
                api.financePendingCandidates(),
                api.financeCandidates(),
                api.financeFills(),
              ])
            : Promise.resolve(null),
        ]);
        if (rb.ok) setBriefs((b) => ({ ...b, [researchMarket]: rb.v }));
        if (rest !== null) {
          const [acct, snaps, mkt, wl, rep, pend, cands, fl] = rest;
          if (acct.status === "fulfilled") setAccount(acct.value);
          if (snaps.status === "fulfilled") setSnapshots(snaps.value);
          if (mkt.status === "fulfilled") setMarket(mkt.value);
          if (wl.status === "fulfilled") setWatchlist(wl.value);
          if (rep.status === "fulfilled") setReports(rep.value);
          if (pend.status === "fulfilled") setPending(pend.value);
          if (cands.status === "fulfilled") setCandidates(cands.value);
          if (fl.status === "fulfilled") setFills(fl.value);
        }
        setLastUpdated(new Date());
      })
      .catch(() => setOffline(true))
      .finally(() => setLoading(false));
  }, [researchMarket]);

  useEffect(() => {
    load();
    const id = window.setInterval(load, REFRESH_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [load]);

  useLayoutEffect(() => {
    setAfterTitle(
      <div className="flex flex-wrap items-center gap-2">
        {health && !offline && (
          <>
            <Badge tone={health.mode === "live" ? "destructive" : "secondary"}>
              {health.mode === "live" ? ft.page.modeLive : ft.page.modePaper}
            </Badge>
            <span className="flex items-center gap-1.5 font-mondwest normal-case text-xs text-muted-foreground">
              <span
                className={cn(
                  "h-2 w-2 rounded-full",
                  health.loop_attached
                    ? "bg-success"
                    : "bg-muted-foreground/40",
                )}
              />
              {health.loop_attached ? ft.page.loopAttached : ft.page.loopIdle}
            </span>
          </>
        )}
        {offline && <Badge tone="destructive">{ft.page.offline}</Badge>}
        {lastUpdated && !offline && (
          <span className="font-mondwest normal-case text-xs text-text-tertiary">
            {ft.page.updatedAt.replace(
              "{time}",
              lastUpdated.toLocaleTimeString(),
            )}
          </span>
        )}
        <Button
          type="button"
          ghost
          size="icon"
          className="text-muted-foreground hover:text-foreground"
          onClick={() => {
            setLoading(true);
            load();
          }}
          disabled={loading}
          aria-label={t.common.refresh}
        >
          {loading ? <Spinner /> : <RefreshCw />}
        </Button>
      </div>,
    );
    setEnd(null);
    return () => {
      setAfterTitle(null);
      setEnd(null);
    };
  }, [health, offline, lastUpdated, loading, load, setAfterTitle, setEnd, ft, t]);

  const brief = briefs[researchMarket];
  const isUsDesk = researchMarket === "us";
  const ledgerFallback = account !== null && "source" in account;
  const liveView = account !== null && !("source" in account) ? account : null;
  const accountNumbers: AccountNumbers | null =
    account === null ? null : "source" in account ? account.snapshot : account;
  const stats = account?.stats ?? null;

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
        <Toast toast={toast} />
      </div>
    );
  }

  // Research-first layout (Loop.md §7 Phase 0.5): the brief is the primary
  // canvas; the approval queue is a compact badged strip below it; account/
  // positions/orders/history are reference material further down.
  return (
    <div className="flex flex-col gap-6">
      {/* Account-risk / order surfaces belong to the US desk only — the
          China/HK desk is research-only (Loop.md §3): no breaker banner,
          no approval queue, no account/positions/orders/history. */}
      {isUsDesk && health?.breaker === "TRIPPED" && (
        <div className="flex items-center gap-3 border border-destructive bg-destructive/10 px-4 py-3 text-destructive">
          <AlertTriangle className="h-5 w-5 shrink-0" />
          <div className="font-mondwest normal-case text-sm">
            <span className="font-semibold">{ft.page.breakerTrippedTitle}</span>{" "}
            {ft.page.breakerTrippedBody}
          </div>
        </div>
      )}

      <ResearchBrief
        brief={brief}
        market={researchMarket}
        onMarketChange={setResearchMarket}
      />

      {isUsDesk && (
        <>
          <ActionsStrip
            pending={pending}
            onActed={load}
            showToast={showToast}
          />

          <AccountSection
            numbers={accountNumbers}
            stats={stats}
            snapshots={snapshots}
            ledgerFallback={ledgerFallback}
          />

          <PositionsAndOrders view={liveView} />

          <MarketStrip market={market} watchlist={watchlist} />

          <HistorySection candidates={candidates} fills={fills} stats={stats} />

          <ReportsCard reports={reports} />
        </>
      )}

      <Toast toast={toast} />
    </div>
  );
}
