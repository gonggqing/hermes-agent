import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
} from "react";
import {
  AlertTriangle,
  Globe,
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
import { ApprovalQueue } from "@/pages/finance/ApprovalQueue";
import { HistorySection } from "@/pages/finance/HistorySection";
import {
  fmtMoney,
  fmtPct,
  fmtQty,
  fmtSigned,
  fmtTs,
  pnlClass,
  sideTone,
} from "@/pages/finance/format";
import type { BadgeTone } from "@/pages/finance/format";

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

function regimeTone(regime: string | undefined): BadgeTone {
  switch (regime) {
    case "risk_on":
      return "success";
    case "risk_off":
      return "destructive";
    default:
      return "outline";
  }
}

function EquitySparkline({ snapshots }: { snapshots: FinanceSnapshot[] }) {
  if (snapshots.length < 2) {
    return (
      <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
        Not enough snapshots yet for an equity curve.
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
        aria-label="Equity curve"
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
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <Wallet className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Account</CardTitle>
            {ledgerFallback && (
              <Badge tone="outline">last ledger snapshot</Badge>
            )}
          </div>
        </CardHeader>
        <CardContent>
          {numbers === null ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              No account snapshot recorded yet
              {stats !== null && stats.n_closed > 0
                ? " (ledger stats below)."
                : "."}
            </p>
          ) : (
            <Stats
              items={[
                { label: "Equity", value: fmtMoney(numbers.equity) },
                { label: "Cash", value: fmtMoney(numbers.cash) },
                {
                  label: "uPnL",
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
                  label: "Day PnL",
                  value: {
                    key: "day_pnl",
                    node: (
                      <span className={pnlClass(numbers.day_pnl)}>
                        {fmtSigned(numbers.day_pnl)}
                      </span>
                    ),
                  },
                },
                { label: "Drawdown", value: fmtPct(numbers.drawdown_pct) },
              ]}
            />
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Equity curve</CardTitle>
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
  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Positions</CardTitle>
        </CardHeader>
        <CardContent>
          {view === null ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              Positions are shown while the daily loop is attached.
            </p>
          ) : view.positions.length === 0 ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              No open positions.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mondwest normal-case text-sm">
                <thead>
                  <tr className="border-b border-border text-muted-foreground text-xs">
                    <th className="text-left py-2 pr-4 font-medium">Symbol</th>
                    <th className="text-right py-2 px-4 font-medium">Qty</th>
                    <th className="text-right py-2 px-4 font-medium">Avg px</th>
                    <th className="text-right py-2 px-4 font-medium">Mkt px</th>
                    <th className="text-right py-2 px-4 font-medium">uPnL</th>
                    <th className="text-left py-2 pl-4 font-medium">Pool</th>
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
          <CardTitle className="text-base">Open orders</CardTitle>
        </CardHeader>
        <CardContent>
          {view === null ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              Open orders are shown while the daily loop is attached.
            </p>
          ) : view.open_orders.length === 0 ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              No working orders.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mondwest normal-case text-sm">
                <thead>
                  <tr className="border-b border-border text-muted-foreground text-xs">
                    <th className="text-left py-2 pr-4 font-medium">Symbol</th>
                    <th className="text-left py-2 pr-4 font-medium">Side</th>
                    <th className="text-right py-2 px-4 font-medium">Qty</th>
                    <th className="text-left py-2 px-4 font-medium">Type</th>
                    <th className="text-right py-2 px-4 font-medium">Limit</th>
                    <th className="text-right py-2 px-4 font-medium">Stop</th>
                    <th className="text-left py-2 pl-4 font-medium">Status</th>
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
          <CardTitle className="text-base">Market & watchlist</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {market !== null && market.status === undefined ? (
          <div className="flex flex-wrap items-center gap-3 font-mondwest normal-case text-sm">
            <Badge tone={regimeTone(market.risk_on_off)}>
              {market.risk_on_off ?? "unknown"}
            </Badge>
            <span className="text-muted-foreground">
              VIX{" "}
              <span className="text-foreground">
                {market.vix === null || market.vix === undefined
                  ? "—"
                  : market.vix.toFixed(1)}
              </span>
            </span>
            <span className="text-muted-foreground">
              Breadth &gt;50dma{" "}
              <span className="text-foreground">
                {fmtPct(market.breadth_pct_above_50dma, 0)}
              </span>
            </span>
            {market.ts && (
              <span className="text-xs text-text-tertiary">
                as of {fmtTs(market.ts)}
              </span>
            )}
          </div>
        ) : (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            No market snapshot yet — the MarketMonitor publishes one while the
            daily loop runs.
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
                    title={`${item.theme} · ${item.ai_phase}${item.enabled ? "" : " · disabled"}`}
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
            Latest report{entry ? ` — ${entry[0]}` : ""}
          </CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {entry === undefined ? (
          <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
            No report generated yet — the reporter runs each morning after
            overnight fills settle.
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
  return (
    <Card>
      <CardContent className="py-12">
        <div className="mx-auto flex max-w-xl flex-col items-center gap-4 text-center">
          <PlugZap className="h-8 w-8 text-muted-foreground opacity-40" />
          <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
            Finance service offline
          </h2>
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            The dashboard could not reach the trading service. Start it on
            this machine, then refresh:
          </p>
          <CommandBlock label="Start the Finance service" code={SERVICE_START_COMMAND} />
        </div>
      </CardContent>
    </Card>
  );
}

export default function FinancePage() {
  const [health, setHealth] = useState<FinanceHealth | null>(null);
  const [offline, setOffline] = useState(false);
  const [loading, setLoading] = useState(true);
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
        const [acct, snaps, mkt, wl, rep, pend, cands, fl] =
          await Promise.allSettled([
            api.financeAccount(),
            api.financeSnapshots(SNAPSHOT_LIMIT),
            api.financeMarket(),
            api.financeWatchlist(),
            api.financeLatestReports(),
            api.financePendingCandidates(),
            api.financeCandidates(),
            api.financeFills(),
          ]);
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
  }, []);

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
              {health.mode}
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
              {health.loop_attached ? "loop attached" : "loop idle"}
            </span>
          </>
        )}
        {offline && <Badge tone="destructive">offline</Badge>}
        {lastUpdated && !offline && (
          <span className="font-mondwest normal-case text-xs text-text-tertiary">
            updated {lastUpdated.toLocaleTimeString()}
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
          aria-label="Refresh"
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
  }, [health, offline, lastUpdated, loading, load, setAfterTitle, setEnd]);

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

  return (
    <div className="flex flex-col gap-6">
      {health?.breaker === "TRIPPED" && (
        <div className="flex items-center gap-3 border border-destructive bg-destructive/10 px-4 py-3 text-destructive">
          <AlertTriangle className="h-5 w-5 shrink-0" />
          <div className="font-mondwest normal-case text-sm">
            <span className="font-semibold">CIRCUIT BREAKER TRIPPED</span> —
            daily drawdown limit hit; no new entries today (Loop.md §3).
          </div>
        </div>
      )}

      <ApprovalQueue pending={pending} onActed={load} showToast={showToast} />

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

      <Toast toast={toast} />
    </div>
  );
}
