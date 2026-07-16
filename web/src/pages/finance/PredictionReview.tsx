import { useEffect, useId, useState } from "react";
import { BarChart3, Info } from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { api } from "@/lib/api";
import type {
  FinancePredictionMetrics,
  FinancePredictionSummary,
  FinanceResearchMarket,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { useFinanceT } from "./i18n";
import { directionTone } from "./format";

type MarketFilter = "all" | FinanceResearchMarket;

const MARKET_FILTERS: MarketFilter[] = ["all", "us", "hk", "cn", "kr"];

function pct(value: number | null, digits = 1): string {
  return value === null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

function returnPct(value: number | null): string {
  return value === null ? "—" : `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function marketLabel(
  value: MarketFilter,
  ft: ReturnType<typeof useFinanceT>,
): string {
  if (value === "all") return ft.prediction.allMarkets;
  return {
    us: ft.layout.marketUs,
    hk: ft.layout.marketHk,
    cn: ft.layout.marketChina,
    kr: ft.layout.marketKorea,
  }[value];
}

function evaluationTone(
  state: string,
): "destructive" | "secondary" | "success" {
  switch (state.toLowerCase()) {
    case "confirmed":
      return "success";
    case "refuted":
      return "destructive";
    default:
      return "secondary";
  }
}

function DescriptionTooltip({ description }: { description: string }) {
  const tooltipId = useId();
  return (
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
  );
}

function StatCard({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="p-4">
        <div className="text-xs text-text-tertiary">{label}</div>
        <div className="mt-1 font-mondwest text-xl tabular-nums text-foreground">
          {value}
        </div>
        {hint && <div className="mt-1 text-xs text-text-tertiary">{hint}</div>}
      </CardContent>
    </Card>
  );
}

function MetricsTable({
  title,
  rows,
  horizon,
}: {
  title: string;
  rows: Array<{ key: string } & FinancePredictionMetrics>;
  horizon?: boolean;
}) {
  const ft = useFinanceT();
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
      </CardHeader>
      <CardContent className="overflow-x-auto p-0">
        <table className="w-full min-w-[520px] text-sm">
          <thead className="border-y border-border text-left text-xs text-text-tertiary">
            <tr>
              <th className="px-4 py-2 font-normal">
                {horizon ? ft.prediction.colHorizon : ft.prediction.colMarket}
              </th>
              <th className="px-4 py-2 text-right font-normal">
                {ft.prediction.colSample}
              </th>
              <th className="px-4 py-2 text-right font-normal">
                {ft.prediction.colAccuracy}
              </th>
              <th className="px-4 py-2 text-right font-normal">
                {ft.prediction.colReturn}
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr
                className="border-b border-border/60 last:border-0"
                key={row.key}
              >
                <td className="px-4 py-2.5">
                  {horizon ? `${row.key}D` : row.key}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums">
                  {row.directional_samples}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums">
                  {pct(row.directional_accuracy)}
                </td>
                <td className="px-4 py-2.5 text-right tabular-nums">
                  {returnPct(row.mean_return_pct)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

export function PredictionReview() {
  const ft = useFinanceT();
  const [market, setMarket] = useState<MarketFilter>("all");
  const [summary, setSummary] = useState<FinancePredictionSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    api
      .financePredictionSummary(market === "all" ? undefined : market)
      .then(
        (value) => !cancelled && setSummary(value),
        () => !cancelled && setError(true),
      )
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [market]);

  const selectMarket = (value: MarketFilter) => {
    if (value === market) {
      return;
    }
    setLoading(true);
    setError(false);
    setMarket(value);
  };

  if (loading)
    return (
      <div className="flex justify-center py-16">
        <Spinner />
      </div>
    );
  if (error || !summary)
    return (
      <Card>
        <CardContent className="p-5 text-sm text-destructive">
          {ft.prediction.empty}
        </CardContent>
      </Card>
    );
  const overview = summary.overview;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-center gap-1.5">
          <h2 className="font-mondwest text-xl text-foreground">
            {ft.prediction.title}
          </h2>
          <DescriptionTooltip description={ft.prediction.description} />
        </div>
        <div
          className="flex flex-wrap border border-border"
          role="group"
          aria-label={ft.prediction.colMarket}
        >
          {MARKET_FILTERS.map((value) => (
            <button
              aria-pressed={market === value}
              className={cn(
                "min-h-9 px-3 text-xs transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary",
                market === value
                  ? "bg-primary text-primary-foreground"
                  : "hover:bg-secondary/40",
              )}
              key={value}
              onClick={() => selectMarket(value)}
              type="button"
            >
              {marketLabel(value, ft)}
            </button>
          ))}
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label={ft.prediction.statActive}
          value={String(overview.active_series)}
        />
        <StatCard
          label={ft.prediction.statEvaluated}
          value={String(overview.evaluated_checkpoints)}
        />
        <StatCard
          label={ft.prediction.statDue}
          value={String(overview.due_checkpoints)}
        />
        <StatCard
          label={ft.prediction.statAccuracy}
          value={pct(overview.directional_accuracy)}
          hint={`${overview.directional_samples} ${ft.prediction.sampleHint}`}
        />
      </div>

      <section className="space-y-3">
        <h3 className="font-mondwest text-base">
          {ft.prediction.activeForecasts}
        </h3>
        {summary.active_forecasts.length === 0 ? (
          <div className="text-sm text-muted-foreground">
            {ft.prediction.empty}
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {summary.active_forecasts.slice(0, 12).map((item) => (
              <Card key={item.series_id}>
                <CardContent className="p-4">
                  <div className="flex items-center justify-between gap-3">
                    <div className="min-w-0">
                      <div className="truncate font-medium">
                        {item.display_name || item.entity_key}
                      </div>
                      {item.display_name && item.entity_type !== "market" && (
                        <div className="font-mono-ui text-xs text-text-tertiary">
                          {item.entity_key}
                        </div>
                      )}
                    </div>
                    <Badge tone={directionTone(item.direction)}>
                      {item.market} · {item.direction}
                    </Badge>
                  </div>
                  <p className="mt-2 line-clamp-3 text-sm leading-5 text-muted-foreground">
                    {item.thesis}
                  </p>
                  <div className="mt-3 flex flex-wrap gap-3 text-xs text-text-tertiary">
                    <span>
                      {ft.prediction.colConfidence} {pct(item.confidence)}
                    </span>
                    <span>{item.horizons.map((h) => `${h}D`).join(" / ")}</span>
                    <span>
                      {item.pending_checkpoints} {ft.prediction.pending}
                    </span>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <h3 className="font-mondwest text-base">{ft.prediction.evaluation}</h3>
        <div className="space-y-4">
          <MetricsTable
            title={ft.prediction.byMarket}
            rows={summary.by_market}
          />
          <MetricsTable
            title={ft.prediction.byHorizon}
            rows={summary.by_horizon}
            horizon
          />
        </div>
        <Card>
          <CardHeader>
            <CardTitle>{ft.prediction.recent}</CardTitle>
          </CardHeader>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full min-w-[760px] text-sm">
              <thead className="border-y border-border text-left text-xs text-text-tertiary">
                <tr>
                  <th className="px-4 py-2 font-normal">
                    {ft.prediction.colEntity}
                  </th>
                  <th className="px-4 py-2 font-normal">
                    {ft.prediction.colDirection}
                  </th>
                  <th className="px-4 py-2 text-right font-normal">
                    {ft.prediction.colHorizon}
                  </th>
                  <th className="px-4 py-2 text-right font-normal">
                    {ft.prediction.colReturn}
                  </th>
                  <th className="px-4 py-2 text-right font-normal">
                    {ft.prediction.colPath}
                  </th>
                  <th className="px-4 py-2 text-right font-normal">
                    {ft.prediction.colStatus}
                  </th>
                </tr>
              </thead>
              <tbody>
                {summary.recent_evaluations.map((item) => (
                  <tr
                    className="border-b border-border/60 last:border-0"
                    key={item.evaluation_id}
                  >
                    <td className="px-4 py-2.5">
                      <span className="font-medium">
                        {item.display_name || item.entity_key}
                      </span>
                      {item.display_name && item.entity_type !== "market" && (
                        <span className="ml-2 font-mono-ui text-xs text-text-tertiary">
                          {item.entity_key}
                        </span>
                      )}
                      <span className="ml-2 text-xs text-text-tertiary">
                        {item.market}
                      </span>
                    </td>
                    <td className="px-4 py-2.5">{item.direction}</td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {item.horizon_sessions}D
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {returnPct(item.return_pct)}
                    </td>
                    <td className="px-4 py-2.5 text-right tabular-nums">
                      {returnPct(item.mfe_pct)} / {returnPct(item.mae_pct)}
                    </td>
                    <td className="px-4 py-2.5 text-right">
                      <Badge tone={evaluationTone(item.state)}>
                        {item.state}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      </section>

      <div className="text-right text-xs text-text-tertiary">
        {ft.prediction.generatedAt} ·{" "}
        {new Date(summary.generated_at).toLocaleString()}
      </div>
    </div>
  );
}

export function StrategyBacktests() {
  const ft = useFinanceT();
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <BarChart3 className="h-5 w-5 text-muted-foreground" />
        <h2 className="font-mondwest text-xl text-foreground">
          {ft.prediction.backtests}
        </h2>
      </div>
      <Card>
        <CardContent className="p-5 text-sm leading-6 text-muted-foreground">
          {ft.prediction.backtestEmpty}
        </CardContent>
      </Card>
    </div>
  );
}
