import { JsonRpcGatewayClient } from '@hermes/shared'

import { reconnectBackoffDelayMs } from '@/lib/reconnect-backoff'
import type {
  ActionResponse,
  ActionStatusResponse,
  AnalyticsResponse,
  AudioSpeakResponse,
  AudioTranscriptionResponse,
  AutomationBlueprint,
  AuxiliaryModelsResponse,
  BackendUpdateCheckResponse,
  ComputerUseStatus,
  ConfigSchemaResponse,
  CronDeliveryTarget,
  CronJob,
  CronJobCreatePayload,
  CronJobUpdates,
  CuratorStatusResponse,
  CustomEndpointsResponse,
  CustomEndpointUpdate,
  CustomEndpointValidationResponse,
  DebugShareResponse,
  ElevenLabsVoicesResponse,
  EnvVarInfo,
  HermesConfig,
  HermesConfigRecord,
  LogsResponse,
  McpCatalogResponse,
  McpServerSummary,
  MemoryProviderConfig,
  MemoryProviderOAuthStatus,
  MemoryStatusResponse,
  MessagingPlatformsResponse,
  MessagingPlatformTestResponse,
  MessagingPlatformUpdate,
  MoaConfigResponse,
  ModelAssignmentRequest,
  ModelAssignmentResponse,
  ModelInfoResponse,
  ModelOptionsResponse,
  OAuthPollResponse,
  OAuthProvidersResponse,
  OAuthStartResponse,
  OAuthSubmitResponse,
  PaginatedSessions,
  PairingResponse,
  PairingUser,
  ProfileCreatePayload,
  ProfileDesktopOverlay,
  ProfileSetupCommand,
  ProfileSoul,
  ProfilesResponse,
  SessionInfo,
  SessionMessage,
  SessionMessagesResponse,
  SessionSearchResponse,
  SkillHubPreview,
  SkillHubScanResult,
  SkillHubSearchResponse,
  SkillHubSourcesResponse,
  SkillInfo,
  StarmapGraph,
  StatusResponse,
  TerminalBackendsResponse,
  ToolsetConfig,
  ToolsetInfo,
  ToolsetModelsResponse,
  WebhookCreatePayload,
  WebhookCreateResponse,
  WebhookEnableResponse,
  WebhooksResponse
} from '@/types/hermes'

// Desktop startup fires a burst of read-only data calls (config, profiles,
// model info/options, cron) the moment the backend passes readiness. On a
// profile-heavy or remote install these can each take tens of seconds — e.g.
// /api/profiles runs list_profiles(), which does a recursive skill-tree walk
// per profile — so the 15s default (DEFAULT_FETCH_TIMEOUT_MS in hardening.ts)
// times out a backend that is alive-but-busy, surfacing as a spurious
// "Timed out connecting to Hermes backend" that hangs the UI (#48504).
//
// Give the boot burst a generous per-call timeout instead of raising the
// global default: interactive/runtime calls and the liveness poll (/api/status)
// keep the short default so a genuinely-dead backend is still detected fast.
export const STARTUP_REQUEST_TIMEOUT_MS = 60_000
const DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS = 30_000
const SESSION_LIST_REQUEST_TIMEOUT_MS = 60_000
// prompt.submit is effectively fire-and-forget: turn completion is signaled by
// stream / message.complete events, NOT by the RPC return. A long turn (MoA
// presets running references + aggregator in series, deep reasoning, large tool
// chains) can legitimately take minutes to ACK, so bounding the ack by the
// generic 30s default surfaces a false "request timed out" toast while the turn
// is still running and will succeed (issue #55024). Match the backend's
// agent-turn ceiling (agent.gateway_timeout = 1800s) so the ack timeout only
// ever fires when the turn itself would have been abandoned server-side.
export const PROMPT_SUBMIT_REQUEST_TIMEOUT_MS = 1_800_000
export const AUDIO_SPEAK_MIN_REQUEST_TIMEOUT_MS = 180_000
export const AUDIO_SPEAK_MAX_REQUEST_TIMEOUT_MS = 600_000
const AUDIO_SPEAK_TIMEOUT_MS_PER_CHAR = 35

export function audioSpeakRequestTimeoutMs(text: string): number {
  const estimated = Math.max(
    AUDIO_SPEAK_MIN_REQUEST_TIMEOUT_MS,
    Math.ceil(String(text || '').length * AUDIO_SPEAK_TIMEOUT_MS_PER_CHAR)
  )

  return Math.min(AUDIO_SPEAK_MAX_REQUEST_TIMEOUT_MS, estimated)
}

export const AUDIO_TRANSCRIBE_MIN_REQUEST_TIMEOUT_MS = 180_000
export const AUDIO_TRANSCRIBE_MAX_REQUEST_TIMEOUT_MS = 600_000
// The transcribe payload is the base64 audio data URL itself, so its string
// length tracks clip size. ~0.1ms/char keeps short clips at the floor while
// letting multi-minute recordings scale toward the cap (a base64 char is
// ~0.75 bytes, so at 128kbps ≈ 21k chars/s of audio this budgets ~2s of
// timeout per 1s of audio before the cap clamps it).
const AUDIO_TRANSCRIBE_TIMEOUT_MS_PER_CHAR = 0.1

export function audioTranscribeRequestTimeoutMs(dataUrl: string): number {
  const estimated = Math.max(
    AUDIO_TRANSCRIBE_MIN_REQUEST_TIMEOUT_MS,
    Math.ceil(String(dataUrl || '').length * AUDIO_TRANSCRIBE_TIMEOUT_MS_PER_CHAR)
  )

  return Math.min(AUDIO_TRANSCRIBE_MAX_REQUEST_TIMEOUT_MS, estimated)
}

export type {
  ActionResponse,
  ActionStatusResponse,
  AnalyticsDailyEntry,
  AnalyticsModelEntry,
  AnalyticsResponse,
  AnalyticsSkillEntry,
  AnalyticsSkillsSummary,
  AnalyticsTotals,
  AudioSpeakResponse,
  AudioTranscriptionResponse,
  AutomationBlueprint,
  AutomationBlueprintField,
  AuxiliaryModelsResponse,
  BackendUpdateCheckResponse,
  ComputerUseCheck,
  ComputerUsePermissionSource,
  ComputerUseStatus,
  ConfigFieldSchema,
  ConfigSchemaResponse,
  CronDeliveryTarget,
  CronJob,
  CronJobCreatePayload,
  CronJobSchedule,
  CronJobUpdates,
  CuratorStatusResponse,
  CustomEndpoint,
  CustomEndpointsResponse,
  CustomEndpointUpdate,
  CustomEndpointValidationResponse,
  DebugShareResponse,
  ElevenLabsVoice,
  ElevenLabsVoicesResponse,
  EnvVarInfo,
  GatewayReadyPayload,
  HermesConfig,
  HermesConfigRecord,
  LogsResponse,
  McpCatalogEntry,
  McpCatalogResponse,
  McpServerSummary,
  McpServerTestResponse,
  MemoryProviderConfig,
  MemoryProviderOAuthStatus,
  MemoryStatusResponse,
  MessagingEnvVarInfo,
  MessagingHomeChannel,
  MessagingPlatformInfo,
  MessagingPlatformsResponse,
  MessagingPlatformTestResponse,
  MessagingPlatformUpdate,
  MoaConfigResponse,
  MoaModelSlot,
  ModelAssignmentRequest,
  ModelAssignmentResponse,
  ModelInfoResponse,
  ModelOptionProvider,
  ModelOptionsResponse,
  PaginatedSessions,
  PairingResponse,
  PairingUser,
  ProfileCreatePayload,
  ProfileDesktopOverlay,
  ProfileInfo,
  ProfileSetupCommand,
  ProfileSoul,
  ProfilesResponse,
  ProjectFolder,
  ProjectInfo,
  ProjectsPayload,
  RpcEvent,
  SessionCreateResponse,
  SessionInfo,
  SessionMessage,
  SessionMessagesResponse,
  SessionResumeResponse,
  SessionRuntimeInfo,
  SessionSearchResponse,
  SessionSearchResult,
  SkillHubInstalledEntry,
  SkillHubPreview,
  SkillHubResult,
  SkillHubScanResult,
  SkillHubSearchResponse,
  SkillHubSource,
  SkillHubSourcesResponse,
  SkillInfo,
  StaleAuxAssignment,
  StarmapGraph,
  StatusResponse,
  ToolsetConfig,
  ToolsetInfo,
  ToolsetModel,
  ToolsetModelsResponse,
  WebhookCreatePayload,
  WebhookCreateResponse,
  WebhookEnableResponse,
  WebhookRoute,
  WebhooksResponse
} from '@/types/hermes'

