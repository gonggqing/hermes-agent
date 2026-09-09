// ---------------------------------------------------------------------------
// Finance service (Loop.md §5.6/§5.9). The Hermes backend reverse-proxies
// /api/finance/* to the swing-trader service's /v1/* (hermes_cli/
// finance_proxy.py), so these calls inherit dashboard auth like every other
// /api path. The service runs as its own process and is routinely OFFLINE
// (evenings, weekends, not started): the proxy then answers 503 with a JSON
// hint. Callers must treat that as an expected offline state, never a crash.
//
// The wire shapes below mirror trader/swing_trader (schemas.py, reporter.py,
// ledger.py, monitors.py, watchlist.py, api.py). They live here rather than in
// types/hermes.ts because they are owned by the finance service's versioned
// API, not by the Hermes gateway.
// ---------------------------------------------------------------------------

export type FinanceMode = 'live' | 'paper'

export interface FinancePortfolioControlsUpdate {
  invested_target_pct: number
  invested_tolerance_pct: number
  agent_budget_pct: number
  agent_budget_tolerance_pct: number
  max_position_pct: number
  per_trade_risk_pct: number
  max_new_positions_per_day: number
  base_currency: string
}

export interface FinancePortfolioControls extends FinancePortfolioControlsUpdate {
  invested_ceiling_pct: number
  agent_ceiling_pct: number
  cash_reserve_floor_pct: number
  updated_at: string
}

export type FinanceBreakerState = 'NORMAL' | 'TRIPPED' | 'UNKNOWN'

export type FinanceCandidateStatus =
  | 'approved'
  | 'edited'
  | 'expired'
  | 'placed'
  | 'proposed'
  | 'pushed'
  | 'rejected'
  | 'risk_approved'
  | 'risk_vetoed'

export type FinanceOrderType = 'BRACKET' | 'LMT' | 'LOC' | 'MOC' | 'STP'

export type FinanceSide = 'BUY' | 'SELL'

export interface FinanceHealth {
  status: string
  mode: FinanceMode
  loop_attached: boolean
  breaker: FinanceBreakerState
  ts: string
}

export interface FinancePosition {
  symbol: string
  currency: string
  qty: number
  avg_px: number
  mkt_px: null | number
  upnl: null | number
  pool: string
}

export interface FinanceOpenOrder {
  symbol: string
  currency: string
  side: FinanceSide
  qty: number
  order_type: FinanceOrderType
  limit: null | number
  stop: null | number
  status: string
}

export interface FinanceStats {
  n_closed: number
  n_wins: number
  win_rate: number
  avg_win: null | number
  avg_loss: null | number
  payoff_ratio: null | number
  expectancy: number
  total_pnl: number
  avg_hold_days: null | number
  max_drawdown_pct: number
}

export interface FinanceSnapshot {
  ts: string
  mode: FinanceMode
  equity: number
  cash: number
  upnl: number
  day_pnl: number
  drawdown_pct: number
  breaker_state: FinanceBreakerState
  base_currency: string
  cash_by_currency: Record<string, number>
  equity_by_currency: Record<string, number>
  fx_to_base: Record<string, number>
}

// Loop attached: the full live AccountView. Loop idle (or the non-active
// mode): a ledger fallback of last snapshot + stats. Discriminate on `source`.
export interface FinanceAccountLive {
  mode: FinanceMode
  ts: string
  equity: number
  cash: number
  upnl: number
  day_pnl: number
  drawdown_pct: number
  breaker_state: FinanceBreakerState
  base_currency: string
  cash_by_currency: Record<string, number>
  equity_by_currency: Record<string, number>
  fx_to_base: Record<string, number>
  positions: FinancePosition[]
  open_orders: FinanceOpenOrder[]
  stats: FinanceStats
  source?: undefined
}

export interface FinanceAccountLedger {
  mode: FinanceMode
  snapshot: FinanceSnapshot | null
  stats: FinanceStats
  source: 'ledger'
}

export type FinanceAccount = FinanceAccountLedger | FinanceAccountLive

export interface FinanceOrderRow {
  id: string
  ts: string
  mode: FinanceMode
  symbol: string
  currency: string
  side: FinanceSide
  qty: number
  order_type: FinanceOrderType
  limit: null | number
  stop: null | number
  tp: null | number
  tif: string
  status: string
  broker_ref: null | string
  filled_qty: number
  avg_fill_px: null | number
}

export interface FinanceFill {
  id: string
  order_id: string
  symbol: string
  currency: string
  side: FinanceSide
  qty: number
  px: number
  commission: number
  mode: FinanceMode
  ts: string
}

export interface FinanceTrade {
  id: string
  mode: FinanceMode
  symbol: string
  qty: number
  entry_px: number
  exit_px: null | number
  pnl: null | number
  r_multiple: null | number
  hold_days: null | number
  rationale: string
  is_open: boolean
  ts: string
  exit_ts: null | string
}

export interface FinanceMarketSnapshot {
  // `{"status": "no snapshot yet"}` before the loop's first market poll.
  status?: string
  ts?: string
  source?: 'research_brief' | 'runtime'
  indices?: Record<string, Record<string, null | number>>
  vix?: null | number
  breadth_pct_above_50dma?: number
  risk_on_off?: 'neutral' | 'risk_off' | 'risk_on'
}

export interface FinanceWatchlistItem {
  symbol: string
  theme: string
  ai_phase: string
  role: string
  enabled: boolean
}

export interface FinanceResearchWatchlistMember {
  symbol: string
  display_name: string
  market: null | string
  exchange: null | string
  currency: null | string
  security_type: null | string
  position: number
  created_at: string
}

export interface FinanceResearchWatchlist {
  id: string
  name: string
  position: number
  members: FinanceResearchWatchlistMember[]
  created_at: string
  updated_at: string
}

