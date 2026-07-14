import type { StatusTone } from '@/components/status-dot'
import type { FinanceBreakerState, FinanceCandidateStatus } from '@/hermes'
import { fmtDateTime } from '@/lib/time'

// Human actor recorded in the confirmation-service audit trail.
// TODO(finance): wire real identity (gateway auth subject) once available.
export const FINANCE_ACTOR = 'hermes-user'

// Root react-query key for every finance query, so one invalidation (the `r`
// refresh hotkey, a landed mutation) sweeps the whole portal.
export const FINANCE_KEY = ['finance'] as const

export const financeKey = (...parts: ReadonlyArray<string | undefined>) =>
  [...FINANCE_KEY, ...parts.filter((part): part is string => part !== undefined)] as const

// ── Idempotency keys (Loop.md §5.6) ─────────────────────────────────────────
// One UUID per user INTENT — keyed by candidate + action + exact edit payload —
// created on first use and reused on retry so the service replays instead of
// double-applying. Dropped only after the service answers definitively
// (applied/replayed or a terminal 4xx); kept across network failures.
const idempotencyKeys = new Map<string, string>()

// `edits` is any structured edit payload (candidate qty/px edits, or a real-
// portfolio draft edit map) — only its JSON shape distinguishes one intent from
// another, so `object` keeps both callers type-safe without a shared edit type.
const intentKey = (candidateId: string, action: string, edits?: object): string =>
  `${candidateId}:${action}:${edits ? JSON.stringify(edits) : ''}`

/**
 * A random id that also works on NON-secure origins. `crypto.randomUUID` only
 * exists in a secure context (HTTPS/localhost); over plain http://<host> it is
 * undefined and throws, breaking every draft/approval action. Fall back to
 * getRandomValues (a v4 UUID) then to time+random (uniqueness, not CSPRNG).
 */
export function randomId(): string {
  if (typeof crypto !== 'undefined') {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
    if (typeof crypto.getRandomValues === 'function') {
      const b = crypto.getRandomValues(new Uint8Array(16))
      b[6] = (b[6] & 0x0f) | 0x40
      b[8] = (b[8] & 0x3f) | 0x80
      const h = Array.from(b, x => x.toString(16).padStart(2, '0')).join('')
      return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`
    }
  }
  return `id-${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`
}

export function idempotencyKeyFor(candidateId: string, action: string, edits?: object): string {
  const key = intentKey(candidateId, action, edits)
  let value = idempotencyKeys.get(key)

  if (!value) {
    value = randomId()
    idempotencyKeys.set(key, value)
  }

  return value
}

export function settleIdempotencyKey(candidateId: string, action: string, edits?: object): void {
  idempotencyKeys.delete(intentKey(candidateId, action, edits))
}

// ── Error parsing ────────────────────────────────────────────────────────────
// The desktop IPC bridge rejects non-2xx REST calls with `Error("<status>:
// <body>")` (electron/main.ts fetchJson). The finance proxy answers 503 with
// `{"error": "finance service offline", "hint": ...}` when the swing-trader
// process is down; FastAPI errors carry `{"detail": ...}`; confirmation-action
// errors carry `{ok, code, message, ...}`.
export interface FinanceApiError {
  status: null | number
  code: null | string
  message: string
  offline: boolean
}

export function parseFinanceError(err: unknown): FinanceApiError {
  const raw = err instanceof Error ? err.message : String(err)
  const match = /^(\d{3}):\s*([\s\S]*)$/.exec(raw)
  const status = match ? Number(match[1]) : null
  const body = match ? match[2] : raw
  let code: null | string = null
  let message = body.trim() || raw
  let offlineBody = false

  try {
    const parsed: unknown = JSON.parse(body)

    if (parsed && typeof parsed === 'object') {
      const record = parsed as Record<string, unknown>
      offlineBody = record.error === 'finance service offline'

      if (typeof record.code === 'string') {
        code = record.code
      }

      const text = [record.message, record.detail, record.error].find(
        (value): value is string => typeof value === 'string' && value.length > 0
      )

      if (text) {
        message = text
      }
    }
  } catch {
    // Non-JSON body (HTML gateway error page, plain text) — keep the raw text.
  }

  return {
    status,
    code,
    message,
    // 502/503 from the proxy (or its explicit offline body) mean the finance
    // service isn't running — an expected state, rendered as the offline panel.
    offline: offlineBody || status === 502 || status === 503
  }
}

// ── Formatting ───────────────────────────────────────────────────────────────

const moneyFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })
const priceFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 })
const qtyFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 4 })