export class HermesGateway extends JsonRpcGatewayClient {
  constructor() {
    super({
      closedErrorMessage: 'Hermes gateway connection closed',
      connectErrorMessage: 'Could not connect to Hermes gateway',
      createRequestId: nextId => nextId,
      notConnectedErrorMessage: 'Hermes gateway is not connected',
      requestTimeoutMs: DEFAULT_GATEWAY_REQUEST_TIMEOUT_MS
    })
  }
}

// Profile that profile-scoped REST settings (config/env/skills/tools/model/…)
// should target. Mirrors $activeGatewayProfile, pushed in from the store via
// setApiRequestProfile so this module needs no store import (avoids a cycle).
// Electron main consumes request.profile to pick which backend *process* serves
// the call; each pooled backend already has its own HERMES_HOME, so no backend
// change is needed. Null → primary, so single-profile users are unaffected.
let _apiProfile: null | string = null

export function setApiRequestProfile(profile: null | string): void {
  _apiProfile = profile || null
}

function profileScoped(profile?: null | string): { profile?: string } {
  const selected = profile === undefined ? _apiProfile : profile

  return selected ? { profile: selected } : {}
}

/** Profile that profile-scoped REST/WS calls should target (null → primary).
 *  Read-only twin of setApiRequestProfile for modules (e.g. voice playback)
 *  that build their own connection URLs and must stay on the same backend. */
export function getApiRequestProfile(): null | string {
  return _apiProfile
}

/** Options for a plugin REST call — mirrors the app's own `hermesDesktop.api`
 *  shape, minus the path (which is namespace-derived). */
export interface PluginRestOptions {
  method?: string
  body?: unknown
  /** Single-file multipart upload (see HermesApiRequest.upload). */
  upload?: { filename: string; contentType?: string; bytes: ArrayBuffer }
  timeoutMs?: number
}

