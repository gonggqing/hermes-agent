// Hand-built master-detail shell for the Finance tab (no shared web
// primitive exists — precedent: ChannelsPage's Tailwind grid). A grouped
// left sidebar + right detail + a bottom paper/live mode switcher, reused by
// the Research, Queue and Portfolio views. Responsive: the two-column grid
// collapses to a stack on narrow screens.

import type { ReactNode } from "react";
import { Segmented } from "@nous-research/ui/ui/components/segmented";
import { cn } from "@/lib/utils";
import type { FinanceMode } from "@/lib/api";
import { useFinanceT } from "./i18n";

export function MasterDetail({
  sidebar,
  footer,
  children,
}: {
  sidebar: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-4 lg:grid-cols-[260px_minmax(0,1fr)] lg:items-start">
      <aside className="flex flex-col gap-3 lg:sticky lg:top-4">
        <div className="flex flex-col gap-3">{sidebar}</div>
        {footer}
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
 * The idiomatic finance paper/live toggle, pinned to the bottom of each
 * master-detail view. `mode` is the effective mode (override ?? service);
 * picking a value overrides the service default, which the hint line names.
 */
export function ModeSwitcher({
  mode,
  serviceMode,
  onChange,
}: {
  mode: FinanceMode;
  serviceMode: FinanceMode | null;
  onChange: (mode: FinanceMode) => void;
}) {
  const ft = useFinanceT();
  const modeLabel = (m: FinanceMode) =>
    m === "live" ? ft.layout.modeLive : ft.layout.modePaper;
  return (
    <div className="flex flex-col gap-1.5 border border-border p-2">
      <span className="px-1 text-xs uppercase tracking-wider text-text-tertiary">
        {ft.layout.modeLabel}
      </span>
      <Segmented<FinanceMode>
        size="sm"
        value={mode}
        onChange={onChange}
        options={[
          { value: "paper", label: ft.layout.modePaper },
          { value: "live", label: ft.layout.modeLive },
        ]}
      />
      {serviceMode && (
        <span className="px-1 font-mondwest normal-case text-xs text-text-tertiary">
          {ft.layout.modeFollowsService.replace("{mode}", modeLabel(serviceMode))}
        </span>
      )}
    </div>
  );
}
