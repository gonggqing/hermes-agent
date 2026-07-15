import { useQuery } from '@tanstack/react-query'
import { useEffect, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import {
  addResearchWatchlistMember,
  deleteResearchWatchlist,
  type FinanceInstrumentMatch,
  type FinanceResearchWatchlist,
  type FinanceResearchWatchlistRecommendation,
  getResearchWatchlistRecommendations,
  removeResearchWatchlistMember,
  renameResearchWatchlist,
  searchInstruments
} from '@/hermes'
import { useI18n } from '@/i18n'
import { Plus, Search, Starmap, X } from '@/lib/icons'
import { notifyError } from '@/store/notifications'

import { useDebounced } from '../hooks/use-debounced'

import { financeKey, type WatchCurrency } from './lib'
import { WatchModulePanel, type WatchSymbolConfig } from './watch'

function watchCurrency(currency: null | string): WatchCurrency | null {
  if (currency === 'USD') {return '$'}

  if (currency === 'CNY') {return '¥'}

  return null
}

function isListedInstrument(item: { exchange: null | string; security_type: null | string }): boolean {
  return (
    (item.security_type === 'stock' || item.security_type === 'etf') &&
    Boolean(item.exchange) &&
    item.exchange?.toUpperCase() !== 'OTC'
  )
}

export function CustomWatchlistPanel({
  enabled,
  group,
  onChange,
  onDelete
}: {
  enabled: boolean
  group: FinanceResearchWatchlist
  onChange: (group: FinanceResearchWatchlist) => void
  onDelete: (id: string) => void
}) {
  const { t } = useI18n()
  const copy = t.finance.watch.custom
  const [query, setQuery] = useState('')
  const debounced = useDebounced(query.trim(), 250)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState(false)
  const [name, setName] = useState(group.name)
  const [confirmDelete, setConfirmDelete] = useState(false)

  useEffect(() => setName(group.name), [group.name])

  const searchQuery = useQuery({
    enabled: enabled && debounced.length > 0,
    queryFn: () => searchInstruments({ q: debounced, limit: 8 }),
    queryKey: financeKey('watchlists', 'search', debounced),
    retry: 1
  })

  const recommendationsQuery = useQuery({
    enabled,
    queryFn: getResearchWatchlistRecommendations,
    queryKey: financeKey('watchlists', 'held'),
    retry: 1
  })

  const existing = useMemo(() => new Set(group.members.map(member => member.symbol)), [group.members])

  const symbols = useMemo<WatchSymbolConfig[]>(
    () =>
      group.members.filter(isListedInstrument).map(member => ({
        currency: watchCurrency(member.currency),
        label: member.display_name || member.symbol,
        symbol: member.symbol,
        unit: null
      })),
    [group.members]
  )

  const fail = (error: unknown) =>
    notifyError(error instanceof Error ? error : new Error(String(error)), copy.readOnly)

  const add = async (item: FinanceInstrumentMatch | FinanceResearchWatchlistRecommendation) => {
    if (!isListedInstrument(item)) {return}
    setBusy(true)

    try {
      onChange(
        await addResearchWatchlistMember(group.id, {
          currency: item.currency,
          display_name: item.display_name,
          exchange: 'exchange' in item ? item.exchange : null,
          market: item.market,
          security_type: 'security_type' in item ? item.security_type : null,
          symbol: 'canonical_symbol' in item ? item.canonical_symbol : item.symbol
        })
      )
      setQuery('')
    } catch (error) {
      fail(error)
    } finally {
      setBusy(false)
    }
  }

  const remove = async (symbol: string) => {
    setBusy(true)

    try {
      onChange(await removeResearchWatchlistMember(group.id, symbol))
    } catch (error) {
      fail(error)
    } finally {
      setBusy(false)
    }
  }

  const saveName = async () => {
    if (!name.trim()) {return}
    setBusy(true)

    try {
      onChange(await renameResearchWatchlist(group.id, name))
      setEditing(false)
    } catch (error) {
      fail(error)
    } finally {
      setBusy(false)
    }
  }

  const removeGroup = async () => {
    setBusy(true)

    try {
      await deleteResearchWatchlist(group.id)
      onDelete(group.id)
    } catch (error) {
      fail(error)
      setBusy(false)
    }
  }

  const held = (recommendationsQuery.data ?? [])
    .filter(item => isListedInstrument(item) && !existing.has(item.symbol))
    .slice(0, 8)

  const matches = (searchQuery.data?.matches ?? []).filter(isListedInstrument)

  const headerActions = (
    <>
      {editing ? (
        <>
          <Input
            autoFocus
            className="h-8 w-40"
            onChange={event => setName(event.target.value)}
            onKeyDown={event => event.key === 'Enter' && void saveName()}
            value={name}
          />
          <Button disabled={busy || !name.trim()} onClick={() => void saveName()} size="xs">
            {t.common.save}
          </Button>
          <Button onClick={() => setEditing(false)} size="xs" variant="ghost">
            {t.common.cancel}
          </Button>
        </>
      ) : (
        <Button onClick={() => setEditing(true)} size="xs" variant="ghost">
          {copy.rename}
        </Button>
      )}

      {confirmDelete ? (
        <>
          <Button disabled={busy} onClick={() => void removeGroup()} size="xs">
            {t.common.confirm}
          </Button>
          <Button onClick={() => setConfirmDelete(false)} size="xs" variant="ghost">
            {t.common.cancel}
          </Button>
        </>
      ) : (
        <Button onClick={() => setConfirmDelete(true)} size="xs" variant="ghost">
          {copy.delete}
        </Button>
      )}

      <Popover>
        <PopoverTrigger asChild>
          <Button size="xs" title={copy.searchPlaceholder} variant="outline">
            <span className="max-w-32 truncate">{copy.add}</span>
          </Button>
        </PopoverTrigger>
        <PopoverContent align="end" className="w-96 max-w-[85vw]">
          <div className="space-y-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-2.5 size-4 text-muted-foreground" />
              <Input
                autoFocus
                className="pl-9"
                onChange={event => setQuery(event.target.value)}
                placeholder={copy.searchPlaceholder}
                value={query}
              />
            </div>

            <div className="max-h-72 overflow-y-auto rounded-md border border-(--ui-stroke-tertiary)">
              {debounced ? (
                searchQuery.isPending ? (
                  <p className="px-3 py-3 text-xs text-muted-foreground">{t.common.loading}</p>
                ) : matches.length === 0 ? (
                  <p className="px-3 py-3 text-xs text-muted-foreground">{copy.noMatches}</p>
                ) : (
                  matches.map(match => (
                    <button
                      className="row-hover flex min-h-11 w-full items-center gap-3 border-b border-(--ui-stroke-tertiary) px-3 py-2 text-left last:border-b-0 disabled:opacity-40"
                      disabled={busy || existing.has(match.canonical_symbol)}
                      key={`${match.market}:${match.canonical_symbol}`}
                      onClick={() => void add(match)}
                      type="button"
                    >
                      <Plus className="size-4 shrink-0" />
                      <span className="min-w-0 flex-1 truncate text-xs">{match.display_name}</span>
                      <span className="font-mono text-[0.65rem] text-muted-foreground">{match.canonical_symbol}</span>
                    </button>
                  ))
                )
              ) : (
                <>
                  {group.members.map(member => (
                    <div
                      className="flex min-h-10 items-center gap-3 border-b border-(--ui-stroke-tertiary) px-3 py-2 last:border-b-0"
                      key={member.symbol}
                    >
                      <span className="min-w-0 flex-1 truncate text-xs">{member.display_name || member.symbol}</span>
                      <span className="font-mono text-[0.65rem] text-muted-foreground">{member.symbol}</span>
                      <button
                        aria-label={`${copy.remove}: ${member.symbol}`}
                        disabled={busy}
                        onClick={() => void remove(member.symbol)}
                        title={copy.remove}
                        type="button"
                      >
                        <X className="size-3.5 text-muted-foreground hover:text-destructive" />
                      </button>
                    </div>
                  ))}
                  {held.map(item => (
                    <button
                      className="row-hover flex min-h-10 w-full items-center gap-3 border-t border-(--ui-stroke-tertiary) px-3 py-2 text-left disabled:opacity-40"
                      disabled={busy}
                      key={item.symbol}
                      onClick={() => void add(item)}
                      type="button"
                    >
                      <Plus className="size-4 shrink-0" />
                      <span className="min-w-0 flex-1 truncate text-xs">{item.display_name}</span>
                      <span className="font-mono text-[0.65rem] text-muted-foreground">{item.symbol}</span>
                    </button>
                  ))}
                  {group.members.length === 0 && held.length === 0 && (
                    <p className="px-3 py-3 text-xs text-muted-foreground">{copy.empty}</p>
                  )}
                </>
              )}
            </div>
          </div>
        </PopoverContent>
      </Popover>
    </>
  )

  return (
    <WatchModulePanel
      enabled={enabled}
      headerActions={headerActions}
      symbols={symbols}
      title={group.name}
      titleIcon={<Starmap className="size-4 text-muted-foreground" />}
    />
  )
}
