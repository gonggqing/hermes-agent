// Phase 0.9 Portfolio sub-area: the user's REAL multi-account holdings
// (US/HK/CN), separate from the paper-trading account in FinancePage. A
// hand-built master-detail (reusing MasterDetail/SidebarGroup/SidebarButton)
// with Accounts / Holdings / Activity / Reconciliation + Drafts review +
// CSV import. READ + DRAFT only — the only writes are creating a draft and
// the human confirm/edit/reject action (Loop.md §3). Self-contained: its own
// plain-fetch + useState loaders (no react-query, no nanostores), mirroring
// the FinancePage house style.

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  Check,
  ListChecks,
  Pencil,
  Plus,
  RefreshCw,
  Search,
  Upload,
  Wallet,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type {
  FinanceImportPreview,
  FinanceInstrumentMatch,
  FinancePortfolioAccount,
  FinancePortfolioDraft,
  FinancePortfolioDraftActionOutcome,
  FinancePortfolioDraftStatus,
  FinancePortfolioEvent,
  FinancePortfolioMarket,
  FinancePortfolioReconcile,
  FinancePortfolioValuation,
  FinancePortfolioValuationHolding,
  FinancePortfolioValuationTotal,
} from "@/lib/api";
import type { FinanceTranslations } from "@/i18n/types";
import { cn, randomId } from "@/lib/utils";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Stats } from "@nous-research/ui/ui/components/stats";
import { Switch } from "@nous-research/ui/ui/components/switch";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { MasterDetail, SidebarButton, SidebarGroup } from "./layout";
import { useFinanceT } from "./i18n";
import {
  FINANCE_ACTOR,
  fmtMoney,
  fmtPnlPct,
  fmtQty,
  fmtSigned,
  fmtTs,
  pnlClass,
} from "./format";
import {
  ACCOUNT_TYPE_OPTIONS,
  DRAFT_STATUS_OPTIONS,
  EMPTY_TRADE_FORM,
  EVENT_TYPE_OPTIONS,
  MARKET_OPTIONS,
  PROVIDER_OPTIONS,
  type DraftEditForm,
  type SelectedInstrument,
  type TradeFormDraft,
  accountTypeLabel,
  authorityLabel,
  draftEditFrom,
  draftStatusLabel,
  draftStatusTone,
  eventTypeLabel,
  fmtCostBasis,
  marketLabel,
  parseDraftEdits,
  parseMarkPrice,
  parseTradeForm,
  priceSourceLabel,
  priceSourceTone,
  providerLabel,
  securityTypeLabel,
} from "./portfolio";

type ShowToast = (message: string, type: "error" | "success") => void;

// ── Small shared presentational bits ─────────────────────────────────────

function Loading() {
  return (
    <div className="flex justify-center py-8">
      <Spinner className="text-xl text-primary" />
    </div>
  );
}

function Note({ children }: { children: ReactNode }) {
  return (
    <p className="font-mondwest normal-case py-4 text-sm text-muted-foreground">
      {children}
    </p>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs text-text-tertiary">{label}</span>
      {children}
      {hint && (
        <span className="font-mondwest normal-case text-xs text-text-tertiary">
          {hint}
        </span>
      )}
    </label>
  );
}

// ── Valuation table + totals (shared by per-account and aggregate views) ─

/** Current price + its source tag, or a localized dash + the "unknown" tag
 * when the holding is unpriced (never a fabricated 0). */
function PriceCell({
  holding,
  ft,
}: {
  holding: FinancePortfolioValuationHolding;
  ft: FinanceTranslations;
}) {
  const v = ft.portfolio.valuation;
  return (
    <div className="flex items-center justify-end gap-2">
      <span className={holding.price === null ? "text-text-tertiary" : ""}>
        {holding.price === null ? v.unknown : fmtMoney(holding.price)}
      </span>
      <Badge tone={priceSourceTone(holding.price_source)}>
        {priceSourceLabel(holding.price_source, ft)}
      </Badge>
    </div>
  );
}

