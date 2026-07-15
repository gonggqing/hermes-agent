import { useEffect, useMemo, useRef, useState } from "react";
import { Plus, Search, Star, X } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Input } from "@nous-research/ui/ui/components/input";
import { api } from "@/lib/api";
import type {
  FinanceInstrumentMatch,
  FinanceResearchWatchlist,
  FinanceResearchWatchlistRecommendation,
} from "@/lib/api";
import { useI18n } from "@/i18n";
import { useFinanceT } from "./i18n";
import type { WatchCurrency, WatchSymbol } from "./constants";
import { WatchModule } from "./WatchModule";

function displayCurrency(currency: string | null): WatchCurrency | null {
  if (currency === "USD") return "$";
  if (currency === "CNY") return "¥";
  return null;
}

export function ResearchWatchlistDetail({
  group,
  onChange,
  onDelete,
}: {
  group: FinanceResearchWatchlist;
  onChange: (group: FinanceResearchWatchlist) => void;
  onDelete: (id: string) => void;
}) {
  const ft = useFinanceT();
  const { t } = useI18n();
  const [editingName, setEditingName] = useState(false);
  const [name, setName] = useState(group.name);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [matches, setMatches] = useState<FinanceInstrumentMatch[]>([]);
  const [recommendations, setRecommendations] = useState<
    FinanceResearchWatchlistRecommendation[]
  >([]);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pickerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .financeResearchWatchlistRecommendations()
      .then(setRecommendations, () => {});
  }, []);

  useEffect(() => {
    if (!pickerOpen) return;
    const close = (event: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(event.target as Node)) {
        setPickerOpen(false);
      }
    };
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setPickerOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", escape);
    };
  }, [pickerOpen]);

  useEffect(() => {
    const clean = query.trim();
    if (!clean) return;
    let alive = true;
    const timer = window.setTimeout(() => {
      api.financeInstrumentSearch(clean, undefined, 8).then(
        (result) => {
          if (alive) {
            setMatches(result.matches);
            setSearching(false);
          }
        },
        (reason: unknown) => {
          if (alive) {
            setError(reason instanceof Error ? reason.message : String(reason));
            setSearching(false);
          }
        },
      );
    }, 250);
    return () => {
      alive = false;
      window.clearTimeout(timer);
    };
  }, [query]);

  const symbols = useMemo<WatchSymbol[]>(
    () =>
      group.members.map((member) => ({
        symbol: member.symbol,
        label: member.display_name || member.symbol,
        currency: displayCurrency(member.currency),
        unit: null,
      })),
    [group.members],
  );
  const existing = useMemo(
    () => new Set(group.members.map((member) => member.symbol)),
    [group.members],
  );

  const addMember = async (
    item: FinanceInstrumentMatch | FinanceResearchWatchlistRecommendation,
  ) => {
    setBusy(true);
    setError(null);
    try {
      const updated = await api.financeAddResearchWatchlistMember(group.id, {
        symbol: "canonical_symbol" in item ? item.canonical_symbol : item.symbol,
        display_name: item.display_name,
        market: item.market,
        exchange: "exchange" in item ? item.exchange : null,
        currency: item.currency,
        security_type: "security_type" in item ? item.security_type : null,
      });
      setQuery("");
      onChange(updated);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const saveName = async () => {
    if (!name.trim()) return;
    setBusy(true);
    try {
      onChange(await api.financeRenameResearchWatchlist(group.id, name));
      setEditingName(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const removeMember = async (symbol: string) => {
    setBusy(true);
    setError(null);
    try {
      onChange(await api.financeRemoveResearchWatchlistMember(group.id, symbol));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(false);
    }
  };

  const deleteGroup = async () => {
    setBusy(true);
    try {
      await api.financeDeleteResearchWatchlist(group.id);
      onDelete(group.id);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
      setBusy(false);
    }
  };

  const availableRecommendations = recommendations
    .filter((item) => !existing.has(item.symbol))
    .slice(0, 8);

  const actions = (
    <>
      {editingName ? (
        <>
          <Input
            autoFocus
            className="h-8 w-40"
            value={name}
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => event.key === "Enter" && void saveName()}
          />
          <Button size="sm" disabled={busy || !name.trim()} onClick={() => void saveName()}>
            {t.common.save}
          </Button>
          <Button size="sm" ghost onClick={() => setEditingName(false)}>
            {t.common.cancel}
          </Button>
        </>
      ) : (
        <Button size="sm" ghost onClick={() => setEditingName(true)} title={ft.watch.renameCustomGroup}>
          {ft.watch.renameCustomGroup}
        </Button>
      )}

      {confirmDelete ? (
        <>
          <Button size="sm" disabled={busy} onClick={() => void deleteGroup()}>
            {t.common.confirm}
          </Button>
          <Button size="sm" ghost onClick={() => setConfirmDelete(false)}>
            {t.common.cancel}
          </Button>
        </>
      ) : (
        <Button size="sm" ghost onClick={() => setConfirmDelete(true)} title={ft.watch.deleteCustomGroup}>
          {ft.watch.deleteCustomGroup}
        </Button>
      )}

      <div ref={pickerRef} className="relative">
        <Button
          type="button"
          size="sm"
          outlined
          aria-expanded={pickerOpen}
          onClick={() => setPickerOpen((open) => !open)}
        >
          {ft.watch.addInstrument}
        </Button>
        {pickerOpen && (
          <div className="absolute right-0 z-40 mt-1 w-96 max-w-[85vw] border border-border bg-background p-3 shadow-md">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
              <Input
                autoFocus
                className="pl-9"
                value={query}
                placeholder={ft.watch.instrumentSearchPlaceholder}
                onChange={(event) => {
                  const value = event.target.value;
                  setQuery(value);
                  if (!value.trim()) {
                    setMatches([]);
                    setSearching(false);
                  } else {
                    setSearching(true);
                  }
                }}
              />
            </div>

            <div className="mt-3 max-h-72 overflow-y-auto border border-border">
              {query.trim() ? (
                searching ? (
                  <p className="px-3 py-3 text-sm text-muted-foreground">{t.common.loading}</p>
                ) : matches.length === 0 ? (
                  <p className="px-3 py-3 text-sm text-muted-foreground">{ft.watch.noInstrumentMatches}</p>
                ) : (
                  matches.map((match) => {
                    const added = existing.has(match.canonical_symbol);
                    return (
                      <button
                        type="button"
                        key={`${match.market}:${match.canonical_symbol}`}
                        disabled={busy || added}
                        onClick={() => void addMember(match)}
                        className="flex min-h-11 w-full items-center gap-3 border-b border-border/50 px-3 py-2 text-left last:border-b-0 hover:bg-secondary/30 disabled:opacity-40"
                      >
                        <Plus className="h-4 w-4 shrink-0" />
                        <span className="min-w-0 flex-1 truncate text-sm">{match.display_name}</span>
                        <span className="font-mono-ui text-xs text-muted-foreground">{match.canonical_symbol}</span>
                      </button>
                    );
                  })
                )
              ) : (
                <>
                  {group.members.map((member) => (
                    <div key={member.symbol} className="flex min-h-10 items-center gap-3 border-b border-border/50 px-3 py-2 last:border-b-0">
                      <span className="min-w-0 flex-1 truncate text-sm">{member.display_name || member.symbol}</span>
                      <span className="font-mono-ui text-xs text-muted-foreground">{member.symbol}</span>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void removeMember(member.symbol)}
                        aria-label={`${ft.watch.removeInstrument}: ${member.symbol}`}
                        title={ft.watch.removeInstrument}
                        className="rounded p-1 text-muted-foreground hover:text-destructive"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))}
                  {availableRecommendations.map((item) => (
                    <button
                      type="button"
                      key={item.symbol}
                      disabled={busy}
                      onClick={() => void addMember(item)}
                      className="flex min-h-10 w-full items-center gap-3 border-t border-dashed border-border/60 px-3 py-2 text-left hover:bg-secondary/30 disabled:opacity-40"
                    >
                      <Plus className="h-4 w-4 shrink-0" />
                      <span className="min-w-0 flex-1 truncate text-sm">{item.display_name}</span>
                      <span className="font-mono-ui text-xs text-muted-foreground">{item.symbol}</span>
                    </button>
                  ))}
                  {group.members.length === 0 && availableRecommendations.length === 0 && (
                    <p className="px-3 py-3 text-sm text-muted-foreground">{ft.watch.emptyCustomGroup}</p>
                  )}
                </>
              )}
            </div>
            {error && <p className="mt-2 text-xs text-destructive">{error}</p>}
          </div>
        )}
      </div>
    </>
  );

  return (
    <WatchModule
      key={group.id}
      title={group.name}
      customSymbols={symbols}
      headerActions={actions}
      titleIcon={<Star className="h-5 w-5 text-muted-foreground" />}
    />
  );
}