// Normalize `path` to a leading-slash suffix relative to `/api/plugins/<id>`.
// The namespace is the boundary — reject `..` so a relative segment can't
// normalize out into another plugin's API or a core route. Check the path
// portion only (before any query/hash).
function pluginPathSuffix(caller: string, path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`

  if (suffix.split(/[?#]/, 1)[0].split('/').includes('..')) {
    throw new Error(`${caller}: illegal path traversal in "${path}"`)
  }

  return suffix
}

/** The plugin REST door. Every call is scoped BY CONSTRUCTION to the plugin's
 *  own backend namespace — `path` is relative to `/api/plugins/<pluginId>`
 *  ('/board' → `/api/plugins/kanban/board`), so a plugin can't address another
 *  plugin's API or a core route through it. Profile-aware like every desktop
 *  REST call. Broader reach (core endpoints, another namespace) is the future
 *  declared-capability seam; today the namespace IS the boundary. */
export async function pluginRest<T>(pluginId: string, path: string, opts: PluginRestOptions = {}): Promise<T> {
  if (!window.hermesDesktop?.api) {
    throw new Error('Hermes desktop bridge unavailable')
  }

  const suffix = pluginPathSuffix('pluginRest', path)

  return window.hermesDesktop.api<T>({
    path: `/api/plugins/${pluginId}${suffix}`,
    method: opts.method,
    body: opts.body,
    upload: opts.upload,
    timeoutMs: opts.timeoutMs,
    ...profileScoped()
  })
}

/** The plugin WebSocket door — the live twin of `pluginRest`, scoped the same
 *  way: `path` is relative to `/api/plugins/<pluginId>` ('/events' → the
 *  plugin's own event stream). Token-mode backends auth via the same query
 *  credential the app's own sockets use; OAuth remotes resolve null (callers
 *  keep their polling fallback — every consumer must have one anyway, since a
 *  socket can drop). Auto-reconnects with backoff until disposed. */
export function pluginSocket(pluginId: string, path: string, onMessage: (data: unknown) => void): () => void {
  const suffix = pluginPathSuffix('pluginSocket', path)

  let socket: null | WebSocket = null
  let disposed = false
  let attempt = 0

  const connect = async () => {
    const connection = await window.hermesDesktop.getConnection().catch(() => null)

    // No bridge / OAuth cookie auth (WS tickets are single-use, core-managed):
    // stay on the polling fallback rather than half-working.
    if (disposed || !connection || connection.authMode === 'oauth') {
      return
    }

    const base = connection.baseUrl.replace(/^http/, 'ws')
    const join = suffix.includes('?') ? '&' : '?'
    socket = new WebSocket(
      `${base}/api/plugins/${pluginId}${suffix}${join}token=${encodeURIComponent(connection.token)}`
    )

    socket.onmessage = event => {
      attempt = 0

      try {
        onMessage(JSON.parse(String(event.data)))
      } catch {
        // Non-JSON frame — plugin streams are JSON by contract; skip it.
      }
    }

    socket.onclose = () => {
      socket = null

      if (!disposed) {
        // Full-jitter exponential backoff: same rationale as the gateway
        // socket reconnect loops — an immediate-retry loop across many
        // desktop clients floods the gateway with connection attempts
        // during a restart.
        window.setTimeout(() => void connect(), reconnectBackoffDelayMs(attempt, { baseDelayMs: 500, capMs: 30_000 }))
        attempt += 1
      }
    }
  }

  void connect()

  return () => {
    disposed = true
    socket?.close()
  }
}

/**
 * Trim a page to its window WITHOUT discarding pinned rows.
 *
 * The list endpoints deliberately back-fill pinned conversations past their
 * LIMIT — a pin means "always reachable", so an aged-out pinned chat is
 * appended after the recency window. A plain `slice(0, limit)` throws exactly
 * those rows away again, which is why pins silently stopped rendering past
 * some count: the sidebar could only ever show the pins that happened to fall
 * inside the most-recent page.
 */
function pageWindow(sessions: SessionInfo[], limit: number): SessionInfo[] {
  if (sessions.length <= limit) {
    return sessions
  }

  const recent = sessions.slice(0, limit)

  return [...recent, ...sessions.slice(limit).filter(session => session.pinned)]
}

export async function listSessions(
  limit = 40,
  minMessages = 0,
  archived: 'exclude' | 'include' | 'only' = 'exclude',
  order: 'created' | 'recent' = 'recent'
): Promise<PaginatedSessions> {
  const result = await window.hermesDesktop.api<PaginatedSessions>({
    path:
      `/api/sessions?limit=${limit}&offset=0&min_messages=${Math.max(0, minMessages)}` +
      `&archived=${archived}&order=${order}`,
    timeoutMs: SESSION_LIST_REQUEST_TIMEOUT_MS
  })

  return {
    ...result,
    sessions: pageWindow(result.sessions, limit),
    offset: 0
  }
}

// Unified, read-only session list aggregated across ALL profiles. Served by the
// primary backend straight off each profile's state.db — no per-profile backend
// is spawned. Single-profile users get the same rows as listSessions(), tagged
// profile="default".
// Source scoping lets callers split the unified list into independent slices:
// recents pass `excludeSources: ['cron']`, the cron-jobs section passes
// `source: 'cron'`. Without this a burst of (always-newest) cron sessions
// consumes the whole recents page and starves real conversations.
export interface SessionSourceFilter {
  source?: string
  excludeSources?: string[]
}

export async function listAllProfileSessions(
  limit = 40,
  minMessages = 0,
  archived: 'exclude' | 'include' | 'only' = 'exclude',
  order: 'created' | 'recent' = 'recent',
  profile: 'all' | (string & {}) = 'all',
  filter: SessionSourceFilter = {}
): Promise<PaginatedSessions> {
  const sourceParam = filter.source ? `&source=${encodeURIComponent(filter.source)}` : ''

  const excludeParam = filter.excludeSources?.length
    ? `&exclude_sources=${encodeURIComponent(filter.excludeSources.join(','))}`
    : ''

  const result = await window.hermesDesktop.api<PaginatedSessions>({
    path:
      `/api/profiles/sessions?limit=${limit}&offset=0&min_messages=${Math.max(0, minMessages)}` +
      `&archived=${archived}&order=${order}&profile=${encodeURIComponent(profile)}${sourceParam}${excludeParam}`,
    timeoutMs: SESSION_LIST_REQUEST_TIMEOUT_MS
  })

  return {
    ...result,
    sessions: pageWindow(result.sessions, limit),
    offset: 0
  }
}

// Batched sidebar slices in one request: recents (scoped to the active profile),
// cron, and messaging. The backend opens each profile's state.db once and runs
// all three filtered queries, replacing three separate listAllProfileSessions
// calls that each reopened + re-counted every profile DB per refresh. Electron
// splices remote profiles per slice (see interceptSessionRequestForRemote).
export interface SidebarSessionSlice {
  sessions: SessionInfo[]
  /** Per-profile "the window came back full, more rows exist on disk" flags —
   *  what pagination needs, without a COUNT(*) per profile DB per refresh. */
  profiles_truncated?: Record<string, boolean>
  /** Per-profile tokens and spend over every session, not just this window.
   *  Absent from the legacy per-slice endpoint, which has no aggregate. */
  profiles_usage?: Record<string, { cost_usd: number; tokens: number }>
}

/** Which profiles filled their per-profile window in a returned page. The
 *  legacy per-slice endpoint doesn't report this, so derive it from the rows:
 *  a profile at (or over) the cap still has more on disk. Pinned rows are
 *  discounted — they're back-filled past the LIMIT, so counting them fakes a
 *  full page and leaves a "Load more" that can never resolve. */
function profilesTruncatedFrom(sessions: SessionInfo[], cap: number): Record<string, boolean> {
  const counts = new Map<string, number>()

  for (const session of sessions) {
    const key = session.profile || 'default'

    counts.set(key, (counts.get(key) ?? 0) + (session.pinned ? 0 : 1))
  }

  return Object.fromEntries([...counts].map(([name, count]) => [name, count >= cap]))
}

export interface SidebarSessionsResponse {
  recents: SidebarSessionSlice
  cron: SidebarSessionSlice
  messaging: SidebarSessionSlice
  errors?: Array<{ profile: string; error: string }>
}

export interface SidebarSessionsRequest {
  recentsProfile: 'all' | (string & {})
  recentsLimit: number
  recentsExclude: string[]
  cronLimit: number
  messagingLimit: number
  messagingExclude: string[]
}

// The batched /sidebar endpoint shipped later than the per-slice route, so a
// newer desktop can meet an older backend that 404s it ("No such API
// endpoint"). Endpoint-missing is a capability signal, not a transient
// failure: remember it (per renderer lifetime — a runtime home change reloads
// the window and re-probes) and serve every subsequent refresh straight from
// the three proven per-slice calls instead of re-probing a known-dead route
// once per turn/broadcast.
let sidebarBatchEndpointMissing = false

// Capability flags are per-backend facts. A hard re-home reloads the window
// (module state resets naturally), but a soft gateway switch re-dials in
// place — the next backend may well have the batched route, so the switch
// paths call this to re-probe rather than leak the old backend's capability.
export function resetSidebarBatchCapability() {
  sidebarBatchEndpointMissing = false
}

// True only for "the route does not exist on this backend" shapes: the
// backend catch-all ('404: {"detail":"No such API endpoint: ...}'), FastAPI's
// bare 404 on headless serve (surfaces as '404: ...' directly or as
// "Error invoking remote method 'hermes:api': Error: 404: ..." through the
// IPC bridge), and the Electron JSON-guard ("endpoint is likely missing").
// This GET has no path params, so a 404 status can only mean route-missing —
// but transient failures (timeouts, 5xx, connection refused) must NOT match,
// or one blip would silently degrade the fast path for the whole session.
function isEndpointMissingError(err: unknown): boolean {
  const message = err instanceof Error ? err.message : String(err)

  return (
    /no such api endpoint/i.test(message) ||
    /endpoint is likely missing/i.test(message) ||
    /(?:^\s*|error:\s*)404\b/i.test(message)
  )
}

// Compatibility fallback: reassemble the three sidebar slices from the
// per-slice endpoint, mirroring the batched route's semantics (min_messages=1,
// archived excluded, recency order; recents scoped to the caller's profile,
// cron + messaging cross-profile). Rides the same Electron remote-splice
// interception as the pre-batching desktop, so remote profiles stay correct.
async function listSidebarSessionsLegacy(req: SidebarSessionsRequest): Promise<SidebarSessionsResponse> {
  const [recents, cron, messaging] = await Promise.all([
    listAllProfileSessions(req.recentsLimit, 1, 'exclude', 'recent', req.recentsProfile, {
      excludeSources: req.recentsExclude
    }),
    listAllProfileSessions(req.cronLimit, 1, 'exclude', 'recent', 'all', { source: 'cron' }),
    listAllProfileSessions(req.messagingLimit, 1, 'exclude', 'recent', 'all', {
      excludeSources: req.messagingExclude
    })
  ])

  const errors = [...(recents.errors ?? []), ...(cron.errors ?? []), ...(messaging.errors ?? [])]

  return {
    recents: {
      profiles_truncated: profilesTruncatedFrom(recents.sessions, req.recentsLimit),
      sessions: recents.sessions
    },
    cron: { sessions: cron.sessions },
    messaging: { sessions: messaging.sessions },
    ...(errors.length ? { errors } : {})
  }
}

/** The PR each of these sessions opened, recovered from its own transcript —
 *  for sessions whose recorded branch can't answer (they started on trunk and
 *  did the work in a worktree). Also returns every id it looked at, so the
 *  caller can remember a miss and never ask again. */
export function scanSessionPullRequests(
  ids: string[]
): Promise<{ pull_requests: Record<string, { number: number; url: string }>; scanned: string[] }> {
  return window.hermesDesktop.api<{
    pull_requests: Record<string, { number: number; url: string }>
    scanned: string[]
  }>({
    path: '/api/profiles/sessions/pull-requests',
    method: 'POST',
    body: { ids }
  })
}

export async function listSidebarSessions(req: SidebarSessionsRequest): Promise<SidebarSessionsResponse> {
  if (sidebarBatchEndpointMissing) {
    return listSidebarSessionsLegacy(req)
  }

  const params = new URLSearchParams({
    recents_profile: req.recentsProfile,
    recents_limit: String(Math.max(1, req.recentsLimit)),
    cron_limit: String(Math.max(1, req.cronLimit)),
    messaging_limit: String(Math.max(1, req.messagingLimit))
  })

  if (req.recentsExclude.length) {
    params.set('recents_exclude', req.recentsExclude.join(','))
  }

  if (req.messagingExclude.length) {
    params.set('messaging_exclude', req.messagingExclude.join(','))
  }

  let result: SidebarSessionsResponse

  try {
    result = await window.hermesDesktop.api<SidebarSessionsResponse>({
      path: `/api/profiles/sessions/sidebar?${params.toString()}`,
      timeoutMs: SESSION_LIST_REQUEST_TIMEOUT_MS
    })
  } catch (err) {
    if (!isEndpointMissingError(err)) {
      throw err
    }

    // Older backend without the batched route (desktop/runtime version skew).
    sidebarBatchEndpointMissing = true

    return listSidebarSessionsLegacy(req)
  }

  return {
    recents: { ...result.recents, sessions: result.recents?.sessions ?? [] },
    cron: { ...result.cron, sessions: result.cron?.sessions ?? [] },
    messaging: { ...result.messaging, sessions: result.messaging?.sessions ?? [] },
    errors: result.errors
  }
}

// Mutations take the owning `profile` so Electron routes them to that profile's
// backend (remote pool or local primary) via request.profile — matching the
// read path. A remote session's row lives only on its remote host, so a mutation
// that hit the local primary would no-op or 404. Omit for the current/default.
export function setSessionArchived(id: string, archived: boolean, profile?: string | null): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}`,
    method: 'PATCH',
    body: { archived }
  })
}