function ValuationTable({
  holdings,
  showAccounts,
  emptyText,
  onSetMark,
  savingSymbol,
  ft,
}: {
  holdings: FinancePortfolioValuationHolding[];
  showAccounts?: boolean;
  emptyText: string;
  onSetMark: (
    symbol: string,
    currency: string,
    price: number,
  ) => Promise<boolean>;
  savingSymbol: string | null;
  ft: FinanceTranslations;
}) {
  const h = ft.portfolio.holdings;
  const v = ft.portfolio.valuation;
  // Inline "更新现价" editor state — one row at a time (ApprovalQueue pattern).
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [editError, setEditError] = useState<string | null>(null);

  if (holdings.length === 0) return <Note>{emptyText}</Note>;

  const startEdit = (row: FinancePortfolioValuationHolding) => {
    setEditing(row.symbol);
    setDraft(row.price === null ? "" : String(row.price));
    setEditError(null);
  };
  const cancelEdit = () => {
    setEditing(null);
    setEditError(null);
  };
  const save = async (row: FinancePortfolioValuationHolding) => {
    const parsed = parseMarkPrice(draft, ft);
    if (typeof parsed === "string") {
      setEditError(parsed);
      return;
    }
    const ok = await onSetMark(row.symbol, row.currency, parsed);
    if (ok) cancelEdit();
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mondwest normal-case text-sm">
        <thead>
          <tr className="border-b border-border text-muted-foreground text-xs">
            <th className="text-left py-2 pr-4 font-medium">{h.symbol}</th>
            <th className="text-left py-2 px-4 font-medium">{h.market}</th>
            <th className="text-right py-2 px-4 font-medium">{h.qty}</th>
            <th className="text-right py-2 px-4 font-medium">{h.avgCost}</th>
            <th className="text-right py-2 px-4 font-medium">{v.price}</th>
            <th className="text-right py-2 px-4 font-medium">{v.marketValue}</th>
            <th className="text-right py-2 px-4 font-medium">{v.pnl}</th>
            <th className="text-right py-2 px-4 font-medium">{v.pnlPct}</th>
            <th className="text-left py-2 px-4 font-medium">{h.currency}</th>
            {showAccounts && (
              <th className="text-left py-2 px-4 font-medium">{h.accounts}</th>
            )}
            <th className="text-right py-2 pl-4 font-medium">
              <span className="sr-only">{v.action}</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {holdings.map((row) => {
            const isEditing = editing === row.symbol;
            const busy = savingSymbol === row.symbol;
            const pnlTone = pnlClass(row.unrealized_pnl);
            return (
              <tr
                key={`${row.symbol}:${row.market ?? ""}`}
                className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
              >
                <td className="py-2 pr-4">
                  {row.display_name ? (
                    <div className="flex flex-col">
                      <span className="text-foreground">
                        {row.display_name}
                      </span>
                      <span className="font-mono-ui text-xs text-muted-foreground">
                        {row.symbol}
                      </span>
                    </div>
                  ) : (
                    <span className="font-mono-ui text-xs">{row.symbol}</span>
                  )}
                </td>
                <td className="py-2 px-4">
                  <Badge tone="outline">{marketLabel(row.market, ft)}</Badge>
                </td>
                <td className="text-right py-2 px-4">{fmtQty(row.qty)}</td>
                <td
                  className={cn(
                    "text-right py-2 px-4",
                    row.cost_basis_known ? "" : "text-text-tertiary italic",
                  )}
                >
                  {fmtCostBasis(row.avg_cost, row.cost_basis_known, ft)}
                </td>
                <td className="text-right py-2 px-4">
                  {isEditing ? (
                    <div className="flex flex-col items-end gap-1">
                      <Input
                        type="number"
                        step="any"
                        min="0"
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        placeholder={v.markPlaceholder}
                        aria-label={v.updateMark}
                        className="w-28 text-right"
                      />
                      {editError && (
                        <span className="text-xs text-destructive">
                          {editError}
                        </span>
                      )}
                    </div>
                  ) : (
                    <PriceCell holding={row} ft={ft} />
                  )}
                </td>
                <td className="text-right py-2 px-4">
                  {row.market_value === null ? (
                    <span className="text-text-tertiary">{v.unknown}</span>
                  ) : (
                    fmtMoney(row.market_value)
                  )}
                </td>
                <td className={cn("text-right py-2 px-4", pnlTone)}>
                  {fmtSigned(row.unrealized_pnl)}
                </td>
                <td className={cn("text-right py-2 px-4", pnlTone)}>
                  {fmtPnlPct(row.pnl_pct)}
                </td>
                <td className="py-2 px-4 text-muted-foreground">
                  {row.currency}
                </td>
                {showAccounts && (
                  <td className="py-2 px-4 text-muted-foreground">
                    {row.account_names.length > 0
                      ? row.account_names.join(", ")
                      : v.unknown}
                  </td>
                )}
                <td className="text-right py-2 pl-4">
                  {isEditing ? (
                    <div className="flex items-center justify-end gap-1">
                      <Button
                        type="button"
                        size="sm"
                        disabled={busy}
                        onClick={() => void save(row)}
                        prefix={busy ? <Spinner /> : <Check />}
                      >
                        {v.markSave}
                      </Button>
                      <Button
                        type="button"
                        size="sm"
                        outlined
                        disabled={busy}
                        onClick={cancelEdit}
                        prefix={<X />}
                      >
                        {v.markCancel}
                      </Button>
                    </div>
                  ) : (
                    <Button
                      type="button"
                      size="sm"
                      ghost
                      disabled={editing !== null}
                      onClick={() => startEdit(row)}
                      prefix={<Pencil />}
                    >
                      {v.updateMark}
                    </Button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── Totals summary (per-currency market value / cost / P&L) ───────────────

function TotalsSummary({
  totals,
  ft,
}: {
  totals: FinancePortfolioValuationTotal[];
  ft: FinanceTranslations;
}) {
  const v = ft.portfolio.valuation;
  if (totals.length === 0) return null;
  return (
    <div className="flex flex-col gap-5">
      {totals.map((t) => {
        const money = (n: number) => `${fmtMoney(n)} ${t.currency}`;
        const items = [
          {
            label: v.totalMarketValue,
            value: {
              key: "mv",
              node: (
                <span className="text-foreground">{money(t.market_value)}</span>
              ),
            },
          },
          {
            label: v.totalCost,
            value: {
              key: "cost",
              node: <span className="text-foreground">{money(t.cost)}</span>,
            },
          },
          {
            label: v.totalPnl,
            value: {
              key: "pnl",
              node: (
                <span className={pnlClass(t.unrealized_pnl)}>
                  {fmtSigned(t.unrealized_pnl)} {t.currency}
                </span>
              ),
            },
          },
          {
            label: v.pnlPct,
            value: {
              key: "pct",
              node: (
                <span className={pnlClass(t.unrealized_pnl)}>
                  {fmtPnlPct(t.pnl_pct)}
                </span>
              ),
            },
          },
          {
            label: v.cash,
            value: {
              key: "cash",
              node: (
                <span className="text-muted-foreground">{money(t.cash)}</span>
              ),
            },
          },
        ];
        return (
          <div key={t.currency} className="flex flex-col gap-2">
            {totals.length > 1 && (
              <span className="font-mono-ui text-xs text-muted-foreground">
                {t.currency}
              </span>
            )}
            <Stats flip items={items} />
            <p className="font-mondwest normal-case text-xs text-text-tertiary">
              {v.pricedCount
                .replace("{priced}", String(t.n_priced))
                .replace("{unpriced}", String(t.n_unpriced))}
            </p>
          </div>
        );
      })}
    </div>
  );
}

// ── Valuation view (per-account & aggregate: totals + P&L holdings) ───────
// Backs both the account "Holdings" tab (accountId set) and the "All
// accounts" aggregate (accountId undefined). Reads the /valuation endpoints
// so every row carries a live/imported/manual price + market value + P&L.

function ValuationView({
  accountId,
  reloadToken,
  showToast,
  ft,
}: {
  accountId?: string;
  reloadToken: number;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const p = ft.portfolio;
  const v = p.valuation;
  const isAggregate = accountId === undefined;
  const [riskOnly, setRiskOnly] = useState(false);
  const [data, setData] = useState<FinancePortfolioValuation | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [localReload, setLocalReload] = useState(0);
  const [refreshing, setRefreshing] = useState(false);
  const [savingSymbol, setSavingSymbol] = useState<string | null>(null);

  // Loader mirrors the house pattern: no synchronous setState in the effect
  // body — the initial `loading` covers first paint; a refetch keeps the
  // stale table on screen until it resolves.
  useEffect(() => {
    let cancelled = false;
    api
      .financePortfolioValuation(accountId, isAggregate ? riskOnly : undefined)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId, isAggregate, riskOnly, reloadToken, localReload]);

  const reload = () => setLocalReload((n) => n + 1);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const res = await api.financeRefreshMarks();
      if (!res.ok || res.data === null) {
        showToast(v.refreshFailed.replace("{message}", res.error), "error");
        return;
      }
      const r = res.data;
      showToast(
        v.refreshDone
          .replace("{refreshed}", String(r.refreshed.length))
          .replace("{skipped}", String(r.skipped.length))
          .replace("{failed}", String(r.failed.length)),
        "success",
      );
      reload();
    } catch (err) {
      showToast(v.refreshFailed.replace("{message}", String(err)), "error");
    } finally {
      setRefreshing(false);
    }
  };

  const setMark = async (
    symbol: string,
    currency: string,
    price: number,
  ): Promise<boolean> => {
    setSavingSymbol(symbol);
    try {
      const res = await api.financeSetMark({
        symbol,
        price,
        currency,
        source: "manual",
        actor: FINANCE_ACTOR,
      });
      if (!res.ok || res.data === null) {
        showToast(v.markFailed.replace("{message}", res.error), "error");
        return false;
      }
      showToast(v.markSaved.replace("{symbol}", symbol), "success");
      reload();
      return true;
    } catch (err) {
      showToast(v.markFailed.replace("{message}", String(err)), "error");
      return false;
    } finally {
      setSavingSymbol(null);
    }
  };

  // Bug fix: render account NAMES, never the raw UUIDs from accounts[].id.
  const accountNames = data?.accounts?.map((a) => a.name) ?? [];

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            {isAggregate && <Wallet className="h-5 w-5 text-muted-foreground" />}
            <CardTitle className="text-base">
              {isAggregate ? p.aggregate.title : p.holdings.title}
            </CardTitle>
            {isAggregate && data?.accounts && (
              <Badge tone="secondary">
                {p.aggregate.accountsCount.replace(
                  "{n}",
                  String(data.accounts.length),
                )}
              </Badge>
            )}
            <div className="ml-auto flex flex-wrap items-center gap-3">
              <Button
                type="button"
                size="sm"
                outlined
                disabled={refreshing}
                onClick={() => void refresh()}
                prefix={refreshing ? <Spinner /> : <RefreshCw />}
              >
                {refreshing ? v.refreshing : v.refresh}
              </Button>
              {isAggregate && (
                <label className="flex items-center gap-2 font-mondwest normal-case text-xs text-muted-foreground">
                  <Switch checked={riskOnly} onCheckedChange={setRiskOnly} />
                  {p.aggregate.riskOnly}
                </label>
              )}
            </div>
          </div>
          {isAggregate && accountNames.length > 0 && (
            <p className="font-mondwest normal-case text-xs text-muted-foreground">
              {accountNames.join(" · ")}
            </p>
          )}
          {data && (
            <p className="font-mondwest normal-case text-xs text-text-tertiary">
              {p.holdings.asOf.replace("{time}", fmtTs(data.as_of))}
            </p>
          )}
        </CardHeader>
        <CardContent>
          {loading && data === null ? (
            <Loading />
          ) : error && data === null ? (
            <Note>{p.loadError}</Note>
          ) : (
            <div className="flex flex-col gap-6">
              <TotalsSummary totals={data?.totals ?? []} ft={ft} />
              <ValuationTable
                holdings={data?.holdings ?? []}
                showAccounts={isAggregate}
                emptyText={
                  isAggregate ? p.holdings.emptyAggregate : p.holdings.empty
                }
                onSetMark={setMark}
                savingSymbol={savingSymbol}
                ft={ft}
              />
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

// ── Instrument type-ahead (hand-built — no combobox in @nous-research/ui) ─

function InstrumentTypeahead({
  market,
  query,
  onQueryChange,
  selected,
  onSelect,
  onClear,
  ft,
}: {
  market: FinancePortfolioMarket;
  query: string;
  onQueryChange: (q: string) => void;
  selected: FinanceInstrumentMatch | null;
  onSelect: (match: FinanceInstrumentMatch) => void;
  onClear: () => void;
  ft: FinanceTranslations;
}) {
  const r = ft.portfolio.record;
  const [matches, setMatches] = useState<FinanceInstrumentMatch[]>([]);
  const [searching, setSearching] = useState(false);
  const [degraded, setDegraded] = useState(false);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Debounced search — skipped once an instrument is chosen. All state
  // updates run inside the (async) timeout callback so nothing sets state
  // synchronously in the effect body.
  useEffect(() => {
    const q = query.trim();
    if (selected) return;
    let cancelled = false;
    const id = window.setTimeout(
      () => {
        if (q.length < 1) {
          setMatches([]);
          setDegraded(false);
          setSearching(false);
          return;
        }
        setSearching(true);
        api
          .financeInstrumentSearch(q, market, 8)
          .then((res) => {
            if (cancelled) return;
            setMatches(res.matches);
            setDegraded(res.degraded);
          })
          .catch(() => {
            if (cancelled) return;
            setMatches([]);
            setDegraded(false);
          })
          .finally(() => {
            if (!cancelled) setSearching(false);
          });
      },
      q.length < 1 ? 0 : 250,
    );
    return () => {
      cancelled = true;
      window.clearTimeout(id);
    };
  }, [query, market, selected]);

  // Close the results list on an outside click.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!containerRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);

  if (selected) {
    return (
      <div className="flex items-center gap-2 border border-border bg-secondary/20 px-3 py-2">
        <span className="font-mono-ui text-sm text-foreground">
          {selected.canonical_symbol}
        </span>
        <Badge tone="outline">{marketLabel(selected.market, ft)}</Badge>
        <Badge tone="secondary">
          {securityTypeLabel(selected.security_type, ft)}
        </Badge>
        <span className="truncate font-mondwest normal-case text-xs text-muted-foreground">
          {selected.display_name} · {selected.currency}
        </span>
        <Button
          type="button"
          ghost
          size="icon"
          className="ml-auto text-muted-foreground hover:text-foreground"
          aria-label={r.clearInstrument}
          onClick={onClear}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  const showList = open && query.trim().length >= 1;

  return (
    <div ref={containerRef} className="relative">
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
        <Input
          value={query}
          onChange={(e) => {
            onQueryChange(e.target.value);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          placeholder={r.instrumentPlaceholder}
          className="pl-8"
          aria-label={r.instrument}
          autoComplete="off"
        />
      </div>
      {showList && (
        <div className="absolute z-50 mt-1 max-h-60 w-full overflow-auto border border-border bg-background shadow-lg">
          {degraded && (
            <p className="border-b border-border/50 px-3 py-2 font-mondwest normal-case text-xs text-warning">
              {r.degraded}
            </p>
          )}
          {searching ? (
            <p className="px-3 py-3 font-mondwest normal-case text-sm text-muted-foreground">
              {r.searching}
            </p>
          ) : matches.length === 0 ? (
            <p className="px-3 py-3 font-mondwest normal-case text-sm text-muted-foreground">
              {r.noMatches}
            </p>
          ) : (
            matches.map((m) => (
              <button
                key={m.provider_id || `${m.canonical_symbol}:${m.exchange}`}
                type="button"
                // onMouseDown (not onClick) so selection wins the race with
                // the input's blur / the outside-click closer.
                onMouseDown={(e) => {
                  e.preventDefault();
                  onSelect(m);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 border-b border-border/50 px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-secondary/30"
              >
                <span className="font-mono-ui text-xs text-foreground">
                  {m.canonical_symbol}
                </span>
                <Badge tone="outline">{marketLabel(m.market, ft)}</Badge>
                <span className="min-w-0 flex-1 truncate font-mondwest normal-case text-xs text-muted-foreground">
                  {m.display_name}
                </span>
                <span className="font-mondwest normal-case text-xs text-text-tertiary">
                  {securityTypeLabel(m.security_type, ft)} · {m.currency}
                </span>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ── Record trade / opening position (draft + confirm in one submit) ──────

function RecordTradeForm({
  account,
  onRecorded,
  showToast,
  ft,
}: {
  account: FinancePortfolioAccount;
  onRecorded: () => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const r = ft.portfolio.record;
  const [form, setForm] = useState<TradeFormDraft>(EMPTY_TRADE_FORM);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<FinanceInstrumentMatch | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const set = (patch: Partial<TradeFormDraft>) =>
    setForm((f) => ({ ...f, ...patch }));

  const reset = () => {
    setForm(EMPTY_TRADE_FORM);
    setQuery("");
    setSelected(null);
    setFormError(null);
  };

  const submit = async () => {
    const instrument: SelectedInstrument | null = selected
      ? {
          symbol: selected.canonical_symbol,
          market: selected.market,
          currency: selected.currency,
        }
      : null;
    const parsed = parseTradeForm(form, instrument, ft);
    if (typeof parsed === "string") {
      setFormError(parsed);
      return;
    }
    setFormError(null);
    setBusy(true);
    try {
      // 1) Create the draft (surface web + a human created_by).
      const created = await api.financePortfolioCreateDraft({
        ...parsed,
        account_id: account.id,
        created_by: FINANCE_ACTOR,
      });
      if (!created.ok || created.data === null) {
        showToast(r.failedDraft.replace("{message}", created.error), "error");
        return;
      }
      // 2) Confirm it as a human action → turns the draft into a holding.
      const draft = created.data;
      const outcome = await api.financePortfolioDraftAction(draft.id, {
        action: "confirm",
        actor: FINANCE_ACTOR,
        idempotency_key: randomId(),
        expected_version: draft.version,
      });
      if (outcome.code === "applied" || outcome.code === "replayed") {
        showToast(
          r.recorded.replace("{symbol}", parsed.symbol ?? draft.symbol ?? ""),
          "success",
        );
        reset();
        onRecorded();
      } else {
        showToast(
          r.failedConfirm.replace(
            "{message}",
            outcome.message || outcome.code,
          ),
          "error",
        );
        onRecorded();
      }
    } catch (err) {
      showToast(r.requestFailed.replace("{error}", String(err)), "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{r.title}</CardTitle>
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {r.subtitle}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Field label={r.instrument}>
          <InstrumentTypeahead
            market={account.market_scope}
            query={query}
            onQueryChange={setQuery}
            selected={selected}
            onSelect={setSelected}
            onClear={() => {
              setSelected(null);
              setQuery("");
            }}
            ft={ft}
          />
        </Field>

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={r.eventType}>
            <Select
              value={form.eventType}
              onValueChange={(v) => set({ eventType: v })}
            >
              {EVENT_TYPE_OPTIONS.map((e) => (
                <SelectOption key={e} value={e}>
                  {eventTypeLabel(e, ft)}
                </SelectOption>
              ))}
            </Select>
          </Field>
          <Field label={r.qty}>
            <Input
              type="number"
              step="any"
              min="0"
              value={form.qty}
              onChange={(e) => set({ qty: e.target.value })}
            />
          </Field>
          <Field label={r.price} hint={r.priceHint}>
            <Input
              type="number"
              step="any"
              min="0"
              value={form.price}
              onChange={(e) => set({ price: e.target.value })}
            />
          </Field>
          <Field label={r.commission}>
            <Input
              type="number"
              step="any"
              min="0"
              value={form.commission}
              onChange={(e) => set({ commission: e.target.value })}
            />
          </Field>
          <Field label={r.occurredAt}>
            <Input
              type="datetime-local"
              value={form.occurredAt}
              onChange={(e) => set({ occurredAt: e.target.value })}
            />
          </Field>
          <Field label={r.note}>
            <Input
              value={form.note}
              onChange={(e) => set({ note: e.target.value })}
              placeholder={r.notePlaceholder}
            />
          </Field>
        </div>

        {formError && <p className="text-xs text-destructive">{formError}</p>}

        <div>
          <Button
            type="button"
            size="sm"
            disabled={busy}
            onClick={() => void submit()}
            prefix={busy ? <Spinner /> : <Check />}
          >
            {busy ? r.submitting : r.submit}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Per-account activity (event ledger) ──────────────────────────────────

function ActivityView({
  accountId,
  reloadToken,
  ft,
}: {
  accountId: string;
  reloadToken: number;
  ft: FinanceTranslations;
}) {
  const [events, setEvents] = useState<FinancePortfolioEvent[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const a = ft.portfolio.activity;

  useEffect(() => {
    let cancelled = false;
    api
      .financePortfolioEvents(accountId)
      .then((d) => {
        if (!cancelled) {
          setEvents(d);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId, reloadToken]);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{a.title}</CardTitle>
      </CardHeader>
      <CardContent>
        {loading && events === null ? (
          <Loading />
        ) : error && events === null ? (
          <Note>{ft.portfolio.loadError}</Note>
        ) : events === null || events.length === 0 ? (
          <Note>{a.empty}</Note>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mondwest normal-case text-sm">
              <thead>
                <tr className="border-b border-border text-muted-foreground text-xs">
                  <th className="text-left py-2 pr-4 font-medium">{a.colTime}</th>
                  <th className="text-left py-2 px-4 font-medium">{a.colType}</th>
                  <th className="text-left py-2 px-4 font-medium">
                    {a.colSymbol}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">{a.colQty}</th>
                  <th className="text-right py-2 px-4 font-medium">
                    {a.colPrice}
                  </th>
                  <th className="text-right py-2 px-4 font-medium">
                    {a.colAmount}
                  </th>
                  <th className="text-left py-2 px-4 font-medium">
                    {a.colSource}
                  </th>
                  <th className="text-left py-2 pl-4 font-medium">{a.colNote}</th>
                </tr>
              </thead>
              <tbody>
                {events.map((e, i) => (
                  <tr
                    key={`${e.occurred_at}:${e.symbol ?? ""}:${i}`}
                    className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                  >
                    <td className="py-2 pr-4 text-text-tertiary">
                      {fmtTs(e.occurred_at)}
                    </td>
                    <td className="py-2 px-4">
                      <Badge tone="secondary">{eventTypeLabel(e.event_type, ft)}</Badge>
                    </td>
                    <td className="py-2 px-4">
                      {e.symbol ? (
                        <span className="font-mono-ui text-xs">{e.symbol}</span>
                      ) : (
                        <span className="text-text-tertiary">—</span>
                      )}
                    </td>
                    <td className="text-right py-2 px-4">
                      {e.qty === null ? "—" : fmtQty(e.qty)}
                    </td>
                    <td className="text-right py-2 px-4">{fmtMoney(e.price)}</td>
                    <td className="text-right py-2 px-4">{fmtMoney(e.amount)}</td>
                    <td className="py-2 px-4 text-muted-foreground">{e.source}</td>
                    <td className="py-2 pl-4 text-muted-foreground">
                      {e.note || "—"}
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

// ── Per-account reconciliation ───────────────────────────────────────────

function ReconcileView({
  accountId,
  reloadToken,
  ft,
}: {
  accountId: string;
  reloadToken: number;
  ft: FinanceTranslations;
}) {
  const [data, setData] = useState<FinancePortfolioReconcile | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const rc = ft.portfolio.reconcile;

  useEffect(() => {
    let cancelled = false;
    api
      .financePortfolioReconcile(accountId)
      .then((d) => {
        if (!cancelled) {
          setData(d);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [accountId, reloadToken]);

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">{rc.title}</CardTitle>
          {data && (
            <>
              <Badge tone={data.ok ? "success" : "warning"}>
                {data.ok ? rc.inSync : rc.drift}
              </Badge>
              <span className="font-mondwest normal-case text-xs text-muted-foreground">
                {rc.authority}: {authorityLabel(data.authority, ft)}
              </span>
              <span className="ml-auto font-mondwest normal-case text-xs text-text-tertiary">
                {rc.asOf.replace("{time}", fmtTs(data.as_of))}
              </span>
            </>
          )}
        </div>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        {loading && data === null ? (
          <Loading />
        ) : error || data === null ? (
          <Note>{rc.unavailable}</Note>
        ) : (
          <>
            {data.summary && (
              <p className="font-mondwest normal-case text-sm text-foreground">
                {data.summary}
              </p>
            )}
            {data.note && (
              <p className="font-mondwest normal-case text-xs text-muted-foreground">
                {data.note}
              </p>
            )}
            {data.drifts.length === 0 ? (
              <Note>{rc.noDrift}</Note>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full font-mondwest normal-case text-sm">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground text-xs">
                      <th className="text-left py-2 pr-4 font-medium">
                        {rc.driftSymbol}
                      </th>
                      <th className="text-right py-2 px-4 font-medium">
                        {rc.portfolioQty}
                      </th>
                      <th className="text-right py-2 pl-4 font-medium">
                        {rc.brokerQty}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.drifts.map((d) => (
                      <tr
                        key={d.symbol}
                        className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                      >
                        <td className="py-2 pr-4">
                          <span className="font-mono-ui text-xs">{d.symbol}</span>
                        </td>
                        <td className="text-right py-2 px-4">
                          {fmtQty(d.portfolio_qty)}
                        </td>
                        <td className="text-right py-2 pl-4">
                          {fmtQty(d.broker_qty)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── CSV import (paste → preview → commit) ────────────────────────────────

function ImportView({
  account,
  onCommitted,
  showToast,
  ft,
}: {
  account: FinancePortfolioAccount;
  onCommitted: () => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const imp = ft.portfolio.import;
  const [csv, setCsv] = useState("");
  const [preview, setPreview] = useState<FinanceImportPreview | null>(null);
  const [previewing, setPreviewing] = useState(false);
  const [committing, setCommitting] = useState(false);

  const runPreview = async () => {
    if (csv.trim() === "") return;
    setPreviewing(true);
    try {
      const res = await api.financePortfolioImportPreview(account.id, csv);
      setPreview(res);
    } catch (err) {
      setPreview(null);
      showToast(imp.previewFailed.replace("{message}", String(err)), "error");
    } finally {
      setPreviewing(false);
    }
  };

  const commit = async () => {
    setCommitting(true);
    try {
      const res = await api.financePortfolioImportCommit(
        account.id,
        csv,
        FINANCE_ACTOR,
      );
      if (!res.ok || res.data === null) {
        showToast(imp.failed.replace("{message}", res.error), "error");
        return;
      }
      showToast(
        imp.committed
          .replace("{committed}", String(res.data.n_committed))
          .replace("{duplicate}", String(res.data.n_duplicate))
          .replace("{skipped}", String(res.data.n_skipped)),
        "success",
      );
      setCsv("");
      setPreview(null);
      onCommitted();
    } catch (err) {
      showToast(imp.failed.replace("{message}", String(err)), "error");
    } finally {
      setCommitting(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-2">
          <Upload className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{imp.title}</CardTitle>
        </div>
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {imp.columns}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-3">
        <textarea
          value={csv}
          onChange={(e) => {
            setCsv(e.target.value);
            setPreview(null);
          }}
          placeholder={imp.placeholder}
          spellCheck={false}
          className="min-h-[160px] w-full border border-border bg-background/40 px-3 py-2 font-mono-ui text-xs text-foreground transition-colors focus-visible:border-primary/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-primary/30"
        />

        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            size="sm"
            outlined
            disabled={previewing || csv.trim() === ""}
            onClick={() => void runPreview()}
            prefix={previewing ? <Spinner /> : <Search />}
          >
            {previewing ? imp.previewing : imp.preview}
          </Button>
          <Button
            type="button"
            size="sm"
            disabled={committing || preview === null || !preview.committable}
            onClick={() => void commit()}
            prefix={committing ? <Spinner /> : <Check />}
          >
            {committing ? imp.committing : imp.commit}
          </Button>
          {preview === null && csv.trim() === "" && (
            <span className="font-mondwest normal-case text-xs text-text-tertiary">
              {imp.emptyCsv}
            </span>
          )}
        </div>

        {preview && (
          <div className="flex flex-col gap-2">
            {preview.header_error && (
              <p className="text-xs text-destructive">
                {imp.headerError.replace("{message}", preview.header_error)}
              </p>
            )}
            <p className="font-mondwest normal-case text-xs text-muted-foreground">
              {imp.summary
                .replace("{valid}", String(preview.n_valid))
                .replace("{invalid}", String(preview.n_invalid))
                .replace("{duplicate}", String(preview.n_duplicate))}
            </p>
            {!preview.committable && !preview.header_error && (
              <p className="text-xs text-warning">{imp.notCommittable}</p>
            )}
            {preview.rows.length > 0 && (
              <div className="overflow-x-auto">
                <table className="w-full font-mondwest normal-case text-sm">
                  <thead>
                    <tr className="border-b border-border text-muted-foreground text-xs">
                      <th className="text-right py-2 pr-4 font-medium">
                        {imp.colLine}
                      </th>
                      <th className="text-left py-2 px-4 font-medium">
                        {imp.colStatus}
                      </th>
                      <th className="text-left py-2 px-4 font-medium">
                        {imp.colType}
                      </th>
                      <th className="text-left py-2 px-4 font-medium">
                        {imp.colSymbol}
                      </th>
                      <th className="text-right py-2 px-4 font-medium">
                        {imp.colQty}
                      </th>
                      <th className="text-right py-2 px-4 font-medium">
                        {imp.colPrice}
                      </th>
                      <th className="text-right py-2 px-4 font-medium">
                        {imp.colAmount}
                      </th>
                      <th className="text-left py-2 pl-4 font-medium">
                        {imp.colErrors}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((row) => (
                      <tr
                        key={row.line}
                        className="border-b border-border/50 hover:bg-secondary/20 transition-colors"
                      >
                        <td className="text-right py-2 pr-4 text-text-tertiary">
                          {row.line}
                        </td>
                        <td className="py-2 px-4">
                          <Badge
                            tone={
                              !row.ok
                                ? "destructive"
                                : row.duplicate
                                  ? "warning"
                                  : "success"
                            }
                          >
                            {!row.ok
                              ? imp.rowInvalid
                              : row.duplicate
                                ? imp.rowDuplicate
                                : imp.rowOk}
                          </Badge>
                        </td>
                        <td className="py-2 px-4 text-muted-foreground">
                          {row.event_type || "—"}
                        </td>
                        <td className="py-2 px-4">
                          {row.symbol ? (
                            <span className="font-mono-ui text-xs">
                              {row.symbol}
                            </span>
                          ) : (
                            "—"
                          )}
                        </td>
                        <td className="text-right py-2 px-4">
                          {row.qty === null ? "—" : fmtQty(row.qty)}
                        </td>
                        <td className="text-right py-2 px-4">
                          {fmtMoney(row.price)}
                        </td>
                        <td className="text-right py-2 px-4">
                          {fmtMoney(row.amount)}
                        </td>
                        <td className="py-2 pl-4 text-xs text-destructive">
                          {row.errors.join("; ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Account settings (update) ────────────────────────────────────────────

function SettingsForm({
  account,
  onSaved,
  showToast,
  ft,
}: {
  account: FinancePortfolioAccount;
  onSaved: () => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const s = ft.portfolio.settings;
  const [name, setName] = useState(account.name);
  const [accountType, setAccountType] = useState(account.account_type);
  const [includeInRisk, setIncludeInRisk] = useState(account.include_in_risk);
  const [note, setNote] = useState(account.note);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    try {
      const res = await api.financePortfolioUpdateAccount(account.id, {
        name: name.trim() || account.name,
        account_type: accountType,
        include_in_risk: includeInRisk,
        note: note.trim(),
        actor: FINANCE_ACTOR,
      });
      if (!res.ok) {
        showToast(s.failed.replace("{message}", res.error), "error");
        return;
      }
      showToast(s.saved, "success");
      onSaved();
    } catch (err) {
      showToast(s.failed.replace("{message}", String(err)), "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{s.title}</CardTitle>
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {s.meta
            .replace("{provider}", providerLabel(account.provider, ft))
            .replace("{market}", marketLabel(account.market_scope, ft))
            .replace("{currency}", account.base_currency)}
          {" · "}
          {s.created.replace("{time}", fmtTs(account.created_at))}
        </p>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Field label={s.name}>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={s.accountType}>
          <Select
            value={accountType}
            onValueChange={(v) =>
              setAccountType(v as FinancePortfolioAccount["account_type"])
            }
          >
            {ACCOUNT_TYPE_OPTIONS.map((a) => (
              <SelectOption key={a} value={a}>
                {accountTypeLabel(a, ft)}
              </SelectOption>
            ))}
          </Select>
        </Field>
        <label className="flex items-center gap-2 font-mondwest normal-case text-sm text-foreground">
          <Switch checked={includeInRisk} onCheckedChange={setIncludeInRisk} />
          {s.includeInRisk}
        </label>
        <Field label={s.note}>
          <Input value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <div>
          <Button
            type="button"
            size="sm"
            disabled={saving}
            onClick={() => void save()}
            prefix={saving ? <Spinner /> : <Check />}
          >
            {saving ? s.saving : s.save}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Account detail (sub-tabbed) ──────────────────────────────────────────

type AccountTab =
  | "holdings"
  | "record"
  | "activity"
  | "reconcile"
  | "import"
  | "settings";

const ACCOUNT_TABS: AccountTab[] = [
  "holdings",
  "record",
  "activity",
  "reconcile",
  "import",
  "settings",
];

function AccountDetail({
  account,
  reloadToken,
  onChanged,
  showToast,
  ft,
}: {
  account: FinancePortfolioAccount;
  reloadToken: number;
  onChanged: () => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const [tab, setTab] = useState<AccountTab>("holdings");
  const [localBump, setLocalBump] = useState(0);
  const token = reloadToken + localBump;
  const p = ft.portfolio;

  // A local mutation (record trade / import) refreshes this account's data;
  // onChanged also refreshes the account list + aggregate at the top.
  const mutated = () => {
    setLocalBump((n) => n + 1);
    onChanged();
  };

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <CardTitle className="text-base">{account.name}</CardTitle>
            <Badge tone="outline">{marketLabel(account.market_scope, ft)}</Badge>
            <Badge tone="secondary">
              {accountTypeLabel(account.account_type, ft)}
            </Badge>
            {account.include_in_risk && (
              <Badge tone="success">{p.includeInRisk}</Badge>
            )}
            <span className="font-mondwest normal-case text-xs text-muted-foreground">
              {providerLabel(account.provider, ft)} · {account.base_currency}
            </span>
          </div>
        </CardHeader>
      </Card>

      <Segmented<AccountTab>
        value={tab}
        onChange={setTab}
        options={ACCOUNT_TABS.map((t) => ({
          value: t,
          label: p.tabs[t],
        }))}
      />

      {tab === "holdings" && (
        <ValuationView
          accountId={account.id}
          reloadToken={token}
          showToast={showToast}
          ft={ft}
        />
      )}
      {tab === "record" && (
        <RecordTradeForm
          account={account}
          onRecorded={mutated}
          showToast={showToast}
          ft={ft}
        />
      )}
      {tab === "activity" && (
        <ActivityView accountId={account.id} reloadToken={token} ft={ft} />
      )}
      {tab === "reconcile" && (
        <ReconcileView accountId={account.id} reloadToken={token} ft={ft} />
      )}
      {tab === "import" && (
        <ImportView
          account={account}
          onCommitted={mutated}
          showToast={showToast}
          ft={ft}
        />
      )}
      {tab === "settings" && (
        <SettingsForm
          account={account}
          onSaved={onChanged}
          showToast={showToast}
          ft={ft}
        />
      )}
    </div>
  );
}

// ── Add-account form ─────────────────────────────────────────────────────

function AddAccountForm({
  onCreated,
  showToast,
  ft,
}: {
  onCreated: (account: FinancePortfolioAccount) => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const f = ft.portfolio.form;
  const [name, setName] = useState("");
  const [market, setMarket] = useState<FinancePortfolioMarket>("US");
  const [baseCurrency, setBaseCurrency] = useState("USD");
  const [provider, setProvider] = useState("manual");
  const [accountType, setAccountType] = useState("cash");
  const [includeInRisk, setIncludeInRisk] = useState(true);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async () => {
    if (name.trim() === "") {
      setError(f.errName);
      return;
    }
    if (baseCurrency.trim() === "") {
      setError(f.errCurrency);
      return;
    }
    setError(null);
    setSaving(true);
    try {
      const res = await api.financePortfolioCreateAccount({
        name: name.trim(),
        market_scope: market,
        base_currency: baseCurrency.trim().toUpperCase(),
        provider: provider as "manual" | "ibkr",
        account_type: accountType as "cash" | "margin",
        include_in_risk: includeInRisk,
        note: note.trim(),
        actor: FINANCE_ACTOR,
      });
      if (!res.ok || res.data === null) {
        showToast(f.failed.replace("{message}", res.error), "error");
        return;
      }
      showToast(f.created.replace("{name}", res.data.name), "success");
      onCreated(res.data);
    } catch (err) {
      showToast(f.failed.replace("{message}", String(err)), "error");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{f.title}</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label={f.name}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={f.namePlaceholder}
            />
          </Field>
          <Field label={f.market}>
            <Select
              value={market}
              onValueChange={(v) => setMarket(v as FinancePortfolioMarket)}
            >
              {MARKET_OPTIONS.map((m) => (
                <SelectOption key={m} value={m}>
                  {marketLabel(m, ft)}
                </SelectOption>
              ))}
            </Select>
          </Field>
          <Field label={f.baseCurrency}>
            <Input
              value={baseCurrency}
              onChange={(e) => setBaseCurrency(e.target.value)}
              placeholder={f.baseCurrencyPlaceholder}
            />
          </Field>
          <Field label={f.provider}>
            <Select value={provider} onValueChange={setProvider}>
              {PROVIDER_OPTIONS.map((pr) => (
                <SelectOption key={pr} value={pr}>
                  {providerLabel(pr, ft)}
                </SelectOption>
              ))}
            </Select>
          </Field>
          <Field label={f.accountType}>
            <Select value={accountType} onValueChange={setAccountType}>
              {ACCOUNT_TYPE_OPTIONS.map((a) => (
                <SelectOption key={a} value={a}>
                  {accountTypeLabel(a, ft)}
                </SelectOption>
              ))}
            </Select>
          </Field>
          <Field label={f.note}>
            <Input
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder={f.notePlaceholder}
            />
          </Field>
        </div>

        <label className="flex items-center gap-2 font-mondwest normal-case text-sm text-foreground">
          <Switch checked={includeInRisk} onCheckedChange={setIncludeInRisk} />
          <span className="flex flex-col">
            {f.includeInRisk}
            <span className="text-xs text-text-tertiary">
              {f.includeInRiskHint}
            </span>
          </span>
        </label>

        {error && <p className="text-xs text-destructive">{error}</p>}

        <div>
          <Button
            type="button"
            size="sm"
            disabled={saving}
            onClick={() => void submit()}
            prefix={saving ? <Spinner /> : <Plus />}
          >
            {saving ? f.submitting : f.submit}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

// ── Drafts review (the human-confirmation surface) ───────────────────────

function DraftCard({
  draft,
  busy,
  onAct,
  ft,
}: {
  draft: FinancePortfolioDraft;
  busy: boolean;
  onAct: (
    draft: FinancePortfolioDraft,
    action: "confirm" | "reject" | "edit",
    edits?: DraftEditForm,
  ) => void;
  ft: FinanceTranslations;
}) {
  const d = ft.portfolio.draftsView;
  const [editing, setEditing] = useState(false);
  const [edit, setEdit] = useState<DraftEditForm>(() => draftEditFrom(draft));
  const [editError, setEditError] = useState<string | null>(null);

  const saveAndConfirm = () => {
    const parsed = parseDraftEdits(edit, ft);
    if (typeof parsed === "string") {
      setEditError(parsed);
      return;
    }
    setEditError(null);
    onAct(draft, "edit", edit);
  };

  return (
    <Card>
      <CardContent className="flex flex-col gap-3 py-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-mono-ui text-base font-semibold text-foreground">
            {draft.symbol ?? "—"}
          </span>
          <Badge tone="secondary">{eventTypeLabel(draft.event_type, ft)}</Badge>
          <Badge tone={draftStatusTone(draft.status)}>
            {draftStatusLabel(draft.status, ft)}
          </Badge>
          {draft.market && (
            <Badge tone="outline">{marketLabel(draft.market, ft)}</Badge>
          )}
          <span className="ml-auto font-mondwest normal-case text-xs text-muted-foreground">
            {d.createdBy.replace("{actor}", draft.created_by || "—")} · v
            {draft.version}
          </span>
        </div>

        {editing ? (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            {(["qty", "price", "commission"] as const).map((field) => (
              <label key={field} className="flex flex-col gap-1">
                <span className="text-xs text-text-tertiary">
                  {d.fields[field]}
                </span>
                <Input
                  type="number"
                  step="any"
                  min="0"
                  value={edit[field]}
                  onChange={(e) =>
                    setEdit((s) => ({ ...s, [field]: e.target.value }))
                  }
                />
              </label>
            ))}
            {/* Trade date — editable so a draft missing "time" can be
                completed and then confirmed (was previously unfixable). */}
            <label className="flex flex-col gap-1">
              <span className="text-xs text-text-tertiary">
                {ft.portfolio.record.occurredAt}
              </span>
              <Input
                type="date"
                value={edit.occurredAt}
                onChange={(e) =>
                  setEdit((s) => ({ ...s, occurredAt: e.target.value }))
                }
              />
            </label>
            <label className="flex flex-col gap-1">
              <span className="text-xs text-text-tertiary">{d.fields.note}</span>
              <Input
                type="text"
                value={edit.note}
                onChange={(e) =>
                  setEdit((s) => ({ ...s, note: e.target.value }))
                }
              />
            </label>
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <PairCell label={d.fields.qty} value={fmtQty(draft.qty)} />
            <PairCell label={d.fields.price} value={fmtMoney(draft.price)} />
            <PairCell
              label={d.fields.commission}
              value={fmtMoney(draft.commission)}
            />
            <PairCell
              label={ft.portfolio.record.occurredAt}
              value={fmtTs(draft.occurred_at)}
            />
          </div>
        )}
        {editError && <p className="text-xs text-destructive">{editError}</p>}

        {draft.missing.length > 0 && (
          <p className="font-mondwest normal-case text-xs text-warning">
            {d.missing.replace("{fields}", draft.missing.join(", "))}
          </p>
        )}
        {draft.ambiguities.length > 0 && (
          <p className="font-mondwest normal-case text-xs text-warning">
            {d.ambiguities.replace("{items}", draft.ambiguities.join(", "))}
          </p>
        )}
        {draft.original_text && (
          <p className="font-mondwest normal-case text-xs text-text-tertiary">
            {d.original.replace("{text}", draft.original_text)}
          </p>
        )}

        <div className="flex flex-wrap items-center gap-2">
          {editing ? (
            <>
              <Button
                type="button"
                size="sm"
                disabled={busy}
                onClick={saveAndConfirm}
                prefix={busy ? <Spinner /> : <Check />}
              >
                {d.save}
              </Button>
              <Button
                type="button"
                size="sm"
                outlined
                disabled={busy}
                onClick={() => {
                  setEditing(false);
                  setEdit(draftEditFrom(draft));
                  setEditError(null);
                }}
                prefix={<X />}
              >
                {d.cancel}
              </Button>
            </>
          ) : (
            <>
              <Button
                type="button"
                size="sm"
                disabled={busy}
                onClick={() => onAct(draft, "confirm")}
                prefix={busy ? <Spinner /> : <Check />}
              >
                {d.confirm}
              </Button>
              <Button
                type="button"
                size="sm"
                destructive
                outlined
                disabled={busy}
                onClick={() => onAct(draft, "reject")}
                prefix={<X />}
              >
                {d.reject}
              </Button>
              <Button
                type="button"
                size="sm"
                ghost
                disabled={busy}
                onClick={() => setEditing(true)}
                prefix={<Pencil />}
              >
                {d.edit}
              </Button>
            </>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function PairCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-xs text-text-tertiary">{label}</span>
      <span className="font-mondwest normal-case text-sm text-foreground">
        {value}
      </span>
    </div>
  );
}

function DraftsView({
  reloadToken,
  onChanged,
  showToast,
  ft,
}: {
  reloadToken: number;
  onChanged: () => void;
  showToast: ShowToast;
  ft: FinanceTranslations;
}) {
  const d = ft.portfolio.draftsView;
  const [status, setStatus] = useState<FinancePortfolioDraftStatus>("draft");
  const [drafts, setDrafts] = useState<FinancePortfolioDraft[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [busyKey, setBusyKey] = useState<string | null>(null);
  const [localBump, setLocalBump] = useState(0);
  // One idempotency key per (draft, action), reused on retry (mirror
  // ApprovalQueue) and dropped as soon as the service responds.
  const keysRef = useRef<Map<string, string>>(new Map());

  useEffect(() => {
    let cancelled = false;
    api
      .financePortfolioDrafts(undefined, status)
      .then((rows) => {
        if (!cancelled) {
          setDrafts(rows);
          setError(false);
        }
      })
      .catch(() => {
        if (!cancelled) setError(true);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [status, reloadToken, localBump]);

  const renderOutcome = (
    draft: FinancePortfolioDraft,
    action: "confirm" | "reject" | "edit",
    outcome: FinancePortfolioDraftActionOutcome,
  ) => {
    const o = d.outcome;
    const symbol = draft.symbol ?? "—";
    const refresh = () => {
      setLocalBump((n) => n + 1);
      onChanged();
    };
    switch (outcome.code) {
      case "applied":
        showToast(
          (action === "reject"
            ? o.rejected
            : action === "edit"
              ? o.edited
              : o.confirmed
          ).replace("{symbol}", symbol),
          "success",
        );
        refresh();
        break;
      case "replayed":
        showToast(o.replayed.replace("{symbol}", symbol), "success");
        refresh();
        break;
      case "not_human":
        showToast(o.notHuman.replace("{symbol}", symbol), "error");
        break;
      case "incomplete":
        showToast(
          o.incomplete
            .replace("{symbol}", symbol)
            .replace("{message}", outcome.message || ""),
          "error",
        );
        break;
      case "invalid_edit":
        showToast(
          o.invalidEdit
            .replace("{symbol}", symbol)
            .replace("{message}", outcome.message || ""),
          "error",
        );
        break;
      case "terminal":
        showToast(o.terminal.replace("{symbol}", symbol), "error");
        refresh();
        break;
      case "version_conflict":
        showToast(o.versionConflict.replace("{symbol}", symbol), "error");
        refresh();
        break;
      case "unknown":
        showToast(o.unknown.replace("{symbol}", symbol), "error");
        refresh();
        break;
      case "service_unavailable":
        showToast(
          o.serviceUnavailable.replace("{message}", outcome.message ?? ""),
          "error",
        );
        break;
      default:
        showToast(
          o.unexpected
            .replace("{symbol}", symbol)
            .replace(
              "{message}",
              outcome.message ||
                o.unexpectedFallback.replace("{status}", String(outcome.status)),
            ),
          "error",
        );
        break;
    }
  };

  const act = async (
    draft: FinancePortfolioDraft,
    action: "confirm" | "reject" | "edit",
    edits?: DraftEditForm,
  ) => {
    const actionKey = `${draft.id}:${action}`;
    let idem = keysRef.current.get(actionKey);
    if (!idem) {
      idem = randomId();
      keysRef.current.set(actionKey, idem);
    }
    setBusyKey(actionKey);
    try {
      const parsedEdits =
        action === "edit" && edits
          ? (() => {
              const p = parseDraftEdits(edits, ft);
              return typeof p === "string" ? null : p;
            })()
          : undefined;
      const outcome = await api.financePortfolioDraftAction(draft.id, {
        action,
        actor: FINANCE_ACTOR,
        idempotency_key: idem,
        expected_version: draft.version,
        ...(parsedEdits ? { edits: parsedEdits } : {}),
      });
      keysRef.current.delete(actionKey);
      renderOutcome(draft, action, outcome);
    } catch (err) {
      showToast(
        d.outcome.requestFailed
          .replace("{symbol}", draft.symbol ?? "—")
          .replace("{error}", String(err)),
        "error",
      );
    } finally {
      setBusyKey(null);
    }
  };

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <ListChecks className="h-5 w-5 text-muted-foreground" />
          <CardTitle className="text-base">{d.title}</CardTitle>
          <div className="ml-auto w-40">
            <Select
              value={status}
              onValueChange={(v) =>
                setStatus(v as FinancePortfolioDraftStatus)
              }
              aria-label={d.filterStatus}
            >
              {DRAFT_STATUS_OPTIONS.map((sv) => (
                <SelectOption key={sv} value={sv}>
                  {draftStatusLabel(sv, ft)}
                </SelectOption>
              ))}
            </Select>
          </div>
        </div>
        <p className="font-mondwest normal-case text-xs text-text-tertiary">
          {d.subtitle}
        </p>
      </CardHeader>
      <CardContent>
        {loading && drafts === null ? (
          <Loading />
        ) : error && drafts === null ? (
          <Note>{ft.portfolio.loadError}</Note>
        ) : drafts === null || drafts.length === 0 ? (
          <Note>{d.empty}</Note>
        ) : (
          <div className="flex flex-col gap-3">
            {drafts.map((draft) => (
              <DraftCard
                key={draft.id}
                draft={draft}
                busy={busyKey !== null && busyKey.startsWith(`${draft.id}:`)}
                onAct={(dr, action, edits) => void act(dr, action, edits)}
                ft={ft}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

// ── Portfolio manager (top-level, self-contained) ────────────────────────

// Sidebar selection: aggregate / drafts / add-account / a specific account.
const SEL_AGGREGATE = "aggregate";
const SEL_DRAFTS = "drafts";
const SEL_ADD = "add";
const ACCT_PREFIX = "acct:";

export function PortfolioManager() {
  const { toast, showToast } = useToast();
  const ft = useFinanceT();
  const [accounts, setAccounts] = useState<FinancePortfolioAccount[] | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<string>(SEL_AGGREGATE);
  const [reloadToken, setReloadToken] = useState(0);
  const bump = () => setReloadToken((n) => n + 1);

  // Loader mirrors the FinancePage house pattern: no synchronous setState in
  // the effect body — the initial `loading` state covers the first-paint
  // spinner and every state update happens in an async callback.
  const load = useCallback(() => {
    api
      .financePortfolioAccounts()
      .then((rows) => {
        setAccounts(rows);
        setError(false);
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
    const id = window.setInterval(load, 30_000);
    return () => window.clearInterval(id);
  }, [load]);

  const selectedAccount = useMemo(() => {
    if (!selected.startsWith(ACCT_PREFIX) || accounts === null) return null;
    const id = selected.slice(ACCT_PREFIX.length);
    return accounts.find((a) => a.id === id) ?? null;
  }, [selected, accounts]);

  const p = ft.portfolio;

  const sidebar = (
    <>
      <SidebarGroup label={p.reviewGroup}>
        <SidebarButton
          active={selected === SEL_AGGREGATE}
          onClick={() => setSelected(SEL_AGGREGATE)}
        >
          {p.allAccounts}
        </SidebarButton>
        <SidebarButton
          active={selected === SEL_DRAFTS}
          onClick={() => setSelected(SEL_DRAFTS)}
        >
          {p.drafts}
        </SidebarButton>
      </SidebarGroup>
      <SidebarGroup label={p.accountsGroup}>
        {accounts === null || accounts.length === 0 ? (
          <p className="px-3 py-3 font-mondwest normal-case text-sm text-muted-foreground">
            {p.noAccounts}
          </p>
        ) : (
          accounts.map((a) => (
            <SidebarButton
              key={a.id}
              active={selected === `${ACCT_PREFIX}${a.id}`}
              onClick={() => setSelected(`${ACCT_PREFIX}${a.id}`)}
              trailing={<Badge tone="outline">{marketLabel(a.market_scope, ft)}</Badge>}
              subtitle={`${providerLabel(a.provider, ft)} · ${a.base_currency}`}
            >
              {a.name}
            </SidebarButton>
          ))
        )}
        <SidebarButton
          active={selected === SEL_ADD}
          onClick={() => setSelected(SEL_ADD)}
        >
          <span className="flex items-center gap-1.5">
            <Plus className="h-3.5 w-3.5" />
            {p.addAccount}
          </span>
        </SidebarButton>
      </SidebarGroup>
    </>
  );

  let detail: ReactNode;
  if (loading && accounts === null && !error) {
    detail = <Loading />;
  } else if (error && accounts === null) {
    detail = (
      <Card>
        <CardContent className="py-8">
          <Note>{p.loadError}</Note>
        </CardContent>
      </Card>
    );
  } else if (selected === SEL_ADD) {
    detail = (
      <AddAccountForm
        showToast={showToast}
        ft={ft}
        onCreated={(account) => {
          load();
          setSelected(`${ACCT_PREFIX}${account.id}`);
        }}
      />
    );
  } else if (selected === SEL_DRAFTS) {
    detail = (
      <DraftsView
        reloadToken={reloadToken}
        onChanged={bump}
        showToast={showToast}
        ft={ft}
      />
    );
  } else if (selectedAccount) {
    detail = (
      <AccountDetail
        key={selectedAccount.id}
        account={selectedAccount}
        reloadToken={reloadToken}
        onChanged={() => {
          load();
          bump();
        }}
        showToast={showToast}
        ft={ft}
      />
    );
  } else if (selected.startsWith(ACCT_PREFIX)) {
    // Selected account id no longer present (deleted/renamed away) — hint.
    detail = (
      <Card>
        <CardContent className="py-8">
          <Note>{p.selectHint}</Note>
        </CardContent>
      </Card>
    );
  } else {
    detail = (
      <ValuationView reloadToken={reloadToken} showToast={showToast} ft={ft} />
    );
  }

  return (
    <>
      <MasterDetail sidebar={sidebar}>{detail}</MasterDetail>
      <Toast toast={toast} />
    </>
  );
}
