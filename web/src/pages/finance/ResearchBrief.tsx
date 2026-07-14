import { useEffect, useRef, useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowDownRight,
  ArrowUpRight,
  HelpCircle,
  Layers,
  Microscope,
  Newspaper,
  Radar,
  Search,
  ShieldAlert,
} from "lucide-react";
import { api, FinanceKnowledgeOfflineError } from "@/lib/api";
import type {
  FinanceBriefMover,
  FinanceBriefRegime,
  FinanceBriefRisk,
  FinanceKnowledgeHit,
  FinanceResearchBrief,
  FinanceResearchMarket,
} from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
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
          <ShieldAlert className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.risk.title}</CardTitle>
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
                { label: ft.brief.risk.equity, value: fmtMoney(risk.equity) },
                { label: ft.brief.risk.cash, value: fmtMoney(risk.cash) },
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
        <div className="flex items-center gap-2">
          <Activity className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.regime.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
        {regime === null ? (
          <p className="font-mondwest normal-case py-2 text-sm text-muted-foreground">
            {ft.brief.regime.unavailable}
          </p>
        ) : (
          <div className="flex flex-wrap items-center gap-3 font-mondwest normal-case text-sm">
            <Badge tone={regimeTone(regime.risk_on_off)}>
              {regime.risk_on_off}
            </Badge>
            <span className="text-muted-foreground">
              {ft.brief.regime.vix}{" "}
              <span className="text-foreground">
                {regime.vix === null ? "—" : regime.vix.toFixed(1)}
              </span>
            </span>
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
        <div className="flex items-center gap-2">
          <Radar className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.movers.title}</CardTitle>
        </div>
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
        <div className="flex items-center gap-2">
          <Layers className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.themes.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
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
      <CardContent>
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
        <div className="flex items-center gap-2">
          <Activity className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{ft.brief.signals.title}</CardTitle>
        </div>
      </CardHeader>
      <CardContent>
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
                  <Badge tone={directionTone(s.direction)}>{s.direction}</Badge>
                  <Badge tone={s.source_agent === "debate" ? "default" : "outline"}>
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
}: {
  brief: FinanceResearchBrief | null;
  market?: FinanceResearchMarket;
  onMarketChange?: (m: FinanceResearchMarket) => void;
}) {
  const ft = useFinanceT();
  const researchOnly = market === "cn";

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

      {/* Account-risk strip is US-desk only — the China/HK desk is
          research-only with no account (`risk` is null). */}
      {!researchOnly && <RiskStrip risk={brief.risk} ft={ft} />}
      <RegimeChips regime={brief.regime} ft={ft} />

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
          <div className="flex items-center gap-2">
            <HelpCircle className="h-5 w-5 text-muted-foreground" />
            <CardTitle className="text-base">
              {ft.brief.uncertainty.title}
            </CardTitle>
          </div>
        </CardHeader>
        <CardContent>
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
