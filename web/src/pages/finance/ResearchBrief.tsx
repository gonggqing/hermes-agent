import { useEffect, useId, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  BookOpen,
  HelpCircle,
  Info,
  Layers,
  Microscope,
  Newspaper,
  Radar,
  RefreshCw,
  Search,
  ShieldAlert,
} from "lucide-react";
import { api, FinanceKnowledgeOfflineError } from "@/lib/api";
import type {
  FinanceDiscoveryPool,
  FinanceBriefMover,
  FinanceBriefRegime,
  FinanceBriefRisk,
  FinanceKnowledgeHit,
  FinanceResearchBrief,
  FinanceResearchMarket,
} from "@/lib/api";
import { discoveryCandidates } from "./research-compat";
import { cn } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { useFinanceT } from "./i18n";
import {
  directionTone,
  fmtMoney,
  fmtPct,
  fmtSigned,
  fmtSignedPct,
  fmtTs,
  pnlClass,
  regimeTone,
  sentimentBorderClass,
} from "./format";
import type { FinanceTranslations } from "@/i18n/types";

/** Debounce before a knowledge-search request is fired (ms). */
const SEARCH_DEBOUNCE_MS = 350;
/** Server-side minimum query length (`q: Query(min_length=2)`). */
const SEARCH_MIN_CHARS = 2;
/** Results requested per search. */
const SEARCH_K = 5;

function interpolate(
  template: string,
  values: Record<string, string | number>,
): string {
  return Object.entries(values).reduce(
    (text, [key, value]) => text.replaceAll(`{${key}}`, String(value)),
    template,
  );
}

function explainDiscoveryReason(
  reason: string,
  ft: FinanceTranslations,
): string {
  const trend = reason.match(/^20d trend\s+(.+)$/i);
  if (trend) {
    return interpolate(ft.brief.discovery.reasonTrend, { value: trend[1] });
  }
  const relative = reason.match(/^relative strength\s+(.+)$/i);
  if (relative) {
    return interpolate(ft.brief.discovery.reasonRelativeStrength, {
      value: relative[1],
    });
  }
  const volume = reason.match(/^volume\s+(.+)\s+20d average$/i);
  if (volume) {
    return interpolate(ft.brief.discovery.reasonVolume, { value: volume[1] });
  }
  return reason;
}

function ModuleTitle({
  description,
  icon,
  title,
}: {
  description?: string;
  icon: ReactNode;
  title: string;
}) {
  const tooltipId = useId();
  return (
    <div className="flex items-center gap-2">
      {icon}
      <CardTitle className="text-base">{title}</CardTitle>
      {description && (
        <span className="group relative inline-flex">
          <button
            type="button"
            aria-describedby={tooltipId}
            aria-label={description}
            className="inline-flex h-6 w-6 cursor-help items-center justify-center text-text-tertiary transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <Info className="h-3.5 w-3.5" />
          </button>
          <span
            id={tooltipId}
            role="tooltip"
            className="pointer-events-none absolute left-1/2 top-full z-50 mt-1.5 hidden w-72 -translate-x-1/2 bg-foreground px-2 py-1.5 font-mondwest text-xs font-semibold normal-case leading-5 text-background shadow-md group-hover:block group-focus-within:block"
          >
            {description}
          </span>
        </span>
      )}
    </div>
  );
}

