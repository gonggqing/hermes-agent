import { Fragment, useState } from "react";
import { ChevronDown, ChevronRight, History, Receipt, Sigma } from "lucide-react";
import { api } from "@/lib/api";
import type {
  FinanceAuditEvent,
  FinanceCandidate,
  FinanceFill,
  FinanceStats,
} from "@/lib/api";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import {
  candidateStatusTone,
  fmtMoney,
  fmtPct,
  fmtQty,
  fmtSigned,
  fmtTs,
  pnlClass,
  sideTone,
} from "./format";

const MAX_HISTORY_ROWS = 25;
const MAX_FILL_ROWS = 25;

type AuditState = FinanceAuditEvent[] | "loading" | "error";

function AuditTimeline({ state }: { state: AuditState }) {
  if (state === "loading") {
    return (
      <div className="flex items-center gap-2 py-3 text-sm text-muted-foreground">
        <Spinner /> Loading audit trail…
      </div>
    );
  }
  if (state === "error") {
    return (
      <p className="py-3 text-sm text-destructive">
        Failed to load the audit trail.
      </p>
    );
  }
  if (state.length === 0) {
    return (
      <p className="py-3 text-sm text-muted-foreground">
        No audit events recorded for this candidate.
      </p>
    );
  }
  return (
    <ol className="flex flex-col gap-2 border-l border-border pl-4 py-3">
      {state.map((e, i) => (
        <li key={`${e.ts}-${i}`} className="font-mondwest normal-case text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-text-tertiary">{fmtTs(e.ts)}</span>
            <span className="font-medium text-foreground">{e.action}</span>
            <span className="text-muted-foreground">
              by {e.actor} via {e.surface} (v{e.version})
            </span>
            {!e.applied && <Badge tone="destructive">refused</Badge>}
          </div>
          {(e.prev_status || e.new_status) && (
            <div className="text-muted-foreground">
              {e.prev_status || "—"} → {e.new_status || "—"}
            </div>
          )}
          {e.detail && <div className="text-text-tertiary">{e.detail}</div>}
        </li>
      ))}
    </ol>
  );
}

/**
 * History block (Loop.md §5.9): recent candidates with status chips and an
 * expandable per-candidate audit timeline, the fills list, and ledger stats.
 */