export interface FinanceResearchWatchlistRecommendation {
  symbol: string
  display_name: string
  market: null | string
  exchange: string
  currency: null | string
  security_type: 'etf' | 'stock'
}

export interface FinanceCandidate {
  id: string
  ts: string
  symbol: string
  side: FinanceSide
  qty: number
  order_type: FinanceOrderType
  limit: null | number
  stop: null | number
  tp: null | number
  sl: null | number
  tif: string
  rationale: string
  confidence: number
  signal_ids: string[]
  ref_px: null | number
  valid_until: null | string
  status: FinanceCandidateStatus
  risk_note: string
  pool: string
}

export interface FinancePendingCandidate {
  candidate: FinanceCandidate
  version: number
  window_open: boolean
}

export interface FinanceAuditEvent {
  ts: string
  mode: string
  candidate_id: string
  action: string
  actor: string
  surface: string
  version: number
  idempotency_key: string
  prev_status: string
  new_status: string
  applied: boolean
  detail: string
}

// Human edits are limited to these five fields (confirmation.py re-validates
// through CandidateOrder, so protection can never be stripped server-side).
export interface FinanceCandidateEdits {
  qty?: number
  limit?: number
  stop?: number
  sl?: number
  tp?: number
}

export interface FinanceActionPayload {
  action: 'approve' | 'edit' | 'reject'
  actor: string
  // Generated ONCE per user intent (crypto.randomUUID()) and reused on retry,
  // so the service replays instead of double-applying (Loop.md §5.6).
  idempotency_key: string
  expected_version?: number
  edits?: FinanceCandidateEdits
  // The IPC bridge cannot set the X-Finance-Surface header, so the service
  // accepts the surface in the request body as a fallback (header wins).
  surface?: 'desktop' | 'web' | 'telegram'
}

export interface FinanceActionResult {
  ok: boolean
  code: 'applied' | 'replayed'
  message: string
  version: null | number
  candidate: FinanceCandidate | null
}

// ── Investment Research brief (Loop.md §7 Phase 0.5) ────────────────────────
// Wire shapes mirror trader/swing_trader/brief.py (ResearchBrief and its
// section models, serialized with model_dump(mode="json")). The endpoint
// always answers: a degraded brief carries freshness warnings and null
// regime/risk instead of failing.

export interface FinanceFreshness {
  market_as_of: null | string
  news_as_of: null | string
  portfolio_as_of: null | string
  market_age_minutes: null | number
  news_age_minutes: null | number
  portfolio_age_minutes: null | number
  market_stale: boolean
  news_stale: boolean
  portfolio_stale: boolean
  warnings: string[]
}

export interface FinanceRegimeView {
  risk_on_off: string
  vix: null | number
  breadth_pct_above_50dma: number
  indices: Record<string, Record<string, null | number>>
}

export interface FinanceRiskView {
  equity: number
  cash: number
  day_pnl: number
  drawdown_pct: number
  breaker_state: string
  pool_exposure_pct: Record<string, number>
  warnings: string[]
  // {n_closed, win_rate, expectancy, max_drawdown_pct} from Ledger.stats;
  // empty when the ledger stats accessor failed (see brief uncertainty).
  stats: Record<string, number>
}

export interface FinanceMover {
  symbol: string
  /** Human name (e.g. "三星电子"); "" when unknown — show the code only. */
  display_name?: string
  last: number
  dist_sma20_pct: number
  dist_sma50_pct: null | number
  theme: string
  ai_phase: string
  role: string
}

export interface FinanceThemeView {
  theme: string
  avg_dist_sma50_pct: number
  n_symbols: number
  leaders: string[]
}

export interface FinanceNewsDigestItem {
  headline: string
  source: string
  url: string
  sentiment: null | number
  symbol: null | string
  published_at?: null | string
  age_hours?: null | number
  source_quality?: string
}

export interface FinanceThesisAction {
  symbol: string
  display_name: string
  stance: 'buy_on_confirmation' | 'hold' | 'reduce_on_weakness' | 'exit_if_invalidated' | 'watch' | 'avoid'
  thesis_state: 'new' | 'strengthened' | 'unchanged' | 'weakened' | 'invalidated'
  confidence: number
  horizon_sessions: number
  what_changed: string
  rationale: string
  invalidation: string
  evidence_refs: string[]
}

export interface FinanceSignalView {
  symbol: string
  /** Human name (e.g. "三星电子"); "" when unknown — show the code only. */
  display_name?: string
  direction: string
  confidence: number
  source_agent: string
  thesis: string
  /** DATA as-of (ISO): last price-bar date the verdict rests on (§5.10),
   *  distinct from the brief's as_of. null for sentiment/macro (no price bar). */
  as_of_bar?: string | null
}

// Compact pending row in the brief (NOT the full FinancePendingCandidate the
// queue actions use — the brief is read-only by design, Loop.md §5.9).
export interface FinanceBriefPendingCandidate {
  symbol: string
  side: string
  qty: number
  confidence: number
  status: string
}

export interface FinanceProvenanceLink {
  label: string
  url: string
}

export interface FinanceResearchNarrative {
  generated_at: string
  market: string
  language: string
  model: string
  headline: string
  summary: string
  change_summary?: string[]
  action_views?: FinanceThesisAction[]
  sections: { title: string; analysis: string }[]
  watch_next: string[]
}

export interface FinanceDiscoveryEvidence {
  source: string
  url: string
  observed_at: string
  summary: string
}

export interface FinanceDiscoveryCandidate {
  symbol: string
  display_name: string
  theme: string
  component: string
  relationship: string
  score: number
  rank: number
  reasons: string[]
  evidence: FinanceDiscoveryEvidence[]
}

export interface FinanceDiscoveryPool {
  market: string
  as_of: string
  candidates: FinanceDiscoveryCandidate[]
  rejected: { symbol: string; reason: string }[]
  source_count: number
  status?: string
  notes?: string[]
}

