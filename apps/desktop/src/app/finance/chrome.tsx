import type { ComponentType, ReactNode, SVGProps } from 'react'

import { StatusDot } from '@/components/status-dot'
import { Button } from '@/components/ui/button'
import type { FinanceHealth, FinanceMode } from '@/hermes'
import { useI18n } from '@/i18n'
import { RefreshCw } from '@/lib/icons'
import { cn } from '@/lib/utils'

import { BREAKER_TONE, enumLabel, fmtTs } from './lib'
import { FinancePill } from './primitives'

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

// A small brand-tinted rounded square holding a region/asset glyph — mirrors
// the messaging sidebar's PlatformAvatar so the Finance rows read as one
// system. DESKTOP-ONLY chrome: the web sidebar carries no leading row glyphs.
// Pass an `icon` (lucide/tabler mark) or an `emoji` (flag / asset); `muted`
// renders the neutral, de-saturated chip used for coming-soon placeholders.
export function FinanceRowGlyph({
  color,
  emoji,
  icon: Icon,
  muted
}: {
  color?: string
  emoji?: string
  icon?: ComponentType<SVGProps<SVGSVGElement>>
  muted?: boolean
}) {
  const base = 'inline-grid size-6 shrink-0 place-items-center rounded-md text-[0.8rem] leading-none'

  if (muted || !color) {
    return (
      <span aria-hidden="true" className={cn(base, 'bg-(--ui-bg-tertiary) text-(--ui-text-tertiary)')}>
        {Icon ? <Icon className="size-3.5" /> : <span className="opacity-60 grayscale">{emoji}</span>}
      </span>
    )
  }

  return (
    <span
      aria-hidden="true"
      className={base}
      style={{
        // 16% tint of the accent so the glyph reads on any surface without the
        // chip dominating the row (same recipe as messaging's PlatformAvatar).
        backgroundColor: `color-mix(in srgb, ${color} 16%, transparent)`,
        color
      }}
    >
      {Icon ? <Icon className="size-3.5" /> : emoji}
    </span>
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

// Single paper/live toggle button (replaces the 2-segment SegmentedControl):
// one pill showing the CURRENT mode; clicking switches to the other. Live is
// amber-toned so real-mode is never mistaken for paper. Override = null follows
// the service /health.mode; clicking sets an explicit override.
function ModeToggle({
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
  const live = mode === 'live'
  const next: FinanceMode = live ? 'paper' : 'live'
  const switchLabel = copy.modeSwitchTo(next === 'live' ? copy.modeLive : copy.modePaper)

  return (
    <span className="flex items-center gap-2">
      <button
        aria-label={switchLabel}
        className={cn(
          'inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[0.6875rem] font-semibold uppercase tracking-wide transition-colors',
          live
            ? 'bg-amber-500/15 text-amber-600 ring-1 ring-inset ring-amber-500/40 hover:bg-amber-500/20 dark:text-amber-300'
            : 'bg-(--ui-bg-tertiary) text-(--ui-text-secondary) ring-1 ring-inset ring-(--ui-stroke-tertiary) hover:text-foreground'
        )}
        onClick={() => onModeChange(next)}
        title={switchLabel}
        type="button"
      >
        <StatusDot tone={live ? 'warn' : 'muted'} />
        {live ? copy.modeLive : copy.modePaper}
      </button>
      <span className="text-[0.62rem] text-muted-foreground/70">
        {modeOverride === null ? copy.modeFollowsService : copy.modeOverridden}
      </span>
    </span>
  )
}

// Bottom utility bar pinned below each master-detail view (rendered inside
// DetailColumn's actionBar): the single paper/live toggle on the LEFT, and the
// service status / breaker / loop / last-updated / manual-refresh cluster on
// the RIGHT — relocated here out of the top header so connection state reads as
// chrome, not a filter.
export function FinanceBottomBar({
  health,
  mode,
  modeOverride,
  offline,
  onModeChange,
  onRefresh
}: {
  health: FinanceHealth | undefined
  mode: FinanceMode
  modeOverride: FinanceMode | null
  offline: boolean
  onModeChange: (mode: FinanceMode) => void
  onRefresh: () => void
}) {
  const { t } = useI18n()
  const copy = t.finance

  return (
    <div aria-label={copy.modeBarAria} className="flex w-full flex-wrap items-center gap-x-3 gap-y-1.5" role="group">
      <ModeToggle mode={mode} modeOverride={modeOverride} onModeChange={onModeChange} />

      <div className="ml-auto flex flex-wrap items-center gap-x-2.5 gap-y-1">
        <span className="inline-flex items-center gap-1.5 text-[0.68rem] text-(--ui-text-secondary)">
          <StatusDot tone={offline ? 'bad' : health ? 'good' : 'muted'} />
          {offline ? copy.serviceOffline : health ? copy.serviceOnline : copy.serviceConnecting}
        </span>

        {health && (
          <>
            <FinancePill variant={health.breaker === 'TRIPPED' ? 'destructive' : 'muted'}>
              <StatusDot tone={BREAKER_TONE[health.breaker] ?? 'muted'} />
              {copy.breakerPill(enumLabel(t.finance.enums.breaker, health.breaker))}
            </FinancePill>
            <FinancePill variant={health.loop_attached ? 'default' : 'muted'}>
              {health.loop_attached ? copy.loopAttached : copy.loopIdle}
            </FinancePill>
            <span className="text-[0.62rem] tabular-nums text-muted-foreground/70">{copy.asOf(fmtTs(health.ts))}</span>
          </>
        )}

        <Button onClick={onRefresh} size="xs" variant="ghost">
          <RefreshCw className="size-3" />
          {t.common.refresh}
        </Button>
      </div>
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
