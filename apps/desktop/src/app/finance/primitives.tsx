import type { ReactNode } from 'react'

import { Badge, type BadgeProps } from '@/components/ui/badge'
import { Codicon } from '@/components/ui/codicon'
import { ErrorBanner } from '@/components/ui/error-state'
import { cn } from '@/lib/utils'

import { parseFinanceError } from './lib'

// Small building blocks shared by the finance tabs. Visual language follows
// the command-center/messaging full views: caption-sized labels in
// --ui-text-tertiary, quinary-surface cards, tabular numerals for figures.

export function FinanceSectionLabel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      className={cn(
        'text-[0.62rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-tertiary)',
        className
      )}
    >
      {children}
    </div>
  )
}

export function FinanceCard({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary) p-3', className)}>
      {children}
    </div>
  )
}

// One stat tile (equity, cash, win rate, …). `tone` colors the value.
export function StatTile({ hint, label, tone, value }: { hint?: string; label: string; tone?: string; value: string }) {
  return (
    <FinanceCard className="min-w-0">
      <div className="truncate text-[0.65rem] font-medium text-(--ui-text-tertiary)" title={label}>
        {label}
      </div>
      <div className={cn('mt-0.5 truncate text-sm font-semibold tabular-nums text-foreground', tone)} title={value}>
        {value}
      </div>
      {hint ? <div className="mt-0.5 truncate text-[0.62rem] text-muted-foreground/70">{hint}</div> : null}
    </FinanceCard>
  )
}

export function FinancePill({ children, variant }: { children: ReactNode; variant?: BadgeProps['variant'] }) {
  return <Badge variant={variant}>{children}</Badge>
}

export function InlineSpinner({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-1.5 py-2 text-xs text-muted-foreground">
      <Codicon name="loading" size="0.75rem" spinning />
      {label}
    </div>
  )
}

// Wraps one query's lifecycle so every finance section renders loading/error/
// empty the same way and a failed section never blanks the whole tab.
export function QuerySection({
  children,
  empty,
  error,
  isEmpty,
  loading
}: {
  children: ReactNode
  empty: string
  error?: unknown
  isEmpty: boolean
  loading: boolean
}) {
  if (loading) {
    return <InlineSpinner />
  }

  if (error) {
    return <ErrorBanner>{parseFinanceError(error).message}</ErrorBanner>
  }

  if (isEmpty) {
    return <div className="py-2 text-xs text-muted-foreground">{empty}</div>
  }

  return <>{children}</>
}

// Minimal data table in the desktop caption style; wide content scrolls
// inside its own container so tabs never overflow the page.
export function FinanceTable({
  columns,
  rows
}: {
  columns: ReadonlyArray<{ align?: 'left' | 'right'; label: string }>
  rows: ReadonlyArray<{ cells: ReactNode[]; key: string }>
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-(--ui-stroke-tertiary)">
      <table className="w-full min-w-max border-collapse text-xs">
        <thead>
          <tr className="border-b border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary)">
            {columns.map(column => (
              <th
                className={cn(
                  'px-2.5 py-1.5 text-[0.62rem] font-semibold uppercase tracking-[0.06em] text-(--ui-text-tertiary)',
                  column.align === 'right' ? 'text-right' : 'text-left'
                )}
                key={column.label}
              >
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map(row => (
            <tr className="border-b border-(--ui-stroke-tertiary)/60 last:border-b-0" key={row.key}>
              {/* Cells are positional (one per column), so the index is a stable key. */}
              {row.cells.map((cell, index) => (
                <td
                  className={cn(
                    'px-2.5 py-1.5 tabular-nums',
                    columns[index]?.align === 'right' ? 'text-right' : 'text-left'
                  )}
                  key={index}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