export interface FinanceResearchSynthesis {
  status: string
  headline?: string
  analysis?: string[]
  watch_next?: string[]
  markets: Record<
    string,
    {
      market: string
      available: boolean
      freshness_status: string
      regime: string | null
      headline?: string | null
      summary?: string | null
    }
  >
  shared_themes: {
    theme: string
    cn_symbols: string[]
    hk_symbols: string[]
    evidence_urls: string[]
    cn_strength_pct?: number | null
    hk_strength_pct?: number | null
    relationship?: string
  }[]
  notes: string[]
}

export interface FinanceResearchBrief {
  as_of: string
  trading_date: string
  mode: FinanceMode
  freshness: FinanceFreshness
  regime: FinanceRegimeView | null
  risk: FinanceRiskView | null
  movers: { top: FinanceMover[]; bottom: FinanceMover[] }
  themes: FinanceThemeView[]
  events: { earnings: unknown[]; notes: string[] }
  news: {
    items: FinanceNewsDigestItem[]
    per_symbol_sentiment: Record<string, number>
    stale_items_excluded?: number
    future_items_excluded?: number
    duplicate_items_excluded?: number
  }
  signals_today: FinanceSignalView[]
  candidates_today: { counts: Record<string, number>; pending: FinanceBriefPendingCandidate[] }
  /** Optional for briefs archived before Phase 0.95 introduced discovery. */
  discovery?: FinanceDiscoveryPool | null
  /** Stable identity and primary-model outcome for a scheduled edition. */
  publication?: {
    edition_id: string
    edition: 'morning' | 'evening'
    scheduled_for: string
    evidence_as_of: string
    status: 'pending' | 'complete' | 'narrative_failed'
    failure: string
  } | null
  /** Model-written synthesis; absent on old archives or failed model runs. */
  narrative?: FinanceResearchNarrative | null
  cross_market_synthesis?: FinanceResearchSynthesis
  uncertainty: string[]
  provenance: FinanceProvenanceLink[]
}

// One hit from the source-linked research knowledge search (Loop.md §5.10:
// results always carry provenance; the endpoint fails closed with 503 when
// the vector index is down).
export interface FinanceKnowledgeHit {
  document_id: string
  title: string
  snippet: string
  source_url: string
  publisher: string
  score: number
  trading_date: string
}

// ── Watch-module market data (Loop.md §3: read-only) ────────────────────────
// The cross-asset watch modules (Gold/Oil/Rates/Crypto) read three read-only
// endpoints proxied at /api/finance/v1/{quote,bars,analyze}. These carry NO
// authority — there is no order or approval path here. Some tickers (GC=F,
// ^TNX, 518880.SS) return 404 from yfinance intermittently, so callers treat a
// per-symbol failure as an inline "no data" note and never crash the panel.

export interface FinanceQuote {
  symbol: string
  last: null | number
  bid: null | number
  ask: null | number
  volume: null | number
  as_of: null | string
  note?: string
}

export interface FinanceBar {
  ts: string
  open: number
  high: number
  low: number
  close: number
  volume: number
}

export interface FinanceBars {
  symbol: string
  timeframe: string
  bars: FinanceBar[]
  as_of: null | string
  note?: string
}

// The multi-agent verdict (direction + confidence) the analyze endpoint returns.
export interface FinanceAnalyzeVerdict {
  direction: string
  confidence: number
}

// Per-agent signal from the analyze fan-out. Rendered defensively (optional
// fields) so an unexpected server field never blanks the panel.
export interface FinanceAnalyzeSignal {
  symbol?: string
  direction: string
  confidence: number
  source_agent?: string
  thesis?: string
}

// One cited source (news item or research note). Field names vary between the
// two collections, so every field is optional and the UI falls back across them.
export interface FinanceAnalyzeCitation {
  title?: string
  headline?: string
  label?: string
  source?: string
  publisher?: string
  url?: string
  source_url?: string
  sentiment?: null | number
}

export interface FinanceAnalyze {
  symbol: string
  last: null | number
  verdict: FinanceAnalyzeVerdict | null
  signals: FinanceAnalyzeSignal[]
  news: FinanceAnalyzeCitation[]
  research: FinanceAnalyzeCitation[]
  as_of: null | string
  note?: string
}

function financeQuery(params: Record<string, boolean | number | string | undefined>): string {
  const query = new URLSearchParams()

  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') {
      query.set(key, String(value))
    }
  }

  const suffix = query.toString()

  return suffix ? `?${suffix}` : ''
}

export function getFinanceHealth(): Promise<FinanceHealth> {
  return window.hermesDesktop.api<FinanceHealth>({ path: '/api/finance/v1/health' })
}

export function getFinanceAccount(mode?: FinanceMode): Promise<FinanceAccount> {
  return window.hermesDesktop.api<FinanceAccount>({ path: `/api/finance/v1/account${financeQuery({ mode })}` })
}

export function getFinancePortfolioControls(): Promise<FinancePortfolioControls> {
  return window.hermesDesktop.api<FinancePortfolioControls>({ path: '/api/finance/v1/portfolio/controls' })
}

export function updateFinancePortfolioControls(
  body: FinancePortfolioControlsUpdate
): Promise<FinancePortfolioControls> {
  return window.hermesDesktop.api<FinancePortfolioControls>({
    path: '/api/finance/v1/portfolio/controls',
    method: 'PUT',
    body
  })
}

export function getFinanceOrders(opts: { activeOnly?: boolean; mode?: FinanceMode } = {}): Promise<FinanceOrderRow[]> {
  return window.hermesDesktop.api<FinanceOrderRow[]>({
    path: `/api/finance/v1/orders${financeQuery({ active_only: opts.activeOnly, mode: opts.mode })}`
  })
}