// Mirror a sidebar pin to the backend "keep" flag so the sessions.auto_archive
// sweep (which runs backend-side, blind to Desktop localStorage) never hides a
// pinned chat. Best-effort: the sidebar stays localStorage-driven for its own
// display; this only feeds the backend policy.
export function setSessionPinnedRemote(id: string, pinned: boolean, profile?: string | null): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}`,
    method: 'PATCH',
    body: { pinned }
  })
}

export function searchSessions(query: string): Promise<SessionSearchResponse> {
  return window.hermesDesktop.api<SessionSearchResponse>({
    path: `/api/sessions/search?q=${encodeURIComponent(query)}`
  })
}

// Resolves a single session row by id on one backend (the active profile, or
// the given `profile`). The backend resolves exact ids and unique prefixes and
// 404s when the id isn't on that profile — so a cheap by-id lookup replaces the
// cross-profile list scan when locating an unknown id's owner.
export function getSession(id: string, profile?: string | null): Promise<SessionInfo> {
  const suffix = profile ? `?profile=${encodeURIComponent(profile)}` : ''

  return window.hermesDesktop.api<SessionInfo>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}${suffix}`
  })
}

// Reads another profile's transcript. For a remote profile Electron reroutes
// this GET to the remote backend (which serves its own state.db); for a local
// profile the primary opens that profile's state.db via ?profile=. Omit for
// the current/default profile.
export function getSessionMessages(
  id: string,
  profile?: string | null,
  page: { limit?: number; offset?: number; order?: 'latest' | 'oldest' } = {}
): Promise<SessionMessagesResponse> {
  const query = new URLSearchParams()

  if (profile) {
    query.set('profile', profile)
  }

  if (page.limit !== undefined) {
    query.set('limit', String(page.limit))
  }

  if (page.offset !== undefined) {
    query.set('offset', String(page.offset))
  }

  if (page.order) {
    query.set('order', page.order)
  }

  const suffix = query.size ? `?${query.toString()}` : ''

  return window.hermesDesktop.api<SessionMessagesResponse>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}/messages${suffix}`
  })
}

export function getLatestSessionMessages(id: string, profile?: string | null): Promise<SessionMessagesResponse> {
  return getSessionMessages(id, profile, { limit: 500, order: 'latest' })
}

export async function getAllSessionMessages(
  id: string,
  profile?: string | null,
  options: { maxJsonChars?: number } = {}
): Promise<SessionMessagesResponse> {
  const messages: SessionMessage[] = []
  const pageSize = 500
  const maxJsonChars = options.maxJsonChars ?? 32_000_000
  let jsonChars = 0
  let offset = 0
  let resolvedSessionId = id

  while (true) {
    const page = await getSessionMessages(id, profile, {
      limit: pageSize,
      offset,
      order: 'oldest'
    })

    resolvedSessionId = page.session_id
    jsonChars += (JSON.stringify(page.messages) ?? '').length

    if (jsonChars > maxJsonChars) {
      throw new Error(
        'Session transcript exceeds the Desktop safe-load limit; use the Web Dashboard export for this session.'
      )
    }

    messages.push(...page.messages)

    // Legacy backends ignore pagination and return the full transcript.
    if (!page.pagination || page.messages.length === 0 || page.messages.length < page.pagination.limit) {
      break
    }

    offset += page.messages.length
  }

  return { session_id: resolvedSessionId, messages }
}

export function deleteSession(id: string, profile?: string | null): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}`,
    method: 'DELETE'
  })
}

export function renameSession(
  id: string,
  title: string,
  profile?: string | null
): Promise<{ ok: boolean; title: string }> {
  return window.hermesDesktop.api<{ ok: boolean; title: string }>({
    ...(profile ? { profile } : {}),
    path: `/api/sessions/${encodeURIComponent(id)}`,
    method: 'PATCH',
    body: { title, ...(profile ? { profile } : {}) }
  })
}

