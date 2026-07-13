import type { ReactNode } from 'react'

import { SegmentedControl } from '@/components/ui/segmented-control'
import type { FinanceMode } from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'

// Shared master-detail chrome for the three Finance views (Research, Queue,
// Portfolio). Rows mirror the messaging sidebar (PlatformRow) so the surfaces
// read as one page; the mode bar is the idiomatic paper/live footer switcher.

export function FinanceListGroup({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div className="mb-3 last:mb-0">
      <div className="px-2 pb-1 text-[0.6rem] font-semibold uppercase tracking-[0.08em] text-(--ui-text-tertiary)">
        {label}
      </div>
      <div className="space-y-0.5">{children}</div>
    </div>
  )
}

// A muted "Phase 0.9 / coming soon" chip for the disabled market placeholders.
export function FinanceComingSoonBadge({ children }: { children: ReactNode }) {
  return (
    <span className="shrink-0 rounded bg-(--ui-bg-quinary) px-1 py-px text-[0.55rem] font-medium text-(--ui-text-tertiary)">
      {children}
    </span>
  )
}

// The one sidebar row shared by every Finance list. Active/disabled styling
// matches messaging's PlatformRow; disabled rows are inert placeholders.
export function FinanceNavRow({
  active,
  badge,
  disabled,
  leading,
  meta,
  onSelect,
  subtitle,
  title,
  titleTone
}: {
  active: boolean
  badge?: ReactNode
  disabled?: boolean
  leading?: ReactNode
  meta?: ReactNode
  onSelect: () => void
  subtitle?: ReactNode
  title: ReactNode
  titleTone?: string
}) {
  return (
    <button
      aria-current={active ? 'true' : undefined}
      className={cn(
        'row-hover flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left',
        disabled
          ? 'cursor-not-allowed text-muted-foreground/45'
          : active
            ? 'bg-(--ui-row-active-background) text-foreground'
            : 'text-(--ui-text-secondary) hover:text-foreground'
      )}
      disabled={disabled}
      onClick={disabled ? undefined : onSelect}
      type="button"
    >
      {leading}
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex items-center gap-1.5">
          <span className={cn('truncate text-[0.8125rem]', active && !disabled ? 'font-medium' : 'font-normal', titleTone)}>
            {title}
          </span>
          {badge}
        </span>
        {subtitle != null && (
          <span className="truncate text-[0.62rem] leading-4 text-muted-foreground/60">{subtitle}</span>
        )}
      </span>
      {meta != null && <span className="shrink-0">{meta}</span>}
    </button>
  )
}

// The paper/live segmented control pinned to the BOTTOM of each master-detail
// view (rendered inside DetailColumn's actionBar). Mode override = null follows
// the service /health.mode; setting it is an explicit override.
export function FinanceModeBar({
  mode,
  modeOverride,
  onModeChange
}: {
  mode: FinanceMode
  modeOverride: FinanceMode | null
  onModeChange: (mode: FinanceMode) => void
}) {
  const { t } = useI18n()
  const copy = t.finance

  return (
    <div aria-label={copy.modeBarAria} className="flex w-full items-center gap-2" role="group">
      <SegmentedControl
        onChange={onModeChange}
        options={[
          { id: 'paper' as const, label: copy.modePaper },
          { id: 'live' as const, label: copy.modeLive }
        ]}
        value={mode}
      />
      <span className="text-[0.62rem] text-muted-foreground/70">
        {modeOverride === null ? copy.modeFollowsService : copy.modeOverridden}
      </span>
    </div>
  )
}

// Centered placeholder shown in the detail column when nothing is selected or
// a list is empty (e.g. the action queue with no pending candidates).
export function FinanceDetailPlaceholder({ children }: { children: ReactNode }) {
  return (
    <div className="grid min-h-64 place-items-center px-4 text-center text-xs leading-5 text-muted-foreground">
      <p className="max-w-sm">{children}</p>
    </div>
  )
}