export function getFinanceFills(mode?: FinanceMode): Promise<FinanceFill[]> {
  return window.hermesDesktop.api<FinanceFill[]>({ path: `/api/finance/v1/fills${financeQuery({ mode })}` })
}

export function getFinanceTrades(opts: { mode?: FinanceMode; openOnly?: boolean } = {}): Promise<FinanceTrade[]> {
  return window.hermesDesktop.api<FinanceTrade[]>({
    path: `/api/finance/v1/trades${financeQuery({ mode: opts.mode, open_only: opts.openOnly })}`
  })
}

export function getFinanceStats(mode?: FinanceMode): Promise<FinanceStats> {
  return window.hermesDesktop.api<FinanceStats>({ path: `/api/finance/v1/stats${financeQuery({ mode })}` })
}

// Equity series for the account sparkline; newest-last, at most `limit` rows.
export function getFinanceSnapshots(opts: { limit?: number; mode?: FinanceMode } = {}): Promise<FinanceSnapshot[]> {
  return window.hermesDesktop.api<FinanceSnapshot[]>({
    path: `/api/finance/v1/snapshots${financeQuery({ limit: opts.limit, mode: opts.mode })}`
  })
}

export function getFinanceMarket(): Promise<FinanceMarketSnapshot> {
  return window.hermesDesktop.api<FinanceMarketSnapshot>({ path: '/api/finance/v1/market' })
}

export function getFinanceWatchlist(): Promise<FinanceWatchlistItem[]> {
  return window.hermesDesktop.api<FinanceWatchlistItem[]>({ path: '/api/finance/v1/watchlist' })
}

export function getResearchWatchlists(): Promise<FinanceResearchWatchlist[]> {
  return window.hermesDesktop.api<FinanceResearchWatchlist[]>({ path: '/api/finance/v1/research/watchlists' })
}

export function createResearchWatchlist(name: string): Promise<FinanceResearchWatchlist> {
  return window.hermesDesktop.api<FinanceResearchWatchlist>({
    path: '/api/finance/v1/research/watchlists',
    method: 'POST',
    body: { name }
  })
}

export function renameResearchWatchlist(id: string, name: string): Promise<FinanceResearchWatchlist> {
  return window.hermesDesktop.api<FinanceResearchWatchlist>({
    path: `/api/finance/v1/research/watchlists/${encodeURIComponent(id)}`,
    method: 'PATCH',
    body: { name }
  })
}

export function deleteResearchWatchlist(id: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    path: `/api/finance/v1/research/watchlists/${encodeURIComponent(id)}`,
    method: 'DELETE'
  })
}

export function addResearchWatchlistMember(
  id: string,
  member: Omit<FinanceResearchWatchlistMember, 'created_at' | 'position'>
): Promise<FinanceResearchWatchlist> {
  return window.hermesDesktop.api<FinanceResearchWatchlist>({
    path: `/api/finance/v1/research/watchlists/${encodeURIComponent(id)}/members`,
    method: 'POST',
    body: member
  })
}

export function removeResearchWatchlistMember(id: string, symbol: string): Promise<FinanceResearchWatchlist> {
  return window.hermesDesktop.api<FinanceResearchWatchlist>({
    path: `/api/finance/v1/research/watchlists/${encodeURIComponent(id)}/members/${encodeURIComponent(symbol)}`,
    method: 'DELETE'
  })
}

export function getResearchWatchlistRecommendations(): Promise<FinanceResearchWatchlistRecommendation[]> {
  return window.hermesDesktop.api<FinanceResearchWatchlistRecommendation[]>({
    path: '/api/finance/v1/research/watchlists/recommendations/holdings'
  })
}

// kind -> plain-text report (e.g. { morning: "..." }).
export function getFinanceReports(): Promise<Record<string, string>> {
  return window.hermesDesktop.api<Record<string, string>>({ path: '/api/finance/v1/reports/latest' })
}

export function getFinanceCandidates(
  opts: { mode?: FinanceMode; status?: FinanceCandidateStatus } = {}
): Promise<FinanceCandidate[]> {
  return window.hermesDesktop.api<FinanceCandidate[]>({
    path: `/api/finance/v1/candidates${financeQuery({ mode: opts.mode, status: opts.status })}`
  })
}

export function getFinancePendingCandidates(): Promise<FinancePendingCandidate[]> {
  return window.hermesDesktop.api<FinancePendingCandidate[]>({ path: '/api/finance/v1/candidates/pending' })
}

export function getFinanceAudit(opts: { candidateId?: string; mode?: FinanceMode } = {}): Promise<FinanceAuditEvent[]> {
  return window.hermesDesktop.api<FinanceAuditEvent[]>({
    path: `/api/finance/v1/audit${financeQuery({ candidate_id: opts.candidateId, mode: opts.mode })}`
  })
}