export function getGlobalModelInfo(): Promise<ModelInfoResponse> {
  return window.hermesDesktop.api<ModelInfoResponse>({
    ...profileScoped(),
    path: '/api/model/info',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function getStatus(): Promise<StatusResponse> {
  return window.hermesDesktop.api<StatusResponse>({
    ...profileScoped(),
    path: '/api/status'
  })
}

export function getLogs(params: {
  component?: string
  file?: string
  level?: string
  lines?: number
  search?: string
}): Promise<LogsResponse> {
  const query = new URLSearchParams()

  if (params.file) {
    query.set('file', params.file)
  }

  if (typeof params.lines === 'number') {
    query.set('lines', String(params.lines))
  }

  if (params.level && params.level !== 'ALL') {
    query.set('level', params.level)
  }

  if (params.component && params.component !== 'all') {
    query.set('component', params.component)
  }

  if (params.search) {
    query.set('search', params.search)
  }

  const suffix = query.toString()

  return window.hermesDesktop.api<LogsResponse>({
    ...profileScoped(),
    path: suffix ? `/api/logs?${suffix}` : '/api/logs'
  })
}

export function getHermesConfig(profile?: string): Promise<HermesConfig> {
  return window.hermesDesktop.api<HermesConfig>({
    ...profileScoped(profile),
    path: '/api/config',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function getHermesConfigRecord(): Promise<HermesConfigRecord> {
  return window.hermesDesktop.api<HermesConfigRecord>({
    ...profileScoped(),
    path: '/api/config'
  })
}

export function getHermesConfigDefaults(): Promise<HermesConfigRecord> {
  return window.hermesDesktop.api<HermesConfigRecord>({
    ...profileScoped(),
    path: '/api/config/defaults',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function getHermesConfigSchema(): Promise<ConfigSchemaResponse> {
  return window.hermesDesktop.api<ConfigSchemaResponse>({
    ...profileScoped(),
    path: '/api/config/schema'
  })
}

export function saveHermesConfig(config: HermesConfigRecord): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: '/api/config',
    method: 'PUT',
    body: { config }
  })
}

// surface=declared serves the curated desktop schema; the dashboard consumes the raw plugin schema.
export function getMemoryProviderConfig(provider: string): Promise<MemoryProviderConfig> {
  return window.hermesDesktop.api<MemoryProviderConfig>({
    ...profileScoped(),
    path: `/api/memory/providers/${encodeURIComponent(provider)}/config?surface=declared`
  })
}

export function saveMemoryProviderConfig(provider: string, values: Record<string, string>): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/memory/providers/${encodeURIComponent(provider)}/config?surface=declared`,
    method: 'PUT',
    body: { values }
  })
}

export function getEnvVars(): Promise<Record<string, EnvVarInfo>> {
  return window.hermesDesktop.api<Record<string, EnvVarInfo>>({
    ...profileScoped(),
    path: '/api/env'
  })
}

export function setEnvVar(key: string, value: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: '/api/env',
    method: 'PUT',
    body: { key, value }
  })
}

export function validateProviderCredential(
  key: string,
  value: string,
  apiKey?: string
): Promise<{ ok: boolean; reachable: boolean; message: string; models?: string[] }> {
  return window.hermesDesktop.api<{ ok: boolean; reachable: boolean; message: string; models?: string[] }>({
    ...profileScoped(),
    path: '/api/providers/validate',
    method: 'POST',
    body: { key, value, api_key: apiKey ?? '' }
  })
}

export function getCustomEndpoints(): Promise<CustomEndpointsResponse> {
  return window.hermesDesktop.api<CustomEndpointsResponse>({
    path: '/api/providers/custom-endpoints'
  })
}

export function saveCustomEndpoint(endpoint: CustomEndpointUpdate): Promise<CustomEndpointsResponse> {
  return window.hermesDesktop.api<CustomEndpointsResponse>({
    path: '/api/providers/custom-endpoints',
    method: 'POST',
    body: endpoint
  })
}

export function validateCustomEndpoint(endpoint: CustomEndpointUpdate): Promise<CustomEndpointValidationResponse> {
  return window.hermesDesktop.api<CustomEndpointValidationResponse>({
    path: '/api/providers/custom-endpoints/validate',
    method: 'POST',
    body: endpoint
  })
}

export function activateCustomEndpoint(id: string): Promise<{ ok: boolean; provider: string; model: string }> {
  return window.hermesDesktop.api<{ ok: boolean; provider: string; model: string }>({
    path: `/api/providers/custom-endpoints/${encodeURIComponent(id)}/activate`,
    method: 'POST'
  })
}

export function deleteCustomEndpoint(id: string): Promise<CustomEndpointsResponse> {
  return window.hermesDesktop.api<CustomEndpointsResponse>({
    path: `/api/providers/custom-endpoints/${encodeURIComponent(id)}`,
    method: 'DELETE'
  })
}

export function deleteEnvVar(key: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: '/api/env',
    method: 'DELETE',
    body: { key }
  })
}

export function revealEnvVar(key: string): Promise<{ key: string; value: string }> {
  return window.hermesDesktop.api<{ key: string; value: string }>({
    ...profileScoped(),
    path: '/api/env/reveal',
    method: 'POST',
    body: { key }
  })
}

export function listOAuthProviders(): Promise<OAuthProvidersResponse> {
  return window.hermesDesktop.api<OAuthProvidersResponse>({
    ...profileScoped(),
    path: '/api/providers/oauth'
  })
}

export function disconnectOAuthProvider(providerId: string): Promise<{ ok: boolean; provider: string }> {
  return window.hermesDesktop.api<{ ok: boolean; provider: string }>({
    ...profileScoped(),
    path: `/api/providers/oauth/${encodeURIComponent(providerId)}`,
    method: 'DELETE'
  })
}

export function startOAuthLogin(providerId: string): Promise<OAuthStartResponse> {
  return window.hermesDesktop.api<OAuthStartResponse>({
    ...profileScoped(),
    path: `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
    method: 'POST',
    body: {}
  })
}

export function submitOAuthCode(providerId: string, sessionId: string, code: string): Promise<OAuthSubmitResponse> {
  return window.hermesDesktop.api<OAuthSubmitResponse>({
    ...profileScoped(),
    path: `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
    method: 'POST',
    body: { session_id: sessionId, code }
  })
}

export function pollOAuthSession(providerId: string, sessionId: string): Promise<OAuthPollResponse> {
  return window.hermesDesktop.api<OAuthPollResponse>({
    ...profileScoped(),
    path: `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`
  })
}

export function cancelOAuthSession(sessionId: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
    method: 'DELETE'
  })
}

// Memory-provider OAuth connect (provider-keyed; 404s for providers without an
// OAuth flow). Profile-scoped: the grant lands in the active profile's config.
export function startMemoryProviderOAuth(provider: string): Promise<MemoryProviderOAuthStatus> {
  return window.hermesDesktop.api<MemoryProviderOAuthStatus>({
    ...profileScoped(),
    path: `/api/memory/providers/${encodeURIComponent(provider)}/oauth/start`,
    method: 'POST'
  })
}

export function getMemoryProviderOAuthStatus(provider: string): Promise<MemoryProviderOAuthStatus> {
  return window.hermesDesktop.api<MemoryProviderOAuthStatus>({
    ...profileScoped(),
    path: `/api/memory/providers/${encodeURIComponent(provider)}/oauth/status`
  })
}

export function getSkills(): Promise<SkillInfo[]> {
  return window.hermesDesktop.api<SkillInfo[]>({
    ...profileScoped(),
    path: '/api/skills'
  })
}

export function getStarmapGraph(): Promise<StarmapGraph> {
  return window.hermesDesktop.api<StarmapGraph>({
    ...profileScoped(),
    // Backend REST contract — stays /api/learning even though the UI feature is
    // now "star map". Renaming this would break against an un-upgraded backend.
    path: '/api/learning/graph'
  })
}

export interface LearningNodeDetail {
  content: string
  kind: 'memory' | 'skill'
  label: string
  ok: boolean
}

export function getLearningNode(id: string): Promise<LearningNodeDetail> {
  return window.hermesDesktop.api<LearningNodeDetail>({
    ...profileScoped(),
    path: `/api/learning/node?id=${encodeURIComponent(id)}`
  })
}

export function deleteLearningNode(id: string): Promise<{ message: string; ok: boolean }> {
  return window.hermesDesktop.api<{ message: string; ok: boolean }>({
    ...profileScoped(),
    path: '/api/learning/node',
    method: 'DELETE',
    body: { id }
  })
}

export function editLearningNode(id: string, content: string): Promise<{ message: string; ok: boolean }> {
  return window.hermesDesktop.api<{ message: string; ok: boolean }>({
    ...profileScoped(),
    path: '/api/learning/node',
    method: 'PUT',
    body: { content, id }
  })
}

export function setSkillEnabled(
  name: string,
  enabled: boolean
): Promise<{ ok: boolean; name: string; enabled: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean; name: string; enabled: boolean }>({
    ...profileScoped(),
    path: '/api/skills/toggle',
    method: 'PUT',
    body: { name, enabled }
  })
}

export interface McpTestResult {
  ok: boolean
  error?: string
  tools: { name: string; description: string }[]
  /** Capability counts (absent on older backends / failed probes). */
  prompts?: number
  resources?: number
}

export interface McpOAuthFlow {
  flow_id: string
  server_name: string
  status: 'starting' | 'authorization_required' | 'approved' | 'error'
  authorization_url: string | null
  error: string | null
  tools?: { name: string; description: string }[]
}

/** Connect to the server, list its tools, disconnect. Slow (spawns/handshakes
 *  for real) — well past the 15s default fetch timeout. */
export function testMcpServer(name: string): Promise<McpTestResult> {
  return window.hermesDesktop.api<McpTestResult>({
    ...profileScoped(),
    path: `/api/mcp/servers/${encodeURIComponent(name)}/test`,
    method: 'POST',
    timeoutMs: 60_000
  })
}

/** Replace the whole `mcp_servers` map (the mcp.json editor's save). Unlike
 *  `saveHermesConfig`, this REPLACES rather than deep-merges, so deletes,
 *  re-enables (dropping `enabled: false`), and removed nested fields persist. */
export function saveMcpServers(servers: Record<string, Record<string, unknown>>): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: '/api/mcp/servers',
    method: 'PUT',
    body: { servers }
  })
}

/** Start an MCP OAuth flow and return the authorization URL. */
export function authMcpServer(name: string): Promise<McpOAuthFlow> {
  return window.hermesDesktop.api<McpOAuthFlow>({
    ...profileScoped(),
    path: `/api/mcp/servers/${encodeURIComponent(name)}/auth`,
    method: 'POST',
    timeoutMs: 60_000
  })
}

export function getMcpOAuthFlow(flowId: string): Promise<McpOAuthFlow> {
  return window.hermesDesktop.api<McpOAuthFlow>({
    ...profileScoped(),
    path: `/api/mcp/oauth/flows/${encodeURIComponent(flowId)}`
  })
}

export function getToolsets(): Promise<ToolsetInfo[]> {
  return window.hermesDesktop.api<ToolsetInfo[]>({
    ...profileScoped(),
    path: '/api/tools/toolsets'
  })
}

export function setToolsetEnabled(
  name: string,
  enabled: boolean
): Promise<{ ok: boolean; name: string; enabled: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean; name: string; enabled: boolean }>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}`,
    method: 'PUT',
    body: { enabled }
  })
}

export function getToolsetConfig(name: string): Promise<ToolsetConfig> {
  return window.hermesDesktop.api<ToolsetConfig>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}/config`
  })
}

export function getToolsetModels(name: string, provider?: string): Promise<ToolsetModelsResponse> {
  const suffix = provider ? `?provider=${encodeURIComponent(provider)}` : ''

  return window.hermesDesktop.api<ToolsetModelsResponse>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}/models${suffix}`
  })
}

