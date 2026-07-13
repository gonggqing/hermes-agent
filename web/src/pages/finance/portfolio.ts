// Pure helpers for the Phase 0.9 Portfolio sub-area (real multi-account
// US/HK/CN holdings). Kept JSX-free + framework-free so the parse/validation
// logic is unit-testable and the .tsx component stays a thin view layer.
// READ + DRAFT only — the only writes are creating a draft + the human
// confirm/edit/reject action (Loop.md §3).

import type {
  FinancePortfolioAccountType,
  FinancePortfolioDraftCreate,
  FinancePortfolioDraftEdits,
  FinancePortfolioDraftStatus,
  FinancePortfolioMarket,
  FinancePortfolioProvider,
} from "@/lib/api";
import type { FinanceTranslations } from "@/i18n/types";
import { type BadgeTone, fmtMoney } from "./format";

// ── Enum option lists (drive the <Select> + segmented controls) ──────────

export const MARKET_OPTIONS: FinancePortfolioMarket[] = ["US", "HK", "CN"];
export const PROVIDER_OPTIONS: FinancePortfolioProvider[] = ["manual", "ibkr"];
export const ACCOUNT_TYPE_OPTIONS: FinancePortfolioAccountType[] = [
  "cash",
  "margin",
];
/** Event types offered in the Record-trade form. Buy/sell are the common
 * cases; the rest cover corporate actions + cash movements. Unknown backend
 * enums still render via {@link eventTypeLabel}'s raw-value fallback. */
export const EVENT_TYPE_OPTIONS = [
  "buy",
  "sell",
  "opening",
  "dividend",
  "deposit",
  "withdraw",
  "fee",
  "split",
] as const;
export const DRAFT_STATUS_OPTIONS: FinancePortfolioDraftStatus[] = [
  "draft",
  "confirmed",
  "rejected",
  "expired",
];

// ── Enum label resolvers (localized; raw-value fallback for unknowns) ────

function labelFrom(
  map: Record<string, string>,
  key: string | null | undefined,
): string {
  if (!key) return "—";
  return map[key] ?? key;
}

export function marketLabel(m: string | null | undefined, ft: FinanceTranslations) {
  return labelFrom(ft.portfolio.markets as Record<string, string>, m);
}
export function providerLabel(p: string | null | undefined, ft: FinanceTranslations) {
  return labelFrom(ft.portfolio.providers as Record<string, string>, p);
}
export function accountTypeLabel(
  a: string | null | undefined,
  ft: FinanceTranslations,
) {
  return labelFrom(ft.portfolio.accountTypes as Record<string, string>, a);
}
export function securityTypeLabel(
  s: string | null | undefined,
  ft: FinanceTranslations,
) {
  return labelFrom(ft.portfolio.securityTypes as Record<string, string>, s);
}
export function eventTypeLabel(
  e: string | null | undefined,
  ft: FinanceTranslations,
) {
  return labelFrom(ft.portfolio.eventTypes as Record<string, string>, e);
}
export function draftStatusLabel(
  s: string | null | undefined,
  ft: FinanceTranslations,
) {
  return labelFrom(ft.portfolio.draftStatus as Record<string, string>, s);
}
export function authorityLabel(
  a: string | null | undefined,
  ft: FinanceTranslations,
) {
  return labelFrom(ft.portfolio.authority as Record<string, string>, a);
}

// ── Tone helpers ─────────────────────────────────────────────────────────

export function draftStatusTone(
  status: FinancePortfolioDraftStatus | string,
): BadgeTone {
  switch (status) {
    case "confirmed":
      return "success";
    case "rejected":
      return "destructive";
    case "expired":
      return "secondary";
    case "draft":
    default:
      return "outline";
  }
}

// ── Value formatting (never fabricate an unknown cost — Loop.md §3) ──────

/** Avg cost, or a localized "unknown" when the basis is not known / null.
 * Never renders a fabricated 0. */
export function fmtCostBasis(
  avgCost: number | null | undefined,
  known: boolean,
  ft: FinanceTranslations,
): string {
  if (!known || avgCost === null || avgCost === undefined || Number.isNaN(avgCost)) {
    return ft.portfolio.holdings.unknownCost;
  }
  return fmtMoney(avgCost);
}