export function HistorySection({
  candidates,
  fills,
  stats,
}: {
  candidates: FinanceCandidate[];
  fills: FinanceFill[];
  stats: FinanceStats | null;
}) {
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [audit, setAudit] = useState<Record<string, AuditState>>({});

  const toggleAudit = (id: string) => {
    if (expandedId === id) {
      setExpandedId(null);
      return;
    }
    setExpandedId(id);
    if (audit[id] === undefined || audit[id] === "error") {
      setAudit((a) => ({ ...a, [id]: "loading" }));
      api
        .financeAudit(id)
        .then((events) => setAudit((a) => ({ ...a, [id]: events })))
        .catch(() => setAudit((a) => ({ ...a, [id]: "error" })));
    }
  };

  const recent = [...candidates]
    .sort((a, b) => (a.ts < b.ts ? 1 : -1))
    .slice(0, MAX_HISTORY_ROWS);
  const recentFills = [...fills]
    .sort((a, b) => (a.ts < b.ts ? 1 : -1))
    .slice(0, MAX_FILL_ROWS);

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-center gap-2">
            <History className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">Candidate history</CardTitle>
          </div>
        </CardHeader>
        <CardContent>
          {recent.length === 0 ? (
            <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
              No candidates recorded yet.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full font-mondwest normal-case text-sm">
                <thead>
                  <tr className="border-b border-border text-muted-foreground text-xs">
                    <th className="w-6 py-2 pr-2" aria-label="Expand" />
                    <th className="text-left py-2 pr-4 font-medium">Time</th>
                    <th className="text-left py-2 pr-4 font-medium">Symbol</th>
                    <th className="text-left py-2 pr-4 font-medium">Side</th>
                    <th className="text-right py-2 px-4 font-medium">Qty</th>
                    <th className="text-left py-2 px-4 font-medium">Type</th>
                    <th className="text-right py-2 px-4 font-medium">Conf.</th>
                    <th className="text-left py-2 pl-4 font-medium">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((c) => (
                    <Fragment key={c.id}>
                      <tr
                        className="cursor-pointer border-b border-border/50 hover:bg-secondary/20 transition-colors"
                        onClick={() => toggleAudit(c.id)}
                      >
                        <td className="py-2 pr-2 text-muted-foreground">
                          {expandedId === c.id ? (
                            <ChevronDown className="h-3.5 w-3.5" />
                          ) : (
                            <ChevronRight className="h-3.5 w-3.5" />
                          )}
                        </td>
                        <td className="py-2 pr-4 text-muted-foreground">
                          {fmtTs(c.ts)}
                        </td>
                        <td className="py-2 pr-4">
                          <span className="font-mono-ui text-xs">{c.symbol}</span>
                        </td>
                        <td className="py-2 pr-4">
                          <Badge tone={sideTone(c.side)}>{c.side}</Badge>
                        </td>
                        <td className="text-right py-2 px-4">{fmtQty(c.qty)}</td>
                        <td className="py-2 px-4 text-muted-foreground">
                          {c.order_type}
                        </td>
                        <td className="text-right py-2 px-4 text-muted-foreground">
                          {(c.confidence * 100).toFixed(0)}%
                        </td>
                        <td className="py-2 pl-4">
                          <Badge tone={candidateStatusTone(c.status)}>
                            {c.status}
                          </Badge>
                        </td>
                      </tr>
                      {expandedId === c.id && (
                        <tr className="border-b border-border/50">
                          <td colSpan={8} className="pl-8">
                            <AuditTimeline state={audit[c.id] ?? "loading"} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Receipt className="h-5 w-5 text-muted-foreground" />
              <CardTitle className="text-base">Fills</CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {recentFills.length === 0 ? (
              <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
                No fills recorded yet.
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full font-mondwest normal-case text-sm">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground text-xs">
                      <th className="text-left py-2 pr-4 font-medium">Time</th>
                      <th className="text-left py-2 pr-4 font-medium">Symbol</th>
                      <th className="text-left py-2 pr-4 font-medium">Side</th>
                      <th className="text-right py-2 px-4 font-medium">Qty</th>
                      <th className="text-right py-2 px-4 font-medium">Price</th>
                      <th className="text-right py-2 pl-4 font-medium">Comm.</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentFills.map((f) => (
                      <tr
                        key={f.id}
                        className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                      >
                        <td className="py-2 pr-4 text-muted-foreground">
                          {fmtTs(f.ts)}
                        </td>
                        <td className="py-2 pr-4">
                          <span className="font-mono-ui text-xs">{f.symbol}</span>
                        </td>
                        <td className="py-2 pr-4">
                          <Badge tone={sideTone(f.side)}>{f.side}</Badge>
                        </td>
                        <td className="text-right py-2 px-4">{fmtQty(f.qty)}</td>
                        <td className="text-right py-2 px-4">{fmtMoney(f.px)}</td>
                        <td className="text-right py-2 pl-4 text-muted-foreground">
                          {fmtMoney(f.commission)}
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
            <div className="flex items-center gap-2">
              <Sigma className="h-5 w-5 text-muted-foreground" />
              <CardTitle className="text-base">Ledger stats</CardTitle>
            </div>
          </CardHeader>
          <CardContent>
            {stats === null ? (
              <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
                No stats available yet.
              </p>
            ) : (
              <div className="flex flex-col gap-4">
                <Stats
                  items={[
                    {
                      label: "Win rate",
                      value: fmtPct(stats.win_rate * 100, 0),
                    },
                    {
                      label: "Expectancy",
                      value: fmtSigned(stats.expectancy),
                    },
                    {
                      label: "Payoff",
                      value:
                        stats.payoff_ratio === null
                          ? "—"
                          : stats.payoff_ratio.toFixed(2),
                    },
                    {
                      label: "Max DD",
                      value: fmtPct(stats.max_drawdown_pct),
                    },
                  ]}
                />
                <div className="font-mondwest normal-case text-xs text-muted-foreground">
                  {stats.n_closed} closed trades · {stats.n_wins} wins · total{" "}
                  <span className={pnlClass(stats.total_pnl)}>
                    {fmtSigned(stats.total_pnl)}
                  </span>
                  {stats.avg_hold_days !== null &&
                    ` · avg hold ${stats.avg_hold_days.toFixed(1)}d`}
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