export function selectToolsetModel(
  name: string,
  model: string,
  provider?: string
): Promise<{ ok: boolean; name: string; model: string }> {
  return window.hermesDesktop.api<{ ok: boolean; name: string; model: string }>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}/model`,
    method: 'PUT',
    body: { model, provider }
  })
}

export interface SelectToolsetProviderResponse {
  ok: boolean
  name: string
  provider: string
  /** Present when the selection was scoped to one web capability. */
  capability?: string
  /** Present (true) when a managed Nous row was selected but the Portal
   *  entitlement is missing — the row won't activate until the user signs
   *  in to Nous Portal. */
  needs_nous_auth?: boolean
  /** The managed feature key (e.g. "browser") when needs_nous_auth is set. */
  feature?: string
}

export function selectToolsetProvider(
  name: string,
  provider: string,
  capability?: 'search' | 'extract'
): Promise<SelectToolsetProviderResponse> {
  return window.hermesDesktop.api<SelectToolsetProviderResponse>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}/provider`,
    method: 'PUT',
    body: capability ? { provider, capability } : { provider }
  })
}

export function runToolsetPostSetup(name: string, key: string): Promise<ActionResponse & { key: string }> {
  return window.hermesDesktop.api<ActionResponse & { key: string }>({
    ...profileScoped(),
    path: `/api/tools/toolsets/${encodeURIComponent(name)}/post-setup`,
    method: 'POST',
    body: { key }
  })
}

export function getTerminalBackends(): Promise<TerminalBackendsResponse> {
  return window.hermesDesktop.api<TerminalBackendsResponse>({
    ...profileScoped(),
    path: '/api/tools/terminal/backends'
  })
}

export function selectTerminalBackend(backend: string): Promise<{ ok: boolean; backend: string }> {
  return window.hermesDesktop.api<{ ok: boolean; backend: string }>({
    ...profileScoped(),
    path: '/api/tools/terminal/backend',
    method: 'PUT',
    body: { backend }
  })
}

export function getComputerUseStatus(): Promise<ComputerUseStatus> {
  return window.hermesDesktop.api<ComputerUseStatus>({
    ...profileScoped(),
    path: '/api/tools/computer-use/status'
  })
}

export function grantComputerUsePermissions(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/tools/computer-use/permissions/grant',
    method: 'POST'
  })
}

export function getMessagingPlatforms(): Promise<MessagingPlatformsResponse> {
  return window.hermesDesktop.api<MessagingPlatformsResponse>({
    path: '/api/messaging/platforms'
  })
}

export function updateMessagingPlatform(
  platformId: string,
  body: MessagingPlatformUpdate
): Promise<{ ok: boolean; platform: string }> {
  return window.hermesDesktop.api<{ ok: boolean; platform: string }>({
    path: `/api/messaging/platforms/${encodeURIComponent(platformId)}`,
    method: 'PUT',
    body
  })
}

export function testMessagingPlatform(platformId: string): Promise<MessagingPlatformTestResponse> {
  return window.hermesDesktop.api<MessagingPlatformTestResponse>({
    path: `/api/messaging/platforms/${encodeURIComponent(platformId)}/test`,
    method: 'POST'
  })
}

// -- Pairing (who may DM the bot) --------------------------------------------
// Unknown DMers get a one-time code and land in `pending` until an admin
// approves them. Approval grants on the row's `request_id`, never on the code:
// the code is the requester's proof that the channel is theirs and is never
// returned by the API, while an authenticated admin is only ever identifying
// a row they can already see.

export function getPairing(): Promise<PairingResponse> {
  return window.hermesDesktop.api<PairingResponse>({
    ...profileScoped(),
    path: '/api/pairing'
  })
}

export function approvePairing(platform: string, requestId: string): Promise<{ ok: boolean; user: PairingUser }> {
  return window.hermesDesktop.api<{ ok: boolean; user: PairingUser }>({
    ...profileScoped(),
    path: '/api/pairing/approve',
    method: 'POST',
    // These endpoints read the profile off the body, not the query string —
    // `profileScoped()` alone would approve into the wrong profile's store.
    body: { platform, request_id: requestId, ...profileScoped() }
  })
}

export function revokePairing(platform: string, userId: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: '/api/pairing/revoke',
    method: 'POST',
    body: { platform, user_id: userId, ...profileScoped() }
  })
}

// -- Webhooks (subscription CRUD) --------------------------------------------
// The webhook receiver is its own gateway platform; subscriptions live in a
// shared JSON store the CLI/dashboard also drive. Enable mutates config and
// best-effort restarts the gateway; subscription changes hot-reload.

export function getWebhooks(): Promise<WebhooksResponse> {
  return window.hermesDesktop.api<WebhooksResponse>({
    ...profileScoped(),
    path: '/api/webhooks'
  })
}

export function enableWebhooks(): Promise<WebhookEnableResponse> {
  return window.hermesDesktop.api<WebhookEnableResponse>({
    ...profileScoped(),
    path: '/api/webhooks/enable',
    method: 'POST'
  })
}

export function createWebhook(body: WebhookCreatePayload): Promise<WebhookCreateResponse> {
  return window.hermesDesktop.api<WebhookCreateResponse>({
    ...profileScoped(),
    path: '/api/webhooks',
    method: 'POST',
    body
  })
}

export function deleteWebhook(name: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/webhooks/${encodeURIComponent(name)}`,
    method: 'DELETE'
  })
}

export function setWebhookEnabled(
  name: string,
  enabled: boolean
): Promise<{ enabled: boolean; name: string; ok: boolean }> {
  return window.hermesDesktop.api<{ enabled: boolean; name: string; ok: boolean }>({
    ...profileScoped(),
    path: `/api/webhooks/${encodeURIComponent(name)}/enabled`,
    method: 'PUT',
    body: { enabled }
  })
}

// Cron jobs are stored per-profile (<HERMES_HOME>/cron/jobs.json), and the
// backend's list endpoint defaults to 'all'. Pass a concrete profile key to
// list just that profile's jobs, or 'all' for the unified cross-profile view.
// Omitting the arg keeps the legacy 'all' default for non-profile callers.
// profileScoped() still rides along for backend-process routing.
export function getCronJobs(profile?: string): Promise<CronJob[]> {
  const suffix = profile ? `?profile=${encodeURIComponent(profile)}` : ''

  return window.hermesDesktop.api<CronJob[]>({
    ...profileScoped(),
    path: `/api/cron/jobs${suffix}`,
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function getCronJob(jobId: string): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}`
  })
}

export async function getCronJobRuns(jobId: string, limit = 20): Promise<SessionInfo[]> {
  const { runs } = await window.hermesDesktop.api<{ runs: SessionInfo[] }>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/runs?limit=${limit}`
  })

  return runs ?? []
}

// The single source of truth for cron delivery targets (local + configured
// gateways). Both the manual cron editor and the blueprint dialog use this so
// they never offer a platform that isn't connected. Mirrors the dashboard.
export async function getCronDeliveryTargets(): Promise<CronDeliveryTarget[]> {
  const { targets } = await window.hermesDesktop.api<{ targets: CronDeliveryTarget[] }>({
    ...profileScoped(),
    path: '/api/cron/delivery-targets'
  })

  return targets ?? []
}

export function createCronJob(body: CronJobCreatePayload): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: '/api/cron/jobs',
    method: 'POST',
    body
  })
}

export function updateCronJob(jobId: string, updates: CronJobUpdates): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}`,
    method: 'PUT',
    body: { updates }
  })
}