/** Cash balance, or a localized "unknown" when the amount is not known. */
export function fmtCashAmount(
  amount: number | null | undefined,
  known: boolean,
  ft: FinanceTranslations,
): string {
  if (!known || amount === null || amount === undefined || Number.isNaN(amount)) {
    return ft.portfolio.holdings.unknownCash;
  }
  return fmtMoney(amount);
}

// ── Record-trade form parsing ────────────────────────────────────────────

export interface SelectedInstrument {
  symbol: string;
  market: string;
  currency: string;
}

export interface TradeFormDraft {
  eventType: string;
  qty: string;
  price: string;
  commission: string;
  occurredAt: string; // <input type="datetime-local"> value, or ""
  note: string;
}

export const EMPTY_TRADE_FORM: TradeFormDraft = {
  eventType: "buy",
  qty: "",
  price: "",
  commission: "",
  occurredAt: "",
  note: "",
};

/** The draft body the caller fills in with account_id/created_by/surface. */
export type TradeDraftPayload = Omit<
  FinancePortfolioDraftCreate,
  "account_id" | "created_by" | "surface"
>;

/**
 * Validate the Record-trade form. Returns a draft-create payload, or a
 * localized error string on bad input. A blank price is intentional
 * (unknown cost) — it is omitted, never coerced to 0.
 */
export function parseTradeForm(
  form: TradeFormDraft,
  instrument: SelectedInstrument | null,
  ft: FinanceTranslations,
): TradeDraftPayload | string {
  const r = ft.portfolio.record;
  if (!instrument) return r.errInstrument;

  const qtyRaw = form.qty.trim();
  const qty = Number(qtyRaw);
  if (qtyRaw === "" || !Number.isFinite(qty) || qty <= 0) return r.errQty;

  const payload: TradeDraftPayload = {
    event_type: form.eventType,
    symbol: instrument.symbol,
    market: instrument.market,
    currency: instrument.currency,
    qty,
  };

  const priceRaw = form.price.trim();
  if (priceRaw !== "") {
    const price = Number(priceRaw);
    if (!Number.isFinite(price) || price <= 0) return r.errPrice;
    payload.price = price;
  }

  const commRaw = form.commission.trim();
  if (commRaw !== "") {
    const commission = Number(commRaw);
    if (!Number.isFinite(commission) || commission < 0) return r.errCommission;
    payload.commission = commission;
  }

  const occurred = form.occurredAt.trim();
  if (occurred !== "") {
    const d = new Date(occurred);
    if (!Number.isNaN(d.getTime())) payload.occurred_at = d.toISOString();
  }

  const note = form.note.trim();
  if (note !== "") payload.note = note;

  return payload;
}

// ── Draft-edit form parsing (the human-confirmation edit surface) ────────

export interface DraftEditForm {
  qty: string;
  price: string;
  commission: string;
  note: string;
}

export function draftEditFrom(draft: {
  qty: number | null;
  price: number | null;
  commission: number | null;
  note: string;
}): DraftEditForm {
  return {
    qty: draft.qty === null ? "" : String(draft.qty),
    price: draft.price === null ? "" : String(draft.price),
    commission: draft.commission === null ? "" : String(draft.commission),
    note: draft.note ?? "",
  };
}

/**
 * Validate the draft-edit form into an edits payload. Only touched fields
 * are sent; a blank price/commission is omitted (unknown), never coerced.
 */
export function parseDraftEdits(
  form: DraftEditForm,
  ft: FinanceTranslations,
): FinancePortfolioDraftEdits | string {
  const d = ft.portfolio.draftsView;
  const edits: FinancePortfolioDraftEdits = {};

  const qtyRaw = form.qty.trim();
  if (qtyRaw !== "") {
    const qty = Number(qtyRaw);
    if (!Number.isFinite(qty) || qty <= 0) return d.errQty;
    edits.qty = qty;
  }

  const priceRaw = form.price.trim();
  if (priceRaw !== "") {
    const price = Number(priceRaw);
    if (!Number.isFinite(price) || price <= 0) return d.errPrice;
    edits.price = price;
  }

  const commRaw = form.commission.trim();
  if (commRaw !== "") {
    const commission = Number(commRaw);
    if (!Number.isFinite(commission) || commission < 0) return d.errCommission;
    edits.commission = commission;
  }

  const note = form.note.trim();
  if (note !== "") edits.note = note;

  return edits;
}