export const fmtMoney = (value: null | number | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '—' : moneyFmt.format(value)

export const fmtSignedMoney = (value: null | number | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : `${value > 0 ? '+' : ''}${moneyFmt.format(value)}`

export const fmtPrice = (value: null | number | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '—' : priceFmt.format(value)

export const fmtQty = (value: null | number | undefined): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '—' : qtyFmt.format(value)

// ── Watch-module price presentation (currency + unit) ────────────────────────
// Each watch symbol carries a currency symbol and a localized unit key; the
// number is rendered with both. `null` currency + `pct` unit renders a yield
// ("4.30 %"); a currency with a `null` unit renders a currency-prefixed number
// ("$67,000"); a currency with a unit renders "value currency / unit"
// ("4,079 $ / 盎司"). The `unitWord` is the localized unit label (copy.units).
export type WatchCurrency = '$' | '¥'
export type WatchUnitKey = 'bbl' | 'g' | 'oz' | 'pct' | 'share'

// Yields want ~2 fixed decimals ("4.30 %"); the shared priceFmt keeps thousands
// separators for large values (BTC → "67,000").
const yieldFmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })

// Full header rendering of a symbol's last price with its currency + unit.
export function fmtWatchPrice(
  value: null | number | undefined,
  currency: WatchCurrency | null,
  unit: WatchUnitKey | null,
  unitWord: null | string
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }

  if (unit === 'pct') {
    return `${yieldFmt.format(value)} ${unitWord ?? '%'}`
  }

  if (currency && unit) {
    return `${priceFmt.format(value)} ${currency} / ${unitWord ?? ''}`.trimEnd()
  }

  if (currency) {
    return `${currency}${priceFmt.format(value)}`
  }

  return priceFmt.format(value)
}

// Compact currency/unit rendering for tight surfaces (chart O/H/L/C tooltip,
// bid/ask): a currency prefix (or the "%" suffix for yields) with no trailing
// "/ unit" so the small 2×2 grid stays legible — the full unit lives in the
// card header.
export function fmtWatchValue(
  value: null | number | undefined,
  currency: WatchCurrency | null,
  unit: WatchUnitKey | null,
  unitWord: null | string
): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }

  if (unit === 'pct') {
    return `${yieldFmt.format(value)} ${unitWord ?? '%'}`
  }

  if (currency) {
    return `${currency}${priceFmt.format(value)}`
  }

  return priceFmt.format(value)
}

export const fmtPct = (value: null | number | undefined, digits = 1): string =>
  value === null || value === undefined || !Number.isFinite(value) ? '—' : `${value.toFixed(digits)}%`

export const fmtSignedPct = (value: null | number | undefined, digits = 1): string =>
  value === null || value === undefined || !Number.isFinite(value)
    ? '—'
    : `${value > 0 ? '+' : ''}${value.toFixed(digits)}%`

export function fmtTs(iso?: null | string): string {
  if (!iso) {
    return '—'
  }

  const date = new Date(iso)

  return Number.isNaN(date.valueOf()) ? iso : fmtDateTime.format(date)
}

// Signed-value tone for PnL-ish numbers.
export const pnlClass = (value: null | number | undefined): string =>
  value === null || value === undefined || value === 0 || !Number.isFinite(value)
    ? 'text-muted-foreground'
    : value > 0
      ? 'text-primary'
      : 'text-destructive'

// ── Tones ────────────────────────────────────────────────────────────────────

export const BREAKER_TONE: Record<FinanceBreakerState, StatusTone> = {
  NORMAL: 'good',
  TRIPPED: 'bad',
  UNKNOWN: 'muted'
}

export const CANDIDATE_STATUS_TONE: Record<FinanceCandidateStatus, StatusTone> = {
  approved: 'good',
  edited: 'good',
  expired: 'muted',
  placed: 'good',
  proposed: 'muted',
  pushed: 'warn',
  rejected: 'bad',
  risk_approved: 'good',
  risk_vetoed: 'bad'
}

export const REGIME_TONE: Record<string, StatusTone> = {
  neutral: 'warn',
  risk_off: 'bad',
  risk_on: 'good'
}

export const statusLabel = (value: string): string => value.replace(/_/g, ' ')

// Localized label for a backend enum value (side, order type, candidate status,
// …). Looks the raw wire value up in the locale map and falls back to a
// humanized form so an unseen backend code still renders legibly instead of
// blank. The map keys are the exact wire values (Loop.md schemas).
export const enumLabel = (map: Record<string, string>, value: string): string => map[value] ?? statusLabel(value)