export function pauseCronJob(jobId: string): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/pause`,
    method: 'POST'
  })
}

export function resumeCronJob(jobId: string): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/resume`,
    method: 'POST'
  })
}

export function triggerCronJob(jobId: string): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}/trigger`,
    method: 'POST'
  })
}

export function deleteCronJob(jobId: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/cron/jobs/${encodeURIComponent(jobId)}`,
    method: 'DELETE'
  })
}

// Automation Blueprints — parameterized cron templates the backend serves from
// cron/blueprint_catalog.py. getAutomationBlueprints returns the gallery
// (deliver options already rewritten to this machine's configured gateways);
// instantiateAutomationBlueprint fills the slots and creates a real cron job via
// the same create_job path as createCronJob.
//
// Profile-scoping is intentionally asymmetric: the GET catalog is global (the
// list endpoint takes no profile — only deliver options are rewritten from the
// configured gateways), so it carries only the profileScoped() header for
// routing. instantiate creates a real per-profile job, so it names the target
// profile explicitly via ?profile=. This mirrors the dashboard's api.ts.
export function getAutomationBlueprints(): Promise<{ blueprints: AutomationBlueprint[] }> {
  return window.hermesDesktop.api<{ blueprints: AutomationBlueprint[] }>({
    ...profileScoped(),
    path: '/api/cron/blueprints',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function instantiateAutomationBlueprint(
  body: { blueprint: string; values: Record<string, string> },
  profile: string
): Promise<CronJob> {
  return window.hermesDesktop.api<CronJob>({
    ...profileScoped(),
    path: `/api/cron/blueprints/instantiate?profile=${encodeURIComponent(profile)}`,
    method: 'POST',
    body
  })
}

export function getProfiles(): Promise<ProfilesResponse> {
  return window.hermesDesktop.api<ProfilesResponse>({
    path: '/api/profiles',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function createProfile(body: ProfileCreatePayload): Promise<{ name: string; ok: boolean; path: string }> {
  return window.hermesDesktop.api<{ name: string; ok: boolean; path: string }>({
    path: '/api/profiles',
    method: 'POST',
    body
  })
}

export function renameProfile(name: string, newName: string): Promise<{ name: string; ok: boolean; path: string }> {
  return window.hermesDesktop.api<{ name: string; ok: boolean; path: string }>({
    path: `/api/profiles/${encodeURIComponent(name)}`,
    method: 'PATCH',
    body: { new_name: newName }
  })
}

export function deleteProfile(name: string): Promise<{ ok: boolean; path: string }> {
  return window.hermesDesktop.api<{ ok: boolean; path: string }>({
    path: `/api/profiles/${encodeURIComponent(name)}`,
    method: 'DELETE'
  })
}

export function getProfileSoul(name: string): Promise<ProfileSoul> {
  return window.hermesDesktop.api<ProfileSoul>({
    path: `/api/profiles/${encodeURIComponent(name)}/soul`
  })
}

export function updateProfileSoul(name: string, content: string): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    path: `/api/profiles/${encodeURIComponent(name)}/soul`,
    method: 'PUT',
    body: { content }
  })
}

export function getProfileSetupCommand(name: string): Promise<ProfileSetupCommand> {
  return window.hermesDesktop.api<ProfileSetupCommand>({
    path: `/api/profiles/${encodeURIComponent(name)}/setup-command`
  })
}

/** Export a profile to a shareable .tar.gz on the backend's filesystem.
 *  `extraFiles` stages extra root-level files (desktop.json — the appearance/
 *  interface overlay) into the archive alongside the profile's own artifacts. */
export function exportProfileArchive(
  name: string,
  opts: { extraFiles?: Record<string, string>; output?: string } = {}
): Promise<{ archive: string; ok: boolean }> {
  return window.hermesDesktop.api<{ archive: string; ok: boolean }>({
    path: `/api/profiles/${encodeURIComponent(name)}/export`,
    method: 'POST',
    body: { extra_files: opts.extraFiles ?? {}, output: opts.output ?? '' },
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

/** Import a profile .tar.gz as a new profile. Returns the bundled desktop
 *  appearance overlay too (when the archive carried one) so the caller can
 *  apply theme/layout without another round-trip. */
export function importProfileArchive(
  archive: string,
  name?: string
): Promise<{ desktop: null | ProfileDesktopOverlay; name: string; ok: boolean; path: string }> {
  return window.hermesDesktop.api<{ desktop: null | ProfileDesktopOverlay; name: string; ok: boolean; path: string }>({
    path: '/api/profiles/import',
    method: 'POST',
    body: { archive, name: name || null },
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export function getUsageAnalytics(days = 30): Promise<AnalyticsResponse> {
  return window.hermesDesktop.api<AnalyticsResponse>({
    ...profileScoped(),
    path: `/api/analytics/usage?days=${Math.max(1, Math.floor(days))}`
  })
}

export function getGlobalModelOptions(opts?: {
  refresh?: boolean
  includeUnconfigured?: boolean
  explicitOnly?: boolean
}): Promise<ModelOptionsResponse> {
  const params = new URLSearchParams()

  if (opts?.refresh) {
    params.set('refresh', '1')
  }

  if (opts?.includeUnconfigured) {
    params.set('include_unconfigured', '1')
  }

  if (opts?.explicitOnly !== false) {
    params.set('explicit_only', '1')
  }

  return window.hermesDesktop.api<ModelOptionsResponse>({
    ...profileScoped(),
    path: params.size > 0 ? `/api/model/options?${params.toString()}` : '/api/model/options',
    timeoutMs: STARTUP_REQUEST_TIMEOUT_MS
  })
}

export interface RecommendedDefaultModel {
  provider: string
  model: string
  /** True/false for Nous (free vs paid tier); null for other providers. */
  free_tier: boolean | null
}

// Recommended default model for a freshly-authenticated provider. Mirrors the
// curation `hermes model` does — for Nous it honors the free/paid tier so a
// free user gets a free model instead of a paid default.
export function getRecommendedDefaultModel(provider: string): Promise<RecommendedDefaultModel> {
  return window.hermesDesktop.api<RecommendedDefaultModel>({
    ...profileScoped(),
    path: `/api/model/recommended-default?provider=${encodeURIComponent(provider)}`
  })
}

export function setGlobalModel(
  provider: string,
  model: string
): Promise<{ ok: boolean; provider: string; model: string }> {
  return window.hermesDesktop.api<{ ok: boolean; provider: string; model: string }>({
    ...profileScoped(),
    path: '/api/model/set',
    method: 'POST',
    body: {
      scope: 'main',
      provider,
      model
    }
  })
}

export function getAuxiliaryModels(): Promise<AuxiliaryModelsResponse> {
  return window.hermesDesktop.api<AuxiliaryModelsResponse>({
    ...profileScoped(),
    path: '/api/model/auxiliary'
  })
}

export function getMoaModels(): Promise<MoaConfigResponse> {
  return window.hermesDesktop.api<MoaConfigResponse>({
    ...profileScoped(),
    path: '/api/model/moa'
  })
}

export function saveMoaModels(body: MoaConfigResponse): Promise<MoaConfigResponse & { ok: boolean }> {
  return window.hermesDesktop.api<MoaConfigResponse & { ok: boolean }>({
    ...profileScoped(),
    path: '/api/model/moa',
    method: 'PUT',
    body
  })
}

export function setModelAssignment(body: ModelAssignmentRequest): Promise<ModelAssignmentResponse> {
  return window.hermesDesktop.api<ModelAssignmentResponse>({
    ...profileScoped(),
    path: '/api/model/set',
    method: 'POST',
    body
  })
}

export function restartGateway(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/gateway/restart',
    method: 'POST'
  })
}

export function updateHermes(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/hermes/update',
    method: 'POST'
  })
}

/** Query the connected backend's own update state. In remote mode this is the
 *  authoritative source for the backend's behind-count + "what's changed",
 *  distinct from the Electron client clone's git state. */
export function checkHermesUpdate(force = false): Promise<BackendUpdateCheckResponse> {
  return window.hermesDesktop.api<BackendUpdateCheckResponse>({
    ...profileScoped(),
    path: `/api/hermes/update/check${force ? '?force=true' : ''}`
  })
}

export function getActionStatus(name: string, lines = 200): Promise<ActionStatusResponse> {
  return window.hermesDesktop.api<ActionStatusResponse>({
    ...profileScoped(),
    path: `/api/actions/${encodeURIComponent(name)}/status?lines=${Math.max(1, lines)}`
  })
}

export function transcribeAudio(dataUrl: string, mimeType?: string): Promise<AudioTranscriptionResponse> {
  return window.hermesDesktop.api<AudioTranscriptionResponse>({
    path: '/api/audio/transcribe',
    method: 'POST',
    ...profileScoped(),
    body: {
      data_url: dataUrl,
      mime_type: mimeType
    },
    // Transcription blocks until provider STT, file handling, and response
    // encoding finish. Remote providers and long clips regularly exceed the
    // default 15s Electron backend timeout.
    timeoutMs: audioTranscribeRequestTimeoutMs(dataUrl)
  })
}

export function speakText(text: string): Promise<AudioSpeakResponse> {
  return window.hermesDesktop.api<AudioSpeakResponse>({
    ...profileScoped(),
    path: '/api/audio/speak',
    method: 'POST',
    body: { text },
    // TTS blocks until provider synthesis, file read, and base64 encoding
    // finish. Remote providers and large messages regularly exceed the
    // default 15s Electron backend timeout.
    timeoutMs: audioSpeakRequestTimeoutMs(text)
  })
}

export function getElevenLabsVoices(): Promise<ElevenLabsVoicesResponse> {
  return window.hermesDesktop.api<ElevenLabsVoicesResponse>({
    path: '/api/audio/elevenlabs/voices',
    ...profileScoped()
  })
}

// ---------------------------------------------------------------------------
// Skills hub — search / preview / scan / install (parity with `hermes skills`
// and the dashboard's Browse-hub tab). Installs spawn background actions whose
// logs are tailed via getActionStatus().
// ---------------------------------------------------------------------------

const HUB_REQUEST_TIMEOUT_MS = 45_000

export function getSkillHubSources(): Promise<SkillHubSourcesResponse> {
  return window.hermesDesktop.api<SkillHubSourcesResponse>({
    ...profileScoped(),
    path: '/api/skills/hub/sources',
    timeoutMs: HUB_REQUEST_TIMEOUT_MS
  })
}

export function searchSkillsHub(query: string, source = 'all', limit = 20): Promise<SkillHubSearchResponse> {
  const params = new URLSearchParams({ q: query, source, limit: String(limit) })

  return window.hermesDesktop.api<SkillHubSearchResponse>({
    ...profileScoped(),
    path: `/api/skills/hub/search?${params.toString()}`,
    timeoutMs: HUB_REQUEST_TIMEOUT_MS
  })
}

export function previewSkillHub(identifier: string): Promise<SkillHubPreview> {
  return window.hermesDesktop.api<SkillHubPreview>({
    ...profileScoped(),
    path: `/api/skills/hub/preview?identifier=${encodeURIComponent(identifier)}`,
    timeoutMs: HUB_REQUEST_TIMEOUT_MS
  })
}

export function scanSkillHub(identifier: string): Promise<SkillHubScanResult> {
  return window.hermesDesktop.api<SkillHubScanResult>({
    ...profileScoped(),
    path: `/api/skills/hub/scan?identifier=${encodeURIComponent(identifier)}`,
    timeoutMs: HUB_REQUEST_TIMEOUT_MS
  })
}

export function installSkillFromHub(identifier: string): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/skills/hub/install',
    method: 'POST',
    body: { identifier }
  })
}

export function uninstallSkillFromHub(name: string): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/skills/hub/uninstall',
    method: 'POST',
    body: { name }
  })
}

