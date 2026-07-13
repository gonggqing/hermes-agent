// Hand-built master-detail shell for the Finance tab (no shared web
// primitive exists — precedent: ChannelsPage's Tailwind grid). A grouped
// left sidebar + right detail, reused by the Research, Queue and Portfolio
// views, plus the page-level bottom status/utility bar (service health +
// breaker + last-updated + refresh + the paper/live toggle). Responsive: the
// two-column grid collapses to a stack on narrow screens.

import type { ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "@nous-research/ui/ui/components/button";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { cn } from "@/lib/utils";
import type { FinanceHealth, FinanceMode } from "@/lib/api";
import { useI18n } from "@/i18n";
import { useFinanceT } from "./i18n";

export function MasterDetail({
  sidebar,
  children,
}: {
  sidebar: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start">
      <aside className="flex flex-col gap-3 lg:sticky lg:top-4">
        <div className="flex flex-col gap-3">{sidebar}</div>
      </aside>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

/** A labeled group of sidebar rows. */
export function SidebarGroup({
  label,
  children,
}: {
  label: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <div className="px-1 text-xs uppercase tracking-wider text-text-tertiary">
        {label}
      </div>
      <div className="flex flex-col border border-border">{children}</div>
    </div>
  );
}

/** One selectable sidebar row. Disabled rows are non-interactive placeholders. */
export function SidebarButton({
  active,
  disabled,
  onClick,
  trailing,
  subtitle,
  children,
}: {
  active: boolean;
  disabled?: boolean;
  onClick?: () => void;
  trailing?: ReactNode;
  subtitle?: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={active}
      aria-current={active ? "true" : undefined}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-2 border-b border-border/50 px-3 py-2 text-left font-mondwest normal-case text-sm transition-colors last:border-b-0",
        active
          ? "bg-primary text-primary-foreground"
          : "text-foreground hover:bg-secondary/30",
        disabled && "cursor-not-allowed opacity-40 hover:bg-transparent",
      )}
    >
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate">{children}</span>
        {subtitle && (
          <span
            className={cn(
              "truncate text-xs",
              active ? "text-primary-foreground/80" : "text-text-tertiary",
            )}
          >
            {subtitle}
          </span>
        )}
      </span>
      {trailing}
    </button>
  );
}

/**
 * The idiomatic finance paper/live toggle — a SINGLE control (not two tabs)
 * that reads out the current mode and flips to the other on click. `mode` is
 * the effective mode (override ?? service); clicking sets an explicit override,
 * so the initial `null`-follows-service semantics are preserved until the user
 * touches it. Filter-only: never re-modes an action (Loop.md §3).
 */
export function ModeToggle({
  mode,
  serviceMode,
  onToggle,
}: {
  mode: FinanceMode;
  serviceMode: FinanceMode | null;
  onToggle: () => void;
}) {
  const ft = useFinanceT();
  const isLive = mode === "live";
  const modeLabel = (m: FinanceMode) =>
    m === "live" ? ft.layout.modeLive : ft.layout.modePaper;
  return (
    <button
      type="button"
      onClick={onToggle}
      aria-label={ft.page.toggleMode}
      title={
        serviceMode
          ? ft.layout.modeFollowsService.replace("{mode}", modeLabel(serviceMode))
          : undefined
      }
      className={cn(
        "inline-flex items-center gap-1.5 border px-2.5 py-1 font-mondwest text-display text-xs uppercase tracking-wider outline-none transition-colors focus-visible:ring-1 focus-visible:ring-primary/60",
        isLive
          ? "border-destructive bg-destructive/10 text-destructive hover:bg-destructive/20"
          : "border-border bg-secondary/30 text-foreground hover:bg-secondary/50",
      )}
    >
      <span
        className={cn(
          "h-1.5 w-1.5 rounded-full",
          isLive ? "bg-destructive" : "bg-muted-foreground/60",
        )}
      />
      {isLive ? ft.page.modeLive : ft.page.modePaper}
    </button>
  );
}

/**
 * Bottom status/utility bar for the Finance view — the service online/offline
 * dot, the loop-attached indicator, the circuit-breaker state, the last-updated
 * time, a manual refresh button, and the paper/live toggle. Pinned to the
 * bottom, out of the top page header.
 */
export function FinanceBottomBar({
  health,
  offline,
  lastUpdated,
  loading,
  onRefresh,
  mode,
  serviceMode,
  onToggleMode,
}: {
  health: FinanceHealth | null;
  offline: boolean;
  lastUpdated: Date | null;
  loading: boolean;
  onRefresh: () => void;
  mode: FinanceMode;
  serviceMode: FinanceMode | null;
  onToggleMode: () => void;
}) {
  const ft = useFinanceT();
  const { t } = useI18n();
  const breaker = health?.breaker ?? "UNKNOWN";
  const tripped = breaker === "TRIPPED";
  const breakerText =
    breaker === "TRIPPED"
      ? ft.page.breakerTripped
      : breaker === "NORMAL"
        ? ft.page.breakerNormal
        : "—";

  return (
    <div className="sticky bottom-0 z-10 flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-border bg-background/95 px-3 py-2 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 font-mondwest normal-case text-xs text-muted-foreground">
        {/* Service online / offline. */}
        <span className="flex items-center gap-1.5">
          <span
            className={cn(
              "h-2 w-2 rounded-full",
              offline ? "bg-destructive" : "bg-success",
            )}
          />
          {offline ? ft.page.offline : ft.page.online}
        </span>

        {/* Loop-attached indicator. */}
        {!offline && health && (
          <span className="flex items-center gap-1.5">
            <span
              className={cn(
                "h-2 w-2 rounded-full",
                health.loop_attached ? "bg-success" : "bg-muted-foreground/40",
              )}
            />
            {health.loop_attached ? ft.page.loopAttached : ft.page.loopIdle}
          </span>
        )}

        {/* Circuit-breaker state. */}
        {!offline && (
          <span className="flex items-center gap-1.5">
            <span className="text-text-tertiary">{ft.page.breakerLabel}</span>
            <span className={tripped ? "text-destructive" : "text-foreground"}>
              {breakerText}
            </span>
          </span>
        )}

        {/* Last-updated time. */}
        {lastUpdated && !offline && (
          <span className="text-text-tertiary">
            {ft.page.updatedAt.replace(
              "{time}",
              lastUpdated.toLocaleTimeString(),
            )}
          </span>
        )}
      </div>

      <div className="ml-auto flex items-center gap-2">
        <Button
          type="button"
          ghost
          size="icon"
          className="text-muted-foreground hover:text-foreground"
          onClick={onRefresh}
          disabled={loading}
          aria-label={t.common.refresh}
        >
          {loading ? <Spinner /> : <RefreshCw className="h-4 w-4" />}
        </Button>
        <ModeToggle
          mode={mode}
          serviceMode={serviceMode}
          onToggle={onToggleMode}
        />
      </div>
    </div>
  );
}