// Relay a HUMAN approve/reject/edit to the confirmation service (Loop.md §3:
// this is the only write surface; nothing here places orders). Non-2xx answers
// (403 window closed, 404 unknown, 409 terminal/version conflict, 422 invalid
// edit, 503 service not active) reject with `Error("<status>: <json body>")` —
// parse via parseFinanceError in app/finance/lib.
//
// The desktop IPC bridge (electron/main.ts fetchJson) forwards only
// method/body/timeout — custom headers are dropped — so the surface rides in
// the request body instead; the service treats body.surface as the fallback
// when the X-Finance-Surface header is absent, keeping the audit trail
// attributed to "desktop" (Loop.md §5.6).
export function postFinanceCandidateAction(id: string, payload: FinanceActionPayload): Promise<FinanceActionResult> {
  return window.hermesDesktop.api<FinanceActionResult>({
    path: `/api/finance/v1/candidates/${encodeURIComponent(id)}/action`,
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

// ── Manual session catch-up (Loop.md §5.6) ──────────────────────────────────
// Human controls to run/finalize a MISSED daily session (e.g. the 11:30 ET
// window nobody caught). Both are HUMAN-ONLY: the service answers 403 for a
// system surface or an LLM/system actor, and 503 when the trading loop is not
// attached. As with postFinanceCandidateAction, the IPC bridge drops the
// X-Finance-Surface header, so the surface rides in the request body (the
// service treats body.surface as the fallback).

export interface FinanceSessionRunResult {
  ran_at: string
  risk_approved: number
  pushed: number
  // Approval-window cutoff as "HH:MM" Eastern.
  cutoff_et: string
  // True when the dead-man's switch blocked NEW entries (stale data / drift);
  // exits still flow.
  entries_halted: boolean
  health_level: null | string
  actor: string
  surface: string
}

export interface FinanceSessionFinalizeResult {
  ran_at: string
  approved: number
  expired: number
  orders_now_active: number
  orders_added: number
  actor: string
  surface: string
}

// "Run session now": run the full monitor→decide→push pipeline NOW and push
// risk-approved candidates into a fresh approval window (cutoff_et). Does NOT
// place orders — the human still approves/rejects each candidate in the queue.
export function postFinanceSessionRun(payload: {
  actor: string
  windowMinutes?: number
}): Promise<FinanceSessionRunResult> {
  return window.hermesDesktop.api<FinanceSessionRunResult>({
    path: '/api/finance/v1/session/run',
    method: 'POST',
    body: {
      surface: 'desktop',
      actor: payload.actor,
      ...(payload.windowMinutes !== undefined ? { window_minutes: payload.windowMinutes } : {})
    }
  })
}

// "Finalize": place the human-APPROVED candidates and expire the rest.
export function postFinanceSessionFinalize(payload: { actor: string }): Promise<FinanceSessionFinalizeResult> {
  return window.hermesDesktop.api<FinanceSessionFinalizeResult>({
    path: '/api/finance/v1/session/finalize',
    method: 'POST',
    body: { surface: 'desktop', actor: payload.actor }
  })
}

// Research market accepted by both the persisted brief and manual refresh
// endpoints. Order authority is runtime-driven; a regional desk may expose a
// full paper loop while another remains research-only.
export type FinanceResearchMarket = 'us' | 'cn' | 'hk' | 'kr'

export interface FinancePredictionMetrics {
  directional_accuracy: null | number
  directional_hits: number
  directional_samples: number
  evaluated_checkpoints: number
  excess_accuracy: null | number
  excess_samples: number
  mean_brier: null | number
  mean_excess_return_pct: null | number
  mean_log_loss: null | number
  mean_return_pct: null | number
  sample_mature: boolean
}

export interface FinancePredictionForecast {
  as_of: string
  claim_type: string
  confidence: null | number
  direction: string
  entity_key: string
  entity_type: string
  display_name: string
  horizons: number[]
  invalidation: string
  market: string
  pending_checkpoints: number
  producer: string
  series_id: string
  status: string
  thesis: string
}

export interface FinancePredictionEvaluation {
  absolute_direction_hit: boolean | null
  claim_type: string
  confidence: null | number
  direction: string
  due_trading_date: null | string
  display_name: string
  entity_key: string
  entity_type: string
  evaluated_at: string
  evaluation_id: string
  evaluator_version: string
  excess_direction_hit: boolean | null
  excess_return_pct: null | number
  horizon_sessions: number
  mae_pct: null | number
  market: string
  mfe_pct: null | number
  producer: string
  return_pct: null | number
  series_id: string
  state: string
}

export interface FinancePredictionSummary {
  active_forecasts: FinancePredictionForecast[]
  by_horizon: Array<FinancePredictionMetrics & { key: string }>
  by_market: Array<FinancePredictionMetrics & { key: string }>
  by_producer: Array<FinancePredictionMetrics & { key: string }>
  filters: {
    due_as_of: string
    horizon_sessions: null | number
    market: null | string
    producer: null | string
  }
  generated_at: string
  overview: FinancePredictionMetrics & {
    active_series: number
    checkpoints: number
    due_checkpoints: number
    pending_checkpoints: number
    revisions: number
    series: number
  }
  recent_evaluations: FinancePredictionEvaluation[]
}

// Result of POST /v1/research/run (ResearchSession.run_now summary).
export interface FinanceRunResearchResult {
  market: string
  market_label: string
  ran_at: string
  signals: number
  sent: boolean
  brief_ready: boolean
}

// Manually RE-RUN a market's research session NOW (the "run research" button):
// refreshes that desk's brief with fresh data. Read-only (no orders) → ungated;
// 404s when that research market's session is disabled.
export function postFinanceResearchRun(market: FinanceResearchMarket): Promise<FinanceRunResearchResult> {
  return window.hermesDesktop.api<FinanceRunResearchResult>({
    path: `/api/finance/v1/research/run${financeQuery({ market })}`,
    method: 'POST'
  })
}

// The daily Investment Research brief (Loop.md §7 Phase 0.5). Always answers
// while the service is up: when the loop has not published a brief yet the
// service builds a DEGRADED one on demand (ledger-only, explicit freshness
// warnings, null regime/risk) instead of erroring. The CN brief is likewise
// DEGRADED (freshness warnings) before that session runs, never an error.
export function getFinanceResearchBrief(market?: FinanceResearchMarket): Promise<FinanceResearchBrief> {
  return window.hermesDesktop.api<FinanceResearchBrief>({
    path: `/api/finance/v1/research/brief${financeQuery({ market })}`
  })
}

export function getFinancePredictionSummary(market?: 'cn' | 'hk' | 'kr' | 'us'): Promise<FinancePredictionSummary> {
  return window.hermesDesktop.api<FinancePredictionSummary>({
    path: `/api/finance/v1/predictions/summary${financeQuery({ market })}`
  })
}

// Source-linked semantic research search. Answers 503 `{detail}` when no
// vector index is configured or the backend is down (fail-closed, Loop.md
// §5.10) — callers should render that as a calm "search offline" note.
export function searchFinanceKnowledge(q: string, k = 5): Promise<FinanceKnowledgeHit[]> {
  return window.hermesDesktop.api<FinanceKnowledgeHit[]>({
    path: `/api/finance/v1/knowledge/search${financeQuery({ k, q })}`
  })
}

// ── Watch-module read-only fetchers (Loop.md §3) ────────────────────────────
// One-shot spot quote for a symbol. Not mode-scoped — market data is global.
export function financeQuote(symbol: string): Promise<FinanceQuote> {
  return window.hermesDesktop.api<FinanceQuote>({ path: `/api/finance/v1/quote${financeQuery({ symbol })}` })
}

// Recent OHLCV bars for a compact price chart (default: 120 daily bars).
export function financeBars(symbol: string, opts: { limit?: number; timeframe?: string } = {}): Promise<FinanceBars> {
  return window.hermesDesktop.api<FinanceBars>({
    path: `/api/finance/v1/bars${financeQuery({ limit: opts.limit ?? 120, symbol, timeframe: opts.timeframe ?? '1d' })}`
  })
}

// The multi-agent read-only verdict for a symbol (direction + confidence, the
// per-agent signals, and the cited sources). Carries no order authority.
export function financeAnalyze(symbol: string): Promise<FinanceAnalyze> {
  return window.hermesDesktop.api<FinanceAnalyze>({ path: `/api/finance/v1/analyze${financeQuery({ symbol })}` })
}

// ---------------------------------------------------------------------------
// Real portfolio (Loop.md Phase 0.9). The user's REAL multi-account holdings
// (US/HK/CN), tracked separately from the paper-trading account above. This is
// a READ / DRAFT surface: the only "write" is creating a draft event and then
// running a HUMAN confirm/edit/reject action on it (no order placement). Manual
// entry = POST a draft (surface desktop, human created_by) then POST a confirm
// action (surface desktop, human actor) so the event lands as a holding.
//
// As with the confirmation service above, the desktop IPC bridge drops custom
// headers, so the human `surface` rides in the request body; the service treats
// body.surface as the fallback when X-Finance-Surface is absent. Confirm/import
// MUST carry a human actor (never "system"/"hermes") or the service answers 403.
// ---------------------------------------------------------------------------

export type FinancePortfolioMarket = 'CN' | 'HK' | 'US'

export type FinanceAccountType = 'cash' | 'margin'

export type FinanceAccountEnvironment = 'live' | 'paper'

// Terminal draft lifecycle (human-confirmation surface).
export type FinanceDraftStatus = 'confirmed' | 'draft' | 'expired' | 'rejected'

export interface FinancePortfolioAccount {
  id: string
  name: string
  provider: string
  market_scope: FinancePortfolioMarket
  account_type: FinanceAccountType
  environment: FinanceAccountEnvironment
  base_currency: string
  include_in_risk: boolean
  note: string
  created_at: string
  updated_at: string
}

// One derived holding. `avg_cost` is null and `cost_basis_known` false when the
// lot cost is not known (e.g. a position opened without a recorded price) — the
// UI shows a localized "unknown", NEVER a fabricated 0. In the aggregate view
// each holding also carries the account ids it is held across.
export interface FinanceHolding {
  symbol: string
  // The instrument name (e.g. "华夏全球科技先锋混合(QDII)A" for code "005698").
  // Null/absent when the instrument is unresolved — the UI falls back to the code.
  display_name?: null | string
  market: string
  currency: string
  qty: number
  avg_cost: null | number
  cost_basis_known: boolean
  accounts?: string[]
}

// A cash balance per currency. `amount` is null and `known` false when the
// balance cannot be derived (no opening cash recorded).
export interface FinanceCashBalance {
  currency: string
  amount: null | number
  known: boolean
}

export interface FinanceHoldingsResponse {
  account_id: string
  as_of: string
  n_events: number
  holdings: FinanceHolding[]
  cash: FinanceCashBalance[]
}

// Cross-account roll-up. `accounts` is the number of accounts rolled up.
export interface FinanceAggregateResponse {
  accounts: number
  as_of: string
  holdings: FinanceHolding[]
  cash: FinanceCashBalance[]
}

// ── Valuation (Phase 0.9 P&L) ────────────────────────────────────────────────
// The valuation endpoints layer live/imported/manual price marks over the
// derived holdings so the UI can show 现价/市值/盈亏. When the price OR the cost
// basis is unknown the money fields are null and the holding is "unpriced" — the
// UI shows a dash/未知, NEVER 0. `price_source` tells the user where the price
// came from (live feed, imported CSV, or a manual override).
export type FinancePriceSource = 'csv' | 'live' | 'manual' | 'none'

// One valued holding. `market_value` = qty·price, `cost` = qty·avg_cost,
// `unrealized_pnl` = market_value − cost — each null when its inputs are unknown.
// `accounts`/`account_names` are the accounts the holding is held across.
export interface FinanceValuationHolding {
  symbol: string
  // The instrument name (e.g. "华夏全球科技先锋混合(QDII)A" for code "005698").
  // Null/absent when the instrument is unresolved — the UI falls back to the code.
  display_name?: null | string
  market: FinancePortfolioMarket | null
  currency: string
  qty: number
  avg_cost: null | number
  cost_basis_known: boolean
  price: null | number
  price_as_of: null | string
  price_source: FinancePriceSource
  market_value: null | number
  cost: null | number
  unrealized_pnl: null | number
  pnl_pct: null | number
  accounts: string[]
  account_names: string[]
}

// One per-currency roll-up (for this user, all CNY). `market_value` includes
// cash; `holdings_value` is the priced holdings only. `n_priced`/`n_unpriced`
// let the UI warn that some 场外基金 may be unpriced.
export interface FinanceValuationTotal {
  currency: string
  market_value: number
  holdings_value: number
  cash: number
  cost: number
  unrealized_pnl: number
  pnl_pct: null | number
  n_priced: number
  n_unpriced: number
}

export interface FinanceValuationAccountRef {
  id: string
  name: string
}

// `accounts` is present on the aggregate valuation (the accounts rolled up) and
// absent on a single-account valuation.
export interface FinanceValuationResponse {
  as_of: string
  accounts?: FinanceValuationAccountRef[]
  totals: FinanceValuationTotal[]
  holdings: FinanceValuationHolding[]
}

export interface FinanceMarkPayload {
  symbol: string
  price: number
  currency?: string
  source?: 'csv' | 'live' | 'manual'
  actor: string
}

export interface FinanceMarkResult {
  symbol: string
  price: number
  currency: string
  as_of: string
  source: string
}

export interface FinanceMarksRefreshResult {
  refreshed: string[]
  failed: string[]
  skipped: string[]
}

export interface FinancePortfolioEvent {
  event_type: string
  symbol: null | string
  market: null | string
  currency: null | string
  qty: null | number
  price: null | number
  commission: null | number
  amount: null | number
  occurred_at: string
  source: string
  external_id: null | string
  note: null | string
}

export interface FinanceDrift {
  symbol: string
  portfolio_qty: number
  broker_qty: number
}

export interface FinanceReconcile {
  account_id: string
  ok: boolean
  authority: 'broker' | 'manual'
  summary: string
  note: string
  as_of: string
  drifts: FinanceDrift[]
}

export interface FinancePortfolioAuditEvent {
  ts: string
  action: string
  actor: string
  surface: string
  applied: boolean
  detail: string
}

export interface FinancePortfolioDraft {
  id: string
  account_id: string
  event_type: string
  symbol: null | string
  market: null | string
  currency: null | string
  qty: null | number
  price: null | number
  commission: null | number
  amount: null | number
  occurred_at: null | string
  source: string
  note: null | string
  status: FinanceDraftStatus
  version: number
  original_text: null | string
  missing: string[]
  ambiguities: string[]
  created_by: null | string
  confirmed_by: null | string
  confirmed_at: null | string
  created_at: string
  updated_at: string
}

// One instrument-resolver hit. `degraded` on the envelope means the resolver
// fell back to a local/cached source; the UI surfaces that as an inline note.
export interface FinanceInstrumentMatch {
  canonical_symbol: string
  display_name: string
  market: string
  exchange: string
  currency: string
  security_type: string
  provider_id: string
}

export interface FinanceInstrumentSearch {
  query: string
  degraded: boolean
  source: string
  matches: FinanceInstrumentMatch[]
}

export interface FinanceImportRow {
  line: number
  duplicate: boolean
  errors: string[]
  ok: boolean
  event_type: null | string
  symbol: null | string
  qty: null | number
  price: null | number
  amount: null | number
}

export interface FinanceImportPreview {
  header_error: null | string
  n_valid: number
  n_invalid: number
  n_duplicate: number
  committable: boolean
  rows: FinanceImportRow[]
}

export interface FinanceImportCommitResult {
  n_committed: number
  n_duplicate: number
  n_skipped: number
  event_ids: string[]
}

export interface FinanceAccountCreatePayload {
  name: string
  market_scope: FinancePortfolioMarket
  base_currency: string
  provider?: string
  account_type?: FinanceAccountType
  environment?: FinanceAccountEnvironment
  include_in_risk?: boolean
  note?: string
  actor: string
}

export interface FinanceAccountUpdatePayload {
  name?: string
  include_in_risk?: boolean
  note?: string
  account_type?: FinanceAccountType
  environment?: FinanceAccountEnvironment
  actor: string
}

export interface FinanceDraftCreatePayload {
  account_id: string
  event_type: string
  symbol?: string
  market?: string
  currency?: string
  qty?: number
  price?: number
  commission?: number
  amount?: number
  occurred_at?: string
  note?: string
  original_text?: string
  created_by?: string
}

// Human confirm/edit/reject on a draft. `actor` MUST be a human identity and
// the surface a human surface (desktop) — the service 403s "not_human"
// otherwise. `idempotency_key` is generated once per intent and reused on
// retry so a confirm replays instead of double-applying.
export interface FinanceDraftActionPayload {
  action: 'confirm' | 'edit' | 'reject'
  actor: string
  idempotency_key: string
  expected_version?: number
  edits?: Record<string, boolean | number | string | null>
}

export interface FinanceDraftActionResult {
  ok: boolean
  code: string
  message: string
  version: null | number
  draft: FinancePortfolioDraft | null
  event: FinancePortfolioEvent | null
}

export function getPortfolioAccounts(environment?: FinanceAccountEnvironment): Promise<FinancePortfolioAccount[]> {
  return window.hermesDesktop.api<FinancePortfolioAccount[]>({
    path: `/api/finance/v1/portfolio/accounts${financeQuery({ environment })}`
  })
}

export function getPortfolioAccount(id: string): Promise<FinancePortfolioAccount> {
  return window.hermesDesktop.api<FinancePortfolioAccount>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}`
  })
}

export function postPortfolioAccount(payload: FinanceAccountCreatePayload): Promise<FinancePortfolioAccount> {
  return window.hermesDesktop.api<FinancePortfolioAccount>({
    path: '/api/finance/v1/portfolio/accounts',
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

export function postPortfolioAccountUpdate(
  id: string,
  payload: FinanceAccountUpdatePayload
): Promise<FinancePortfolioAccount> {
  return window.hermesDesktop.api<FinancePortfolioAccount>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/update`,
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

export function getPortfolioHoldings(id: string): Promise<FinanceHoldingsResponse> {
  return window.hermesDesktop.api<FinanceHoldingsResponse>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/holdings`
  })
}

export function getPortfolioEvents(id: string): Promise<FinancePortfolioEvent[]> {
  return window.hermesDesktop.api<FinancePortfolioEvent[]>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/events`
  })
}

export function getPortfolioReconcile(id: string): Promise<FinanceReconcile> {
  return window.hermesDesktop.api<FinanceReconcile>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/reconcile`
  })
}

export function getPortfolioAggregate(
  opts: { environment?: FinanceAccountEnvironment; includeInRiskOnly?: boolean } = {}
): Promise<FinanceAggregateResponse> {
  return window.hermesDesktop.api<FinanceAggregateResponse>({
    path: `/api/finance/v1/portfolio/aggregate${financeQuery({
      environment: opts.environment,
      include_in_risk_only: opts.includeInRiskOnly
    })}`
  })
}

// Portfolio valuation (Phase 0.9 P&L). With an `accountId` this reads the single
// account's valuation; without it the cross-account aggregate (which also lists
// the accounts rolled up in `accounts`). `includeInRiskOnly` only applies to the
// aggregate — the service ignores it on the per-account path.
export function getPortfolioValuation(
  opts: {
    accountId?: string
    environment?: FinanceAccountEnvironment
    includeInRiskOnly?: boolean
  } = {}
): Promise<FinanceValuationResponse> {
  if (opts.accountId) {
    return window.hermesDesktop.api<FinanceValuationResponse>({
      path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(opts.accountId)}/valuation`
    })
  }

  return window.hermesDesktop.api<FinanceValuationResponse>({
    path: `/api/finance/v1/portfolio/valuation${financeQuery({
      environment: opts.environment,
      include_in_risk_only: opts.includeInRiskOnly
    })}`
  })
}