export function updateSkillsFromHub(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/skills/hub/update',
    method: 'POST',
    body: {}
  })
}

// ---------------------------------------------------------------------------
// MCP servers — structured list / test / enable toggle / catalog (parity with
// `hermes mcp` and the dashboard MCP page). Raw JSON editing stays in
// config.yaml via saveHermesConfig.
// ---------------------------------------------------------------------------

export function listMcpServers(): Promise<{ servers: McpServerSummary[] }> {
  return window.hermesDesktop.api<{ servers: McpServerSummary[] }>({
    ...profileScoped(),
    path: '/api/mcp/servers'
  })
}

export function setMcpServerEnabled(name: string, enabled: boolean): Promise<{ ok: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean }>({
    ...profileScoped(),
    path: `/api/mcp/servers/${encodeURIComponent(name)}/enabled`,
    method: 'PUT',
    body: { enabled }
  })
}

export function getMcpCatalog(): Promise<McpCatalogResponse> {
  return window.hermesDesktop.api<McpCatalogResponse>({
    ...profileScoped(),
    path: '/api/mcp/catalog'
  })
}

export function installMcpCatalogEntry(
  name: string,
  env: Record<string, string> = {}
): Promise<{ ok: boolean; name?: string; pid?: number; action?: string; background?: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean; name?: string; pid?: number; action?: string; background?: boolean }>({
    ...profileScoped(),
    path: '/api/mcp/catalog/install',
    method: 'POST',
    body: { name, env, enable: true },
    timeoutMs: 60_000
  })
}

// ---------------------------------------------------------------------------
// Memory data + curator (parity with `hermes memory` / `hermes curator`).
// ---------------------------------------------------------------------------

export function getMemoryStatus(): Promise<MemoryStatusResponse> {
  return window.hermesDesktop.api<MemoryStatusResponse>({
    ...profileScoped(),
    path: '/api/memory'
  })
}

export function resetMemory(target: 'all' | 'memory' | 'user'): Promise<{ ok: boolean; deleted: string[] }> {
  return window.hermesDesktop.api<{ ok: boolean; deleted: string[] }>({
    ...profileScoped(),
    path: '/api/memory/reset',
    method: 'POST',
    body: { target }
  })
}

export function getCuratorStatus(): Promise<CuratorStatusResponse> {
  return window.hermesDesktop.api<CuratorStatusResponse>({
    ...profileScoped(),
    path: '/api/curator'
  })
}

export function setCuratorPaused(paused: boolean): Promise<{ ok: boolean; paused: boolean }> {
  return window.hermesDesktop.api<{ ok: boolean; paused: boolean }>({
    ...profileScoped(),
    path: '/api/curator/paused',
    method: 'PUT',
    body: { paused }
  })
}

export function runCurator(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({
    ...profileScoped(),
    path: '/api/curator/run',
    method: 'POST',
    body: {}
  })
}

// ---------------------------------------------------------------------------
// Maintenance operations (parity with `hermes doctor` / `hermes security
// audit` / `hermes backup` / `hermes debug share` and the dashboard System
// page). All except debug share are spawn-based background actions tailed via
// getActionStatus().
// ---------------------------------------------------------------------------

export function runDoctor(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({ path: '/api/ops/doctor', method: 'POST', body: {} })
}

export function runSecurityAudit(): Promise<ActionResponse> {
  return window.hermesDesktop.api<ActionResponse>({ path: '/api/ops/security-audit', method: 'POST', body: {} })
}

export function runBackup(): Promise<ActionResponse & { archive?: string }> {
  return window.hermesDesktop.api<ActionResponse & { archive?: string }>({
    path: '/api/ops/backup',
    method: 'POST',
    body: {}
  })
}

export function runDebugShare(): Promise<DebugShareResponse> {
  return window.hermesDesktop.api<DebugShareResponse>({
    path: '/api/ops/debug-share',
    method: 'POST',
    body: {},
    // Synchronous upload of report + logs to the paste service.
    timeoutMs: 120_000
  })
}

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