function NarrativeBrief({
  brief,
  ft,
}: {
  brief: FinanceResearchBrief;
  ft: FinanceTranslations;
}) {
  const copy = ft.brief.summary;
  const narrative = brief.narrative;

  return (
    <Card className="border-primary/30 bg-primary/[0.03]">
      <CardHeader>
        <div className="flex items-start gap-3">
          <BookOpen className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
          <div className="min-w-0">
            <CardTitle className="text-base">{copy.title}</CardTitle>
            {narrative && (
              <p className="mt-1 font-mondwest normal-case text-xs text-text-tertiary">
                {narrative.market} · {narrative.model}
              </p>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {!narrative ? (
          <p className="font-mondwest normal-case text-sm leading-6 text-muted-foreground">
            {copy.unavailable}
          </p>
        ) : (
          <>
            <div className="space-y-2">
              <h3 className="font-mondwest normal-case text-base font-semibold leading-6 text-foreground">
                {narrative.headline}
              </h3>
              {narrative.summary.split(/\n{2,}/).map((paragraph) => (
                <p
                  key={paragraph}
                  className="font-mondwest normal-case text-sm leading-6 text-foreground/90"
                >
                  {paragraph}
                </p>
              ))}
            </div>
            {(narrative.change_summary ?? []).length > 0 && (
              <section className="border-t border-border pt-4">
                <h4 className="text-xs uppercase tracking-wide text-text-tertiary">
                  {copy.sincePrior}
                </h4>
                <ul className="mt-2 space-y-2">
                  {(narrative.change_summary ?? []).map((item) => (
                    <li
                      key={item}
                      className="border-l-2 border-primary/30 pl-3 font-mondwest normal-case text-sm leading-6 text-foreground"
                    >
                      {item}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            {(narrative.action_views ?? []).length > 0 && (
              <section className="border-t border-border pt-4">
                <h4 className="text-xs uppercase tracking-wide text-text-tertiary">
                  {copy.actionMap}
                </h4>
                <div className="mt-3 grid gap-3 md:grid-cols-2">
                  {(narrative.action_views ?? []).map((action) => (
                    <div
                      key={`${action.symbol}-${action.stance}`}
                      className="border border-border p-3"
                    >
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-mono-ui text-sm font-semibold">
                          {action.symbol}
                        </span>
                        {action.display_name && (
                          <span className="text-xs text-text-tertiary">
                            {action.display_name}
                          </span>
                        )}
                        <Badge variant="outline">
                          {copy.stances[action.stance] ?? action.stance}
                        </Badge>
                        <span className="text-xs text-text-tertiary">
                          {interpolate(copy.horizonSessions, {
                            n: action.horizon_sessions,
                          })}{" "}
                          · {Math.round(action.confidence * 100)}%
                        </span>
                      </div>
                      <p className="mt-2 font-mondwest normal-case text-sm leading-6 text-foreground">
                        {action.what_changed}
                      </p>
                      <p className="mt-1 font-mondwest normal-case text-xs leading-5 text-muted-foreground">
                        {action.rationale}
                      </p>
                      {action.invalidation && (
                        <p className="mt-2 text-xs text-text-tertiary">
                          {copy.invalidation}: {action.invalidation}
                        </p>
                      )}
                    </div>
                  ))}
                </div>
              </section>
            )}
            <div className="grid gap-4 md:grid-cols-2">
              {narrative.sections.map((section) => (
                <section
                  key={section.title}
                  className="border-l-2 border-primary/30 pl-3"
                >
                  <h4 className="text-xs uppercase tracking-wide text-text-tertiary">
                    {section.title}
                  </h4>
                  <p className="mt-1 font-mondwest normal-case text-sm leading-6 text-foreground">
                    {section.analysis}
                  </p>
                </section>
              ))}
            </div>
            {narrative.watch_next.length > 0 && (
              <section className="border-t border-border pt-4">
                <h4 className="text-xs uppercase tracking-wide text-text-tertiary">
                  {copy.watchNext}
                </h4>
                <ul className="mt-2 grid gap-2 md:grid-cols-2">
                  {narrative.watch_next.map((item) => (
                    <li
                      key={item}
                      className="border-l border-border pl-3 font-mondwest normal-case text-sm leading-6 text-muted-foreground"
                    >
                      {item}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── Risk strip ────────────────────────────────────────────────────────

function RiskStrip({
  risk,
  ft,
}: {
  risk: FinanceBriefRisk | null;
  ft: FinanceTranslations;
}) {
  const tripped = risk?.breaker_state === "TRIPPED";
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <ModuleTitle
            description={ft.brief.risk.description}
            icon={<ShieldAlert className="h-5 w-5 text-muted-foreground" />}
            title={ft.brief.risk.title}
          />
          {risk !== null && (
            <Badge tone={tripped ? "destructive" : "success"}>
              {risk.breaker_state}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {risk === null ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.risk.unavailable}
          </p>
        ) : (
          <>
            {tripped && (
              <div className="flex items-center gap-3 border border-destructive bg-destructive/10 px-4 py-3 text-destructive">
                <AlertTriangle className="h-5 w-5 shrink-0" />
                <div className="font-mondwest normal-case text-sm">
                  <span className="font-semibold">
                    {ft.page.breakerTrippedTitle}
                  </span>{" "}
                  {ft.page.breakerTrippedBody}
                </div>
              </div>
            )}
            {risk.warnings.length > 0 && (
              <ul className="flex flex-col gap-1 border border-warning bg-warning/10 px-4 py-3">
                {risk.warnings.map((w) => (
                  <li
                    key={w}
                    className="flex items-start gap-2 font-mondwest normal-case text-sm text-warning"
                  >
                    <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {w}
                  </li>
                ))}
              </ul>
            )}
            <Stats
              items={[
                {
                  label: ft.brief.risk.equity,
                  value: `${fmtMoney(risk.equity)} ${risk.currency ?? ""}`.trim(),
                },
                {
                  label: ft.brief.risk.cash,
                  value: `${fmtMoney(risk.cash)} ${risk.currency ?? ""}`.trim(),
                },
                {
                  label: ft.brief.risk.dayPnl,
                  value: {
                    key: "day_pnl",
                    node: (
                      <span className={pnlClass(risk.day_pnl)}>
                        {fmtSigned(risk.day_pnl)}
                      </span>
                    ),
                  },
                },
                {
                  label: ft.brief.risk.drawdown,
                  value: fmtPct(risk.drawdown_pct),
                },
                {
                  label: ft.brief.risk.breaker,
                  value: {
                    key: "breaker",
                    node: (
                      <span
                        className={
                          tripped ? "text-destructive" : "text-foreground"
                        }
                      >
                        {risk.breaker_state}
                      </span>
                    ),
                  },
                },
              ]}
            />
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mondwest normal-case text-xs text-muted-foreground">
              {Object.keys(risk.pool_exposure_pct).length > 0 && (
                <span className="flex flex-wrap items-center gap-1.5">
                  <span className="text-text-tertiary">
                    {ft.brief.risk.poolExposure}
                  </span>
                  {Object.entries(risk.pool_exposure_pct).map(([pool, pct]) => (
                    <span
                      key={pool}
                      className="border border-border px-1.5 py-0.5 font-mono-ui text-xs"
                    >
                      {pool} {fmtPct(pct, 0)}
                    </span>
                  ))}
                </span>
              )}
              {Object.keys(risk.stats).length > 0 && (
                <span>
                  {ft.brief.risk.winRate}{" "}
                  <span className="text-foreground">
                    {fmtPct((risk.stats.win_rate ?? 0) * 100, 0)}
                  </span>{" "}
                  · {ft.brief.risk.expectancy}{" "}
                  <span className={pnlClass(risk.stats.expectancy)}>
                    {fmtSigned(risk.stats.expectancy)}
                  </span>{" "}
                  · {ft.brief.risk.maxDrawdown}{" "}
                  <span className="text-foreground">
                    {fmtPct(risk.stats.max_drawdown_pct)}
                  </span>{" "}
                  ·{" "}
                  {ft.brief.risk.closedTrades.replace(
                    "{n}",
                    String(risk.stats.n_closed ?? 0),
                  )}
                </span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── Regime chips ──────────────────────────────────────────────────────

function RegimeChips({
  regime,
  ft,
}: {
  regime: FinanceBriefRegime | null;
  ft: FinanceTranslations;
}) {
  return (
    <Card>
      <CardHeader>
        <ModuleTitle
          description={ft.brief.regime.description}
          icon={<Activity className="h-5 w-5 text-muted-foreground" />}
          title={ft.brief.regime.title}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {regime === null ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.regime.unavailable}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3 font-mondwest normal-case text-sm">
            <Badge tone={regimeTone(regime.risk_on_off)}>
              {regime.risk_on_off === "risk_on"
                ? ft.brief.regime.riskOn
                : regime.risk_on_off === "risk_off"
                  ? ft.brief.regime.riskOff
                  : ft.brief.regime.neutral}
            </Badge>
            {regime.vix !== null && (
              <span className="text-muted-foreground">
                {ft.brief.regime.vix}{" "}
                <span className="text-foreground">{regime.vix.toFixed(1)}</span>
              </span>
            )}
            <span className="text-muted-foreground">
              {ft.brief.regime.breadth}{" "}
              <span className="text-foreground">
                {fmtPct(regime.breadth_pct_above_50dma, 0)}
              </span>
            </span>
            {Object.entries(regime.indices).map(([sym, vals]) => (
              <span
                key={sym}
                title={`${ft.brief.movers.vsSma50}: ${fmtSignedPct(vals.sma50_dist_pct)}`}
                className="border border-border px-1.5 py-0.5 font-mono-ui text-xs"
              >
                {sym} {fmtMoney(vals.last)}{" "}
                <span className={pnlClass(vals.sma50_dist_pct)}>
                  {fmtSignedPct(vals.sma50_dist_pct)}
                </span>
              </span>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Movers ────────────────────────────────────────────────────────────

function MoversTable({
  label,
  icon,
  rows,
  ft,
}: {
  label: string;
  icon: React.ReactNode;
  rows: FinanceBriefMover[];
  ft: FinanceTranslations;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-center gap-1.5 text-xs uppercase text-text-tertiary">
        {icon}
        {label}
      </div>
      <div className="overflow-x-auto">
        <table className="w-full font-mondwest normal-case text-sm">
          <thead>
            <tr className="border-b border-border text-muted-foreground text-xs">
              <th className="text-left py-1.5 pr-3 font-medium">
                {ft.brief.movers.symbol}
              </th>
              <th className="text-right py-1.5 px-3 font-medium">
                {ft.brief.movers.last}
              </th>
              <th className="text-right py-1.5 px-3 font-medium">
                {ft.brief.movers.vsSma20}
              </th>
              <th className="text-right py-1.5 px-3 font-medium">
                {ft.brief.movers.vsSma50}
              </th>
              <th className="text-left py-1.5 pl-3 font-medium">
                {ft.brief.movers.theme} / {ft.brief.movers.role}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((m) => (
              <tr key={m.symbol} className="border-b border-border/50">
                <td className="py-1.5 pr-3">
                  <span className="font-mono-ui text-xs">{m.symbol}</span>
                  {m.display_name && (
                    <span className="ml-1.5 font-mondwest normal-case text-xs text-muted-foreground">
                      {m.display_name}
                    </span>
                  )}
                </td>
                <td className="text-right py-1.5 px-3">{fmtMoney(m.last)}</td>
                <td
                  className={cn(
                    "text-right py-1.5 px-3",
                    pnlClass(m.dist_sma20_pct),
                  )}
                >
                  {fmtSignedPct(m.dist_sma20_pct)}
                </td>
                <td
                  className={cn(
                    "text-right py-1.5 px-3",
                    pnlClass(m.dist_sma50_pct),
                  )}
                >
                  {fmtSignedPct(m.dist_sma50_pct)}
                </td>
                <td className="py-1.5 pl-3">
                  <span className="flex flex-wrap items-center gap-1">
                    <Badge tone="secondary">{m.theme}</Badge>
                    <Badge tone="outline">{m.role}</Badge>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function MoversCard({
  movers,
  ft,
}: {
  movers: FinanceResearchBrief["movers"];
  ft: FinanceTranslations;
}) {
  const empty = movers.top.length === 0 && movers.bottom.length === 0;
  return (
    <Card>
      <CardHeader>
        <ModuleTitle
          description={ft.brief.movers.description}
          icon={<Radar className="h-5 w-5 text-muted-foreground" />}
          title={ft.brief.movers.title}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {empty ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.movers.empty}
          </p>
        ) : (
          <>
            {movers.top.length > 0 && (
              <MoversTable
                label={ft.brief.movers.top}
                icon={<ArrowUpRight className="h-3.5 w-3.5 text-success" />}
                rows={movers.top}
                ft={ft}
              />
            )}
            {movers.bottom.length > 0 && (
              <MoversTable
                label={ft.brief.movers.bottom}
                icon={
                  <ArrowDownRight className="h-3.5 w-3.5 text-destructive" />
                }
                rows={movers.bottom}
                ft={ft}
              />
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── Themes ────────────────────────────────────────────────────────────

function ThemesCard({
  themes,
  ft,
}: {
  themes: FinanceResearchBrief["themes"];
  ft: FinanceTranslations;
}) {
  const maxAbs = Math.max(
    1e-9,
    ...themes.map((t) => Math.abs(t.avg_dist_sma50_pct)),
  );
  return (
    <Card>
      <CardHeader>
        <ModuleTitle
          description={ft.brief.themes.description}
          icon={<Layers className="h-5 w-5 text-muted-foreground" />}
          title={ft.brief.themes.title}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {themes.length === 0 ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.themes.empty}
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {themes.map((theme) => (
              <li key={theme.theme} className="flex flex-col gap-1">
                <div className="flex items-baseline justify-between gap-2 font-mondwest normal-case text-sm">
                  <span className="text-foreground">{theme.theme}</span>
                  <span className={pnlClass(theme.avg_dist_sma50_pct)}>
                    {fmtSignedPct(theme.avg_dist_sma50_pct)}
                  </span>
                </div>
                <div className="h-1.5 w-full bg-secondary/40">
                  <div
                    className={cn(
                      "h-full",
                      theme.avg_dist_sma50_pct >= 0
                        ? "bg-success"
                        : "bg-destructive",
                    )}
                    style={{
                      width: `${Math.max(
                        2,
                        (Math.abs(theme.avg_dist_sma50_pct) / maxAbs) * 100,
                      )}%`,
                    }}
                  />
                </div>
                <div className="font-mondwest normal-case text-xs text-text-tertiary">
                  {ft.brief.themes.symbols.replace(
                    "{n}",
                    String(theme.n_symbols),
                  )}
                  {theme.leaders.length > 0 && (
                    <>
                      {" "}
                      · {ft.brief.themes.leaders}:{" "}
                      <span className="font-mono-ui">
                        {theme.leaders.join(", ")}
                      </span>
                    </>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function DiscoveryCard({
  pool,
  ft,
}: {
  pool: FinanceDiscoveryPool | null | undefined;
  ft: FinanceTranslations;
}) {
  const candidates = discoveryCandidates(pool);
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <ModuleTitle
            description={ft.brief.discovery.description}
            icon={<Radar className="h-5 w-5 text-muted-foreground" />}
            title={ft.brief.discovery.title}
          />
          {pool != null && (
            <Badge tone="secondary">
              {candidates.length}/{pool.source_count}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {pool != null && (pool.status === "unavailable" || pool.source_count === 0) ? (
          <p className="font-mondwest normal-case py-2 text-sm text-destructive">
            {ft.brief.discovery.sourceUnavailable}
          </p>
        ) : candidates.length === 0 ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.discovery.empty}
          </p>
        ) : (
          <ol className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {candidates.map((candidate) => (
              <li
                key={candidate.symbol}
                className="border border-border/70 bg-secondary/10 p-3"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="font-mono-ui text-sm text-foreground">
                      #{candidate.rank} {candidate.symbol}
                    </div>
                    <div className="truncate font-mondwest normal-case text-sm text-muted-foreground">
                      {candidate.display_name}
                    </div>
                  </div>
                  <Badge tone="secondary">
                    {ft.brief.discovery.score} {candidate.score.toFixed(1)}
                  </Badge>
                </div>
                <p className="mt-2 font-mondwest normal-case text-xs text-foreground">
                  {candidate.theme} · {candidate.component}
                </p>
                <p className="mt-1 line-clamp-3 font-mondwest normal-case text-xs text-muted-foreground">
                  {candidate.relationship}
                </p>
                <div className="mt-2 font-mondwest normal-case text-xs">
                  <div className="text-text-tertiary">
                    {ft.brief.discovery.reasons}
                  </div>
                  {candidate.reasons.length > 0 ? (
                    <ul className="mt-1 list-disc space-y-0.5 pl-4 text-muted-foreground">
                      {candidate.reasons.slice(0, 3).map((reason) => (
                        <li key={reason}>
                          {explainDiscoveryReason(reason, ft)}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="mt-1 text-muted-foreground">
                      {ft.brief.discovery.noReasons}
                    </p>
                  )}
                </div>
                {candidate.evidence.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-2">
                    {candidate.evidence.slice(0, 2).map((evidence) => (
                      <a
                        key={evidence.url}
                        href={evidence.url}
                        target="_blank"
                        rel="noreferrer"
                        title={`${evidence.summary} · ${evidence.observed_at}`}
                        className="font-mondwest normal-case text-xs text-primary hover:underline"
                      >
                        {ft.brief.discovery.sources}: {evidence.source}
                      </a>
                    ))}
                  </div>
                )}
              </li>
            ))}
          </ol>
        )}
      </CardContent>
    </Card>
  );
}

function SynthesisCard({
  synthesis,
  ft,
}: {
  synthesis: FinanceResearchBrief["cross_market_synthesis"];
  ft: FinanceTranslations;
}) {
  if (synthesis === undefined) return null;
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Layers className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">
            {ft.brief.synthesis.title}
          </CardTitle>
          <Badge tone="secondary">{synthesis.status}</Badge>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {synthesis.headline && (
          <p className="font-mondwest normal-case text-sm font-medium leading-6 text-foreground">
            {synthesis.headline}
          </p>
        )}
        <div className="flex flex-wrap gap-2">
          {Object.values(synthesis.markets).map((market) => (
            <span
              key={market.market}
              className="border border-border px-2 py-1 font-mono-ui text-xs"
            >
              {market.market}:{" "}
              {market.available
                ? `${market.regime ?? "unknown"} · ${market.freshness_status}`
                : "missing"}
            </span>
          ))}
        </div>
        {synthesis.shared_themes.length === 0 ? (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.brief.synthesis.empty}
          </p>
        ) : (
          <ul className="grid gap-2 md:grid-cols-2">
            {synthesis.shared_themes.map((theme) => (
              <li
                key={theme.theme}
                className="border border-border/70 bg-secondary/10 p-3 font-mondwest normal-case text-sm"
              >
                <div className="text-foreground">{theme.theme}</div>
                <div className="mt-1 font-mono-ui text-xs text-muted-foreground">
                  CN {theme.cn_symbols.join(", ") || "—"} · HK{" "}
                  {theme.hk_symbols.join(", ") || "—"}
                </div>
                {theme.relationship && (
                  <p className="mt-2 text-sm leading-5 text-muted-foreground">
                    {theme.relationship}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
        {(synthesis.analysis?.length ?? 0) > 0 && (
          <div className="flex flex-col gap-3 border-t border-border pt-3">
            {synthesis.analysis!.slice(0, 2).map((paragraph) => (
              <p key={paragraph} className="font-mondwest normal-case whitespace-pre-line text-sm leading-6 text-muted-foreground">
                {paragraph}
              </p>
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── News digest ───────────────────────────────────────────────────────

function NewsCard({
  news,
  ft,
}: {
  news: FinanceResearchBrief["news"];
  ft: FinanceTranslations;
}) {
  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Newspaper className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.news.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {news.items.length === 0 ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.news.empty}
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {news.items.map((item, i) => (
              <li
                key={`${item.url || item.headline}-${i}`}
                className={cn(
                  "border-l-2 pl-3 font-mondwest normal-case text-sm",
                  sentimentBorderClass(item.sentiment),
                )}
              >
                {item.url ? (
                  <a
                    href={item.url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-foreground hover:underline"
                  >
                    {item.headline}
                  </a>
                ) : (
                  <span className="text-foreground">{item.headline}</span>
                )}
                <div className="flex flex-wrap items-center gap-2 text-xs text-text-tertiary">
                  {item.source && <span>{item.source}</span>}
                  {item.age_hours !== null && item.age_hours !== undefined && (
                    <span>
                      {item.age_hours < 24
                        ? `${Math.round(item.age_hours)}h`
                        : `${Math.round(item.age_hours / 24)}d`}
                    </span>
                  )}
                  {item.symbol && (
                    <span className="font-mono-ui">{item.symbol}</span>
                  )}
                  {item.sentiment !== null && (
                    <span
                      title={ft.brief.news.sentiment}
                      className={pnlClass(item.sentiment)}
                    >
                      {item.sentiment > 0 ? "+" : ""}
                      {item.sentiment.toFixed(2)}
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ── Signals ───────────────────────────────────────────────────────────

function SignalsCard({
  signals,
  ft,
}: {
  signals: FinanceResearchBrief["signals_today"];
  ft: FinanceTranslations;
}) {
  return (
    <Card>
      <CardHeader>
        <ModuleTitle
          description={ft.brief.signals.description}
          icon={<Activity className="h-5 w-5 text-muted-foreground" />}
          title={ft.brief.signals.title}
        />
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {signals.length === 0 ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.signals.empty}
          </p>
        ) : (
          <ul className="flex flex-col gap-3">
            {/* Server order is debate-first, then by confidence — keep it. */}
            {signals.map((s, i) => (
              <li
                key={`${s.symbol}-${s.source_agent}-${i}`}
                className="flex flex-col gap-1"
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-mono-ui text-sm font-semibold text-foreground">
                    {s.symbol}
                  </span>
                  {s.display_name && (
                    <span className="font-mondwest normal-case text-xs text-muted-foreground">
                      {s.display_name}
                    </span>
                  )}
                  <Badge tone={directionTone(s.direction)}>{s.direction}</Badge>
                  <Badge
                    tone={s.source_agent === "debate" ? "default" : "outline"}
                  >
                    {s.source_agent}
                  </Badge>
                  <span className="font-mondwest normal-case text-xs text-muted-foreground">
                    {ft.brief.signals.confidence.replace(
                      "{pct}",
                      (s.confidence * 100).toFixed(0),
                    )}
                  </span>
                  {s.as_of_bar && (
                    // DATA as-of (§5.10): the bar these numbers rest on, distinct
                    // from the brief's as_of — so a stale verdict can't mislead.
                    <span className="font-mondwest normal-case text-xs text-muted-foreground/70">
                      {ft.brief.signals.asOfBar.replace(
                        "{date}",
                        s.as_of_bar.slice(0, 10),
                      )}
                    </span>
                  )}
                </div>
                <p className="font-mondwest normal-case text-sm text-muted-foreground">
                  {s.thesis}
                </p>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

// ── Knowledge search ──────────────────────────────────────────────────

type SearchStatus = "idle" | "searching" | "done" | "offline" | "error";

function KnowledgeSearch({ ft }: { ft: FinanceTranslations }) {
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState<SearchStatus>("idle");
  const [hits, setHits] = useState<FinanceKnowledgeHit[]>([]);
  // Monotonic sequence so a slow earlier response never clobbers the
  // result of a newer query.
  const seqRef = useRef(0);

  // Status flips in the event handler; the effect only debounces the fetch
  // and updates state from the async callbacks.
  const onQueryChange = (value: string) => {
    setQuery(value);
    if (value.trim().length < SEARCH_MIN_CHARS) {
      setStatus("idle");
      setHits([]);
    } else {
      setStatus("searching");
    }
  };

  useEffect(() => {
    const q = query.trim();
    const seq = ++seqRef.current; // cancels any in-flight response
    if (q.length < SEARCH_MIN_CHARS) return;
    const id = window.setTimeout(() => {
      api
        .financeKnowledgeSearch(q, SEARCH_K)
        .then((results) => {
          if (seqRef.current !== seq) return;
          setHits(results);
          setStatus("done");
        })
        .catch((err: unknown) => {
          if (seqRef.current !== seq) return;
          setHits([]);
          // 503 fail-closed (FinanceKnowledgeOfflineError) renders the calm
          // offline note; transport failures read the same to the user.
          setStatus(
            err instanceof FinanceKnowledgeOfflineError ? "offline" : "error",
          );
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(id);
  }, [query]);

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Search className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.search.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Input
          type="search"
          value={query}
          onChange={(e) => onQueryChange(e.target.value)}
          placeholder={ft.brief.search.placeholder}
          aria-label={ft.brief.search.title}
        />
        {status === "searching" && (
          <div className="flex items-center gap-2 font-mondwest normal-case text-sm text-muted-foreground">
            <Spinner /> {ft.brief.search.searching}
          </div>
        )}
        {(status === "offline" || status === "error") && (
          <p className="font-mondwest normal-case text-sm text-muted-foreground">
            {ft.brief.search.offline}
          </p>
        )}
        {status === "done" &&
          (hits.length === 0 ? (
            <p className="font-mondwest normal-case text-sm text-muted-foreground">
              {ft.brief.search.noResults}
            </p>
          ) : (
            <ul className="flex flex-col gap-3">
              {hits.map((hit) => (
                <li key={hit.document_id} className="flex flex-col gap-0.5">
                  {hit.source_url ? (
                    <a
                      href={hit.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-mondwest normal-case text-sm text-foreground hover:underline"
                    >
                      {hit.title}
                    </a>
                  ) : (
                    <span className="font-mondwest normal-case text-sm text-foreground">
                      {hit.title}
                    </span>
                  )}
                  <p className="font-mondwest normal-case text-xs text-muted-foreground">
                    {hit.snippet}
                  </p>
                  <div className="font-mondwest normal-case text-xs text-text-tertiary">
                    {[hit.publisher, hit.trading_date]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                </li>
              ))}
            </ul>
          ))}
      </CardContent>
    </Card>
  );
}

// ── Market toggle (US vs China/HK research desk) ──────────────────────

/**
 * Segmented US / China·HK switch. Selects which research brief the view
 * renders (Loop.md §7 Phase 0.5). It only swaps the read-only brief — it
 * carries no execution authority; the China/HK desk is research-only.
 */
function MarketToggle({
  market,
  onMarketChange,
  ft,
}: {
  market: FinanceResearchMarket;
  onMarketChange: (m: FinanceResearchMarket) => void;
  ft: FinanceTranslations;
}) {
  const options: { value: FinanceResearchMarket; label: string }[] = [
    { value: "us", label: ft.brief.markets.us },
    { value: "cn", label: ft.brief.markets.cn },
  ];
  return (
    <div
      role="group"
      aria-label={ft.brief.markets.label}
      className="ml-auto flex items-center border border-border"
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          aria-pressed={market === opt.value}
          onClick={() => onMarketChange(opt.value)}
          className={cn(
            "px-2.5 py-1 font-mondwest normal-case text-xs transition-colors",
            market === opt.value
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

// ── The brief ─────────────────────────────────────────────────────────

/**
 * Research-first top section of the Finance tab (Loop.md §7 Phase 0.5):
 * as-of header with PAPER/LIVE mode, freshness warnings, risk strip,
 * regime, movers, themes, news, signals, an explicit unknowns box, a
 * provenance footer, and a knowledge search box. Read-only — no element
 * here carries any execution authority (Loop.md §3).
 *
 * When `onMarketChange` is supplied a US / China·HK toggle renders in the
 * header. The China/HK desk is research-only: `risk` is null (no CN
 * account) so the account-risk strip is hidden, and a "research only" badge
 * makes the read-only nature explicit.
 */
export function ResearchBrief({
  brief,
  market = "us",
  onMarketChange,
  onRunResearch,
  researchRunning = false,
}: {
  brief: FinanceResearchBrief | null;
  market?: FinanceResearchMarket;
  onMarketChange?: (m: FinanceResearchMarket) => void;
  onRunResearch?: () => void;
  researchRunning?: boolean;
}) {
  const ft = useFinanceT();
  // Order authority is backend-driven (the HK desk becomes order-capable when
  // hk_orders_enabled), so the "research only" badge tracks the live flag
  // instead of a hard-coded market list. Fall back to "US only" until loaded.
  const [orderCapableMarkets, setOrderCapableMarkets] = useState<
    string[] | null
  >(null);
  useEffect(() => {
    let cancelled = false;
    api
      .financeMarkets()
      .then((r) => {
        if (!cancelled) setOrderCapableMarkets(r.order_capable);
      })
      .catch(() => {
        if (!cancelled) setOrderCapableMarkets(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);
  const researchOnly =
    orderCapableMarkets !== null
      ? !orderCapableMarkets.includes(market)
      : market !== "us";

  // Self-contained "regenerate this brief" — forces the prose narrative to
  // rebuild now (the recovery path for a market the twice-daily cycle missed).
  // Background + poll-driven, so no prop drilling from the page is needed.
  const [regenerating, setRegenerating] = useState(false);
  const [regenNote, setRegenNote] = useState<string | null>(null);
  const handleRegenerate = async () => {
    setRegenerating(true);
    setRegenNote(null);
    try {
      await api.financeRegenerateBrief(market);
      setRegenNote(ft.brief.regenerateStarted);
    } catch (err) {
      setRegenNote(ft.brief.regenerateFailed.replace("{error}", String(err)));
    } finally {
      setRegenerating(false);
    }
  };
  const headerActions = (
    <div className="ml-auto flex items-center gap-2">
      {regenNote && (
        <span className="font-mondwest normal-case text-xs text-muted-foreground">
          {regenNote}
        </span>
      )}
      {onRunResearch && (
        <Button
          disabled={researchRunning}
          onClick={onRunResearch}
          size="sm"
          type="button"
        >
          <RefreshCw
            className={cn("h-3.5 w-3.5", researchRunning && "animate-spin")}
          />
          {researchRunning ? ft.layout.runningResearch : ft.layout.runResearch}
        </Button>
      )}
      <Button
        outlined
        disabled={regenerating}
        onClick={handleRegenerate}
        size="sm"
        type="button"
      >
        <RefreshCw
          className={cn("h-3.5 w-3.5", regenerating && "animate-spin")}
        />
        {regenerating ? ft.brief.regenerating : ft.brief.regenerate}
      </Button>
    </div>
  );

  if (brief === null) {
    return (
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <Microscope className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">{ft.brief.title}</CardTitle>
            {researchOnly && (
              <Badge tone="secondary">{ft.brief.markets.researchOnly}</Badge>
            )}
            {onMarketChange && (
              <MarketToggle
                market={market}
                onMarketChange={onMarketChange}
                ft={ft}
              />
            )}
            {headerActions}
          </div>
        </CardHeader>
        <CardContent>
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.unavailable}
          </p>
        </CardContent>
      </Card>
    );
  }

  const f = brief.freshness;
  const anyStale = f.market_stale || f.news_stale || f.portfolio_stale;

  return (
    <section className="flex flex-col gap-4" aria-label={ft.brief.title}>
      {/* Header line: title · mode · trading date · as-of time · desk toggle. */}
      <div className="flex flex-wrap items-center gap-2">
        <Microscope className="h-5 w-5 text-muted-foreground" />
        <h2 className="font-mondwest text-display text-base tracking-wider text-foreground">
          {ft.brief.title}
        </h2>
        <Badge tone={brief.mode === "live" ? "destructive" : "secondary"}>
          {brief.mode === "live" ? ft.page.modeLive : ft.page.modePaper}
        </Badge>
        {researchOnly && (
          <Badge tone="secondary">{ft.brief.markets.researchOnly}</Badge>
        )}
        <span
          className="font-mono-ui text-xs text-foreground"
          title={ft.brief.tradingDate}
        >
          {brief.trading_date}
        </span>
        <span className="font-mondwest normal-case text-xs text-text-tertiary">
          {ft.brief.asOf.replace("{time}", fmtTs(brief.as_of))}
        </span>
        {onMarketChange && (
          <MarketToggle
            market={market}
            onMarketChange={onMarketChange}
            ft={ft}
          />
        )}
        {headerActions}
      </div>

      {/* Stale-data banner: any stale source or freshness warning. */}
      {(anyStale || f.warnings.length > 0) && (
        <div className="flex flex-col gap-1 border border-warning bg-warning/10 px-4 py-3">
          <div className="flex items-center gap-2 font-mondwest normal-case text-sm font-semibold text-warning">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            {ft.brief.staleWarningsTitle}
          </div>
          {f.warnings.length > 0 && (
            <ul className="flex flex-col gap-0.5 pl-6">
              {f.warnings.map((w) => (
                <li
                  key={w}
                  className="list-disc font-mondwest normal-case text-sm text-warning"
                >
                  {w}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <NarrativeBrief brief={brief} ft={ft} />

      {/* Account-risk strip is US-desk only — the China/HK desk is
          research-only with no account (`risk` is null). */}
      {!researchOnly && <RiskStrip risk={brief.risk} ft={ft} />}
      <RegimeChips regime={brief.regime} ft={ft} />
      <DiscoveryCard pool={brief.discovery} ft={ft} />
      {market !== "us" && (
        <SynthesisCard synthesis={brief.cross_market_synthesis} ft={ft} />
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <MoversCard movers={brief.movers} ft={ft} />
        <ThemesCard themes={brief.themes} ft={ft} />
      </div>

      <div className="grid gap-4 lg:grid-cols-2">
        <NewsCard news={brief.news} ft={ft} />
        <SignalsCard signals={brief.signals_today} ft={ft} />
      </div>

      {/* Unknowns & uncertainty — every item rendered; honesty is the
          feature (Loop.md §5.9). */}
      <Card>
        <CardHeader>
          <ModuleTitle
            description={ft.brief.uncertainty.description}
            icon={<HelpCircle className="h-5 w-5 text-muted-foreground" />}
            title={ft.brief.uncertainty.title}
          />
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          {brief.uncertainty.length === 0 ? (
            <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
              {ft.brief.uncertainty.empty}
            </p>
          ) : (
            <ul className="flex flex-col gap-1 pl-5">
              {brief.uncertainty.map((item) => (
                <li
                  key={item}
                  className="list-disc font-mondwest normal-case text-sm text-muted-foreground"
                >
                  {item}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <KnowledgeSearch ft={ft} />

      {/* Provenance footer — every brief cites its sources. */}
      {brief.provenance.length > 0 && (
        <footer className="flex flex-wrap items-baseline gap-x-4 gap-y-1 font-mondwest normal-case text-xs text-text-tertiary">
          <span className="uppercase">{ft.brief.provenance.title}</span>
          {brief.provenance.map((link) => (
            <a
              key={link.url}
              href={link.url}
              target="_blank"
              rel="noreferrer"
              className="hover:text-foreground hover:underline"
            >
              {link.label}
            </a>
          ))}
        </footer>
      )}
    </section>
  );
}