// Set/override the current price for one symbol — used to update a 场外基金 NAV
// by hand, since a bare fund code has no live feed. As elsewhere, the human
// `surface` rides in the body because the IPC bridge drops the X-Finance-Surface
// header; the service keeps the audit trail attributed to "desktop".
export function setPortfolioMark(payload: FinanceMarkPayload): Promise<FinanceMarkResult> {
  return window.hermesDesktop.api<FinanceMarkResult>({
    path: '/api/finance/v1/portfolio/marks',
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

// Refresh marks from live quotes for held EXCHANGE symbols. Bare fund codes have
// no live feed and come back in `skipped`, never as an error.
export function refreshPortfolioMarks(): Promise<FinanceMarksRefreshResult> {
  return window.hermesDesktop.api<FinanceMarksRefreshResult>({
    path: '/api/finance/v1/portfolio/marks/refresh',
    method: 'POST',
    body: { surface: 'desktop' }
  })
}

export function getPortfolioAudit(opts: { accountId?: string } = {}): Promise<FinancePortfolioAuditEvent[]> {
  return window.hermesDesktop.api<FinancePortfolioAuditEvent[]>({
    path: `/api/finance/v1/portfolio/audit${financeQuery({ account_id: opts.accountId })}`
  })
}

export function getPortfolioDrafts(
  opts: { accountId?: string; status?: FinanceDraftStatus } = {}
): Promise<FinancePortfolioDraft[]> {
  return window.hermesDesktop.api<FinancePortfolioDraft[]>({
    path: `/api/finance/v1/portfolio/drafts${financeQuery({ account_id: opts.accountId, status: opts.status })}`
  })
}

export function getPortfolioDraft(id: string): Promise<FinancePortfolioDraft> {
  return window.hermesDesktop.api<FinancePortfolioDraft>({
    path: `/api/finance/v1/portfolio/drafts/${encodeURIComponent(id)}`
  })
}

// Create a DRAFT event (surface desktop, human created_by). A draft is inert
// until a human confirm action applies it — this call never mutates holdings.
export function postPortfolioDraft(payload: FinanceDraftCreatePayload): Promise<FinancePortfolioDraft> {
  return window.hermesDesktop.api<FinancePortfolioDraft>({
    path: '/api/finance/v1/portfolio/drafts',
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

// Relay a HUMAN confirm/edit/reject to the draft. 200 applied/replayed, 403
// not_human, 422 incomplete/invalid_edit, 409 terminal/version_conflict, 404
// unknown — parse via parseFinanceError. Surface rides in the body (header is
// dropped by the IPC bridge); the service keeps the audit trail as "desktop".
export function postPortfolioDraftAction(
  id: string,
  payload: FinanceDraftActionPayload
): Promise<FinanceDraftActionResult> {
  return window.hermesDesktop.api<FinanceDraftActionResult>({
    path: `/api/finance/v1/portfolio/drafts/${encodeURIComponent(id)}/action`,
    method: 'POST',
    body: { surface: 'desktop', ...payload }
  })
}

// Instrument type-ahead resolver. `degraded` on the envelope flags a fallback
// source; callers surface that inline but still use the matches.
export function searchInstruments(opts: {
  q: string
  market?: string
  limit?: number
}): Promise<FinanceInstrumentSearch> {
  return window.hermesDesktop.api<FinanceInstrumentSearch>({
    path: `/api/finance/v1/instruments/search${financeQuery({ q: opts.q, market: opts.market, limit: opts.limit })}`
  })
}

export function postPortfolioImportPreview(id: string, csv: string): Promise<FinanceImportPreview> {
  return window.hermesDesktop.api<FinanceImportPreview>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/import/preview`,
    method: 'POST',
    body: { csv }
  })
}

export function postPortfolioImportCommit(id: string, csv: string, actor: string): Promise<FinanceImportCommitResult> {
  return window.hermesDesktop.api<FinanceImportCommitResult>({
    path: `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/import/commit`,
    method: 'POST',
    body: { surface: 'desktop', csv, actor }
  })
}
