import { buildHermesWebSocketUrl } from "@hermes/shared";

// The dashboard can be served either at the root of its host (e.g.
// https://kanban.tilos.com/) or under a URL prefix when reverse-proxied
// (e.g. https://mission-control.tilos.com/hermes/). The Python backend
// injects ``window.__HERMES_BASE_PATH__`` into index.html based on the
// incoming ``X-Forwarded-Prefix`` header so the SPA can address its own
// ``/api/...`` and ``/dashboard-plugins/...`` URLs correctly without a
// rebuild. Empty string means "served at root".
function readBasePath(): string {
  if (typeof window === "undefined") return "";
  const raw = window.__HERMES_BASE_PATH__ ?? "";
  if (!raw) return "";
  // Normalise: ensure leading slash, strip trailing slash.
  const withLead = raw.startsWith("/") ? raw : `/${raw}`;
  return withLead.replace(/\/+$/, "");
}

export const HERMES_BASE_PATH = readBasePath();
const BASE = HERMES_BASE_PATH;

import type { DashboardTheme } from "@/themes/types";

// Ephemeral session token for protected endpoints.
// Injected into index.html by the server — never fetched via API.
declare global {
  interface Window {
    __HERMES_SESSION_TOKEN__?: string;
    __HERMES_BASE_PATH__?: string;
    /** Server-injected flag: ``true`` when the dashboard's OAuth gate is
     * engaged (public bind, no ``--insecure``). Toggles the SPA's
     * WS-upgrade path from legacy ``?token=`` to single-use ``?ticket=``
     * fetched via :func:`getWsTicket`. */
    __HERMES_AUTH_REQUIRED__?: boolean;
  }
}
const SESSION_HEADER = "X-Hermes-Session-Token";

function setSessionHeader(headers: Headers, token: string): void {
  if (!headers.has(SESSION_HEADER)) {
    headers.set(SESSION_HEADER, token);
  }
}

// ── Global management-profile scope ──────────────────────────────────
// The dashboard is a machine-level management surface: one header switcher
// (ProfileProvider in App.tsx) decides which profile the management pages
// read/write, and fetchJSON transparently appends ?profile=<name> to the
// profile-scoped endpoint families below. "" = the dashboard process's own
// profile (legacy behavior). Calls that already carry an explicit profile
// (e.g. ProfileBuilder writes) are left untouched — explicit beats global.
let _managementProfile = "";

export function setManagementProfile(name: string): void {
  _managementProfile = (name || "").trim();
}

export function getManagementProfile(): string {
  return _managementProfile;
}

// Endpoint families that honor ?profile= on the backend (web_server.py
// _profile_scope or explicit per-profile DB opens). Anything else — ops,
// pairing, cron (which has its own per-job profile params), profiles
// themselves — is machine-global or self-scoped and must NOT be rewritten.
const PROFILE_SCOPED_PREFIXES = [
  "/api/status",
  "/api/gateway",
  "/api/analytics",
  "/api/skills",
  "/api/tools/toolsets",
  "/api/config",
  "/api/env",
  "/api/mcp",
  "/api/messaging/platforms",
  "/api/messaging/telegram/onboarding",
  "/api/messaging/whatsapp/onboarding",
  "/api/model/info",
  "/api/model/set",
  "/api/model/auxiliary",
  "/api/model/moa",
  "/api/model/options",
];

function withManagementProfile(url: string): string {
  if (!_managementProfile) return url;
  if (url.includes("profile=")) return url; // explicit param wins
  const path = url.split("?")[0];
  if (!PROFILE_SCOPED_PREFIXES.some((p) => path.startsWith(p))) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}profile=${encodeURIComponent(_managementProfile)}`;
}

export async function fetchJSON<T>(
  url: string,
  init?: RequestInit,
  options?: FetchJSONOptions,
): Promise<T> {
  url = withManagementProfile(url);
  // Inject the session token into all /api/ requests.
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  const res = await fetch(`${BASE}${url}`, {
    ...init,
    headers,
    // ``credentials: 'include'`` so the cookie-auth path (gated mode) works
    // for any fetch routed through here. Loopback mode is unaffected — the
    // server doesn't read cookies and the legacy session-token header is
    // already attached above.
    credentials: init?.credentials ?? "include",
  });
  if (res.status === 401) {
    // Phase 6: the gated middleware emits a structured envelope so the
    // SPA can full-page-navigate to /login on session expiry. Parse it,
    // and only redirect on the known error codes — domain-level 401s
    // (e.g. "you don't have permission to read this monitor") bubble
    // up as regular errors so callers can handle them.
    let body: { error?: string; login_url?: string } = {};
    try {
      body = await res.clone().json();
    } catch {
      /* non-JSON 401 — let it fall through */
    }
    if (
      (body.error === "unauthenticated" || body.error === "session_expired") &&
      body.login_url
    ) {
      // Preserve where the user was so /auth/callback can land them back
      // after re-auth. The gate's login_url already carries a ``next=``
      // built from the request path, but the SPA may be deep inside a
      // SPA route the gate never saw — e.g. a hash route or a client-side
      // /sessions/<id> deep link. Save the current location as a
      // fallback the post-login handler can read.
      try {
        sessionStorage.setItem(
          "hermes.lastLocation",
          window.location.pathname + window.location.search,
        );
      } catch {
        /* SSR / privacy mode — ignore */
      }
      window.location.assign(body.login_url);
      // Never resolve — the page is about to unload.
      return new Promise<T>(() => {});
    }
    // Loopback mode: ``_SESSION_TOKEN`` rotates on every server restart
    // (``hermes update``, ``hermes gateway restart``, etc.). A tab kept
    // open across the restart holds the OLD token in
    // ``window.__HERMES_SESSION_TOKEN__`` from the previous HTML render,
    // so every fetch returns 401. The HTML is served ``Cache-Control:
    // no-store`` so a reload picks up the freshly-injected token. Trigger
    // that reload once on the first stale-token 401 — gated mode is
    // handled above, so reaching here in gated mode means a real
    // middleware failure that should not reload-loop.
    if (!window.__HERMES_AUTH_REQUIRED__ && !options?.allowUnauthorized) {
      let alreadyReloaded = false;
      try {
        alreadyReloaded =
          sessionStorage.getItem("hermes.tokenReloadAttempted") === "1";
      } catch {
        /* SSR / privacy mode — fall through to throw */
      }
      if (!alreadyReloaded) {
        try {
          sessionStorage.setItem("hermes.tokenReloadAttempted", "1");
        } catch {
          /* SSR / privacy mode — best effort */
        }
        window.location.reload();
        return new Promise<T>(() => {});
      }
    }
  }
  if (res.ok) {
    // Clear the stale-token reload guard: a successful 2xx proves the
    // current ``window.__HERMES_SESSION_TOKEN__`` is valid, so the next
    // 401 — if any — should be allowed to trigger its own reload cycle.
    try {
      sessionStorage.removeItem("hermes.tokenReloadAttempted");
    } catch {
      /* SSR / privacy mode — ignore */
    }
  }
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`${res.status}: ${text}`);
  }
  return res.json();
}

/** Encode a plugin registry key for URL paths (preserves `/` segment separators). */
function pluginPath(name: string): string {
  return name.split("/").map(encodeURIComponent).join("/");
}

/**
 * Fetch a single-use ticket for a WebSocket upgrade in gated mode.
 *
 * The dashboard's gated-mode WS auth (``hermes_cli.web_server._ws_auth_ok``)
 * rejects the legacy ``?token=<_SESSION_TOKEN>`` path and only accepts
 * ``?ticket=<minted>`` consumed against the in-memory ticket store. Browsers
 * can't set ``Authorization`` on a WS upgrade, so this round-trip via the
 * authenticated REST endpoint is the bridge from cookie auth to WS auth.
 *
 * Tickets are single-use and TTL=30s — every WS connect attempt must
 * fetch a fresh ticket.
 */
export async function getWsTicket(): Promise<{ ticket: string; ttl_seconds: number }> {
  const res = await fetch(`${BASE}/api/auth/ws-ticket`, {
    method: "POST",
    credentials: "include",
  });
  if (!res.ok) {
    throw new Error(`/api/auth/ws-ticket: HTTP ${res.status}`);
  }
  return res.json();
}

/**
 * Resolve the auth query-param pair (``[name, value]``) for a WebSocket
 * connect. In gated mode mints a fresh single-use ticket; in loopback
 * mode returns the injected session token.
 */
export async function buildWsAuthParam(): Promise<[string, string]> {
  if (window.__HERMES_AUTH_REQUIRED__) {
    const { ticket } = await getWsTicket();
    return ["ticket", ticket];
  }
  const token = window.__HERMES_SESSION_TOKEN__ ?? "";
  return ["token", token];
}

/**
 * Authenticated ``fetch`` for dashboard ``/api/...`` requests that aren't
 * plain JSON — file uploads (``FormData``), binary downloads (blobs), etc.
 * Mirrors ``fetchJSON``'s auth handling but returns the raw ``Response`` so
 * the caller can read ``.blob()`` / ``.formData()`` / stream it.
 *
 * Auth, in both modes, exactly as ``fetchJSON`` does it:
 *  - loopback / ``--insecure``: attach the ``X-Hermes-Session-Token`` header.
 *  - gated OAuth: no token header (it's absent by design); the
 *    ``hermes_session_at`` cookie rides along via ``credentials: 'include'``.
 *
 * Unlike ``fetchJSON`` this does NOT parse the body, does NOT throw on
 * non-2xx (the caller decides — a 404 on a download is meaningful), and
 * does NOT run the global 401 → /login redirect (binary endpoints aren't
 * navigation targets). Callers that want the redirect behaviour should use
 * ``fetchJSON``.
 */
export async function authedFetch(
  url: string,
  init?: RequestInit,
): Promise<Response> {
  const headers = new Headers(init?.headers);
  const token = window.__HERMES_SESSION_TOKEN__;
  if (token) {
    setSessionHeader(headers, token);
  }
  return fetch(`${BASE}${url}`, {
    ...init,
    headers,
    credentials: init?.credentials ?? "include",
  });
}

/**
 * Build an absolute ``ws(s)://`` URL for a dashboard WebSocket endpoint,
 * with the correct auth query param appended for the active mode (fresh
 * single-use ``ticket`` in gated mode, ``token`` in loopback). Plugins and
 * the SPA should use this instead of hand-assembling a WS URL + reading
 * ``window.__HERMES_SESSION_TOKEN__`` directly, so the gated-mode ticket
 * path can never be forgotten.
 *
 * ``path`` is the dashboard-relative path (e.g.
 * ``"/api/plugins/kanban/events"``); the base-path prefix and host are
 * applied here. Extra query params can be supplied via ``params`` and are
 * merged before the auth param.
 */
export async function buildWsUrl(
  path: string,
  params?: Record<string, string>,
): Promise<string> {
  return buildHermesWebSocketUrl({
    authParam: await buildWsAuthParam(),
    basePath: BASE,
    params,
    path,
  });
}

/** Build a ``?profile=<name>`` query suffix, or "" when unset.
 *
 * Used by the skills/toolsets endpoints so the dashboard can manage a
 * profile other than the one the server process runs under. */
function profileQuery(profile?: string): string {
  return profile ? `?profile=${encodeURIComponent(profile)}` : "";
}

function appendProfileParam(url: string, profile?: string): string {
  if (!profile || url.includes("profile=")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}profile=${encodeURIComponent(profile)}`;
}

export const api = {
  buildWsUrl,
  getStatus: () => fetchJSON<StatusResponse>("/api/status"),
  /**
   * Identity probe for the dashboard auth gate (Phase 7).
   *
   * Returns the verified Session as JSON when gated mode is active and a
   * valid cookie is attached. Loopback mode is unaffected — the endpoint
   * still exists but is never useful there (no Session, no cookie). The
   * AuthWidget component swallows 401s from this call: if the gate isn't
   * engaged, /api/auth/me returns 401 and the widget renders nothing.
   *
   * ``allowUnauthorized`` is load-bearing: in loopback mode this endpoint
   * 401s by design, and fetchJSON's default loopback behaviour treats a
   * 401 as a rotated session token and full-page-reloads to pick up a
   * fresh one. Because every *other* dashboard request succeeds (and so
   * clears the one-shot reload guard), that turns this expected 401 into
   * an infinite reload loop. Opting out keeps the 401 a plain throw the
   * widget can catch.
   */
  getAuthMe: () =>
    fetchJSON<AuthMeResponse>("/api/auth/me", undefined, {
      allowUnauthorized: true,
    }),
  logout: () =>
    fetch(`${BASE}/auth/logout`, {
      method: "POST",
      credentials: "include",
    }).then((r) => {
      // /auth/logout returns 302 → /login. Follow that with a full-page
      // navigation rather than letting fetch() opaquely consume the
      // redirect — the SPA needs to leave the protected area.
      window.location.assign("/login");
      return r;
    }),
  getSessions: (
    limit = 20,
    offset = 0,
    profile = getManagementProfile(),
    order: "created" | "recent" = "created",
  ) =>
    fetchJSON<PaginatedSessions>(
      appendProfileParam(
        `/api/sessions?limit=${limit}&offset=${offset}&order=${order}`,
        profile,
      ),
    ),
  getSessionMessages: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionMessagesResponse>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}/messages`, profile),
    ),
  getSessionDetail: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionInfo>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}`, profile),
    ),
  getSessionLatestDescendant: (id: string, profile = getManagementProfile()) =>
    fetchJSON<SessionLatestDescendantResponse>(
      appendProfileParam(
        `/api/sessions/${encodeURIComponent(id)}/latest-descendant`,
        profile,
      ),
    ),
  deleteSession: (id: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(
      appendProfileParam(`/api/sessions/${encodeURIComponent(id)}`, profile),
      {
        method: "DELETE",
      },
    ),
  getEmptySessionsCount: (profile = getManagementProfile()) =>
    fetchJSON<{ count: number }>(
      appendProfileParam("/api/sessions/empty/count", profile),
    ),
  deleteEmptySessions: (profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; deleted: number }>(
      appendProfileParam("/api/sessions/empty", profile),
      {
        method: "DELETE",
      },
    ),
  bulkDeleteSessions: (ids: string[], profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; deleted: number }>("/api/sessions/bulk-delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids, profile: profile || undefined }),
    }),
  renameSession: (id: string, title: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean; title: string }>(
      `/api/sessions/${encodeURIComponent(id)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title, profile: profile || undefined }),
      },
    ),
  getSessionStats: (profile = getManagementProfile()) =>
    fetchJSON<SessionStoreStats>(appendProfileParam("/api/sessions/stats", profile)),
  exportSessionUrl: (id: string, profile = getManagementProfile()) =>
    appendProfileParam(`/api/sessions/${encodeURIComponent(id)}/export`, profile),
  importSessions: (
    sessions: Array<Record<string, unknown>>,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<SessionImportResponse>("/api/sessions/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessions, profile: profile || undefined }),
    }),
  pruneSessions: (
    older_than_days: number,
    source?: string,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<{ ok: boolean; removed: number }>("/api/sessions/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ older_than_days, source, profile: profile || undefined }),
    }),
  listFiles: (path?: string) => {
    const query = path ? `?path=${encodeURIComponent(path)}` : "";
    return fetchJSON<ManagedFilesResponse>(`/api/files${query}`);
  },
  readFile: (path: string) =>
    fetchJSON<ManagedFileReadResponse>(
      `/api/files/read?path=${encodeURIComponent(path)}`,
    ),
  uploadFile: (path: string, file: File, overwrite = true) => {
    // Stream the raw bytes as multipart/form-data. Do NOT set Content-Type —
    // the browser adds the multipart boundary automatically. Sending the file
    // as base64 JSON (the old path) inflated the body ~33%, buffered the whole
    // file in memory, and 502'd on large backup archives behind the proxy
    // (NS-501).
    const form = new FormData();
    form.append("path", path);
    form.append("overwrite", String(overwrite));
    form.append("file", file, file.name);
    return fetchJSON<ManagedFileWriteResponse>("/api/files/upload-stream", {
      method: "POST",
      body: form,
    });
  },
  createDirectory: (path: string) =>
    fetchJSON<ManagedFileWriteResponse>("/api/files/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    }),
  deleteFile: (path: string, recursive = false) =>
    fetchJSON<{ ok: boolean; path: string }>("/api/files", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, recursive }),
    }),
  getLogs: (params: { file?: string; lines?: number; level?: string; component?: string }) => {
    const qs = new URLSearchParams();
    if (params.file) qs.set("file", params.file);
    if (params.lines) qs.set("lines", String(params.lines));
    if (params.level && params.level !== "ALL") qs.set("level", params.level);
    if (params.component && params.component !== "all") qs.set("component", params.component);
    return fetchJSON<LogsResponse>(`/api/logs?${qs.toString()}`);
  },
  getAnalytics: (days: number, profile = getManagementProfile()) =>
    fetchJSON<AnalyticsResponse>(
      appendProfileParam(`/api/analytics/usage?days=${days}`, profile),
    ),
  getModelsAnalytics: (days: number, profile = getManagementProfile()) =>
    fetchJSON<ModelsAnalyticsResponse>(
      appendProfileParam(`/api/analytics/models?days=${days}`, profile),
    ),
  getConfig: (profile = getManagementProfile()) =>
    fetchJSON<Record<string, unknown>>(appendProfileParam("/api/config", profile)),
  getDefaults: () => fetchJSON<Record<string, unknown>>("/api/config/defaults"),
  getSchema: () => fetchJSON<{ fields: Record<string, unknown>; category_order: string[] }>("/api/config/schema"),
  getModelInfo: (profile = getManagementProfile()) =>
    fetchJSON<ModelInfoResponse>(appendProfileParam("/api/model/info", profile)),
  getModelOptions: (
    profileOrOptions?: string | { profile?: string; refresh?: boolean },
  ) => {
    const profile =
      typeof profileOrOptions === "string"
        ? profileOrOptions
        : profileOrOptions?.profile;
    const refresh =
      typeof profileOrOptions === "object" && !!profileOrOptions.refresh;
    const qs = new URLSearchParams();
    if (profile) qs.set("profile", profile);
    if (refresh) qs.set("refresh", "1");
    // Dashboard surfaces (Models page, profile builder, cron) are
    // management/setup UIs: keep the full provider universe with setup
    // affordances. The endpoint now defaults to the configured subset for
    // desktop chat pickers (#56974), so opt in explicitly here.
    qs.set("include_unconfigured", "1");
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return fetchJSON<ModelOptionsResponse>(`/api/model/options${suffix}`);
  },
  getAuxiliaryModels: (profile = getManagementProfile()) =>
    fetchJSON<AuxiliaryModelsResponse>(
      appendProfileParam("/api/model/auxiliary", profile),
    ),
  getMoaModels: () => fetchJSON<MoaConfigResponse>("/api/model/moa"),
  saveMoaModels: (body: MoaConfigResponse) =>
    fetchJSON<MoaConfigResponse & { ok: boolean }>("/api/model/moa", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  setModelAssignment: (
    body: ModelAssignmentRequest,
    profile = getManagementProfile(),
  ) =>
    fetchJSON<ModelAssignmentResponse>(
      appendProfileParam("/api/model/set", profile),
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  saveConfig: (config: Record<string, unknown>, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(appendProfileParam("/api/config", profile), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ config }),
    }),
  getConfigRaw: (profile = getManagementProfile()) =>
    fetchJSON<{ yaml: string; path?: string }>(
      appendProfileParam("/api/config/raw", profile),
    ),
  saveConfigRaw: (yaml_text: string, profile = getManagementProfile()) =>
    fetchJSON<{ ok: boolean }>(appendProfileParam("/api/config/raw", profile), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ yaml_text }),
    }),
  getEnvVars: () => fetchJSON<Record<string, EnvVarInfo>>("/api/env"),
  setEnvVar: (key: string, value: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key, value }),
    }),
  deleteEnvVar: (key: string) =>
    fetchJSON<{ ok: boolean }>("/api/env", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),
  revealEnvVar: (key: string) =>
    fetchJSON<{ key: string; value: string }>("/api/env/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key }),
    }),

  // Cron jobs
  getCronJobs: (profile = "all") =>
    fetchJSON<CronJob[]>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`),
  getCronDeliveryTargets: () =>
    fetchJSON<{ targets: CronDeliveryTarget[] }>("/api/cron/delivery-targets"),
  createCronJob: (job: CronJobMutation, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(job),
    }),
  pauseCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/pause?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  updateCronJob: (
    id: string,
    updates: CronJobMutation,
    profile = "default",
  ) =>
    fetchJSON<CronJob>(
      `/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      },
    ),
  resumeCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/resume?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  triggerCronJob: (id: string, profile = "default") =>
    fetchJSON<CronJob>(`/api/cron/jobs/${encodeURIComponent(id)}/trigger?profile=${encodeURIComponent(profile)}`, { method: "POST" }),
  deleteCronJob: (id: string, profile = "default") =>
    fetchJSON<{ ok: boolean }>(`/api/cron/jobs/${encodeURIComponent(id)}?profile=${encodeURIComponent(profile)}`, { method: "DELETE" }),

  // Automation Blueprints — parameterized automation blueprints
  getAutomationBlueprints: () =>
    fetchJSON<{ blueprints: AutomationBlueprint[] }>("/api/cron/blueprints"),
  instantiateAutomationBlueprint: (
    body: { blueprint: string; values: Record<string, string> },
    profile = "default",
  ) =>
    fetchJSON<CronJob>(`/api/cron/blueprints/instantiate?profile=${encodeURIComponent(profile)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  // Profiles
  getProfiles: () =>
    fetchJSON<{ profiles: ProfileInfo[] }>("/api/profiles"),
  getActiveProfile: () =>
    fetchJSON<ActiveProfileInfo>("/api/profiles/active"),
  setActiveProfile: (name: string) =>
    fetchJSON<{ ok: boolean; active: string }>("/api/profiles/active", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  createProfile: (body: {
    name: string;
    clone_from?: string | null;
    clone_from_default?: boolean;
    clone_all?: boolean;
    no_skills?: boolean;
    description?: string;
    provider?: string;
    model?: string;
    mcp_servers?: McpServerCreate[];
    keep_skills?: string[];
    hub_skills?: string[];
  }) =>
    fetchJSON<{
      ok: boolean;
      name: string;
      path: string;
      model_set?: boolean;
      mcp_written?: number;
      skills_disabled?: number;
      hub_installs?: Array<{ identifier: string; pid: number | null }>;
    }>("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  updateProfileDescription: (name: string, description: string) =>
    fetchJSON<{ ok: boolean; description: string; description_auto: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/description`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ description }),
      },
    ),
  describeProfileAuto: (name: string, overwrite = true) =>
    fetchJSON<ProfileDescribeAutoResult>(
      `/api/profiles/${encodeURIComponent(name)}/describe-auto`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ overwrite }),
      },
    ),
  setProfileModel: (name: string, provider: string, model: string) =>
    fetchJSON<{ ok: boolean; provider: string; model: string }>(
      `/api/profiles/${encodeURIComponent(name)}/model`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, model }),
      },
    ),
  renameProfile: (name: string, newName: string) =>
    fetchJSON<{ ok: boolean; name: string; path: string }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ new_name: newName }),
      },
    ),
  deleteProfile: (name: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}`,
      { method: "DELETE" },
    ),
  getProfileSetupCommand: (name: string) =>
    fetchJSON<{ command: string }>(
      `/api/profiles/${encodeURIComponent(name)}/setup-command`,
    ),
  getProfileSoul: (name: string) =>
    fetchJSON<{ content: string; exists: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
    ),
  updateProfileSoul: (name: string, content: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/profiles/${encodeURIComponent(name)}/soul`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content }),
      },
    ),

  // Skills & Toolsets
  //
  // All calls accept an optional ``profile`` so the Skills page can manage
  // any profile's skills/toolsets — not just the one the dashboard process
  // runs under. Omitted/empty profile = the dashboard's own profile.
  getSkills: (profile?: string) =>
    fetchJSON<SkillInfo[]>(`/api/skills${profileQuery(profile)}`),
  toggleSkill: (name: string, enabled: boolean, profile?: string) =>
    fetchJSON<{ ok: boolean }>("/api/skills/toggle", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled, profile: profile || undefined }),
    }),
  getSkillContent: (name: string, profile?: string) =>
    fetchJSON<SkillContent>(
      `/api/skills/content?name=${encodeURIComponent(name)}${profile ? `&profile=${encodeURIComponent(profile)}` : ""}`,
    ),
  createSkill: (skill: { name: string; content: string; category?: string }, profile?: string) =>
    fetchJSON<SkillWriteResult>("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...skill, profile: profile || undefined }),
    }),
  updateSkillContent: (name: string, content: string, profile?: string) =>
    fetchJSON<SkillWriteResult>("/api/skills/content", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, content, profile: profile || undefined }),
    }),
  getToolsets: (profile?: string) =>
    fetchJSON<ToolsetInfo[]>(`/api/tools/toolsets${profileQuery(profile)}`),
  toggleToolset: (name: string, enabled: boolean, profile?: string) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled, profile: profile || undefined }),
      },
    ),
  getToolsetConfig: (name: string, profile?: string) =>
    fetchJSON<ToolsetConfig>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/config${profileQuery(profile)}`,
    ),
  selectToolsetProvider: (name: string, provider: string, profile?: string) =>
    fetchJSON<{ ok: boolean; name: string; provider: string }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/provider`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, profile: profile || undefined }),
      },
    ),
  saveToolsetEnv: (name: string, env: Record<string, string>, profile?: string) =>
    fetchJSON<ToolsetEnvResult>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/env`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ env, profile: profile || undefined }),
      },
    ),
  runToolsetPostSetup: (name: string, key: string, profile?: string) =>
    fetchJSON<ActionResponse & { key: string }>(
      `/api/tools/toolsets/${encodeURIComponent(name)}/post-setup`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ key, profile: profile || undefined }),
      },
    ),

  // Session search (FTS5)
  searchSessions: (q: string, profile = getManagementProfile()) =>
    fetchJSON<SessionSearchResponse>(
      appendProfileParam(`/api/sessions/search?q=${encodeURIComponent(q)}`, profile),
    ),

  // OAuth provider management
  getOAuthProviders: () =>
    fetchJSON<OAuthProvidersResponse>("/api/providers/oauth"),
  disconnectOAuthProvider: (providerId: string) =>
    fetchJSON<{ ok: boolean; provider: string }>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}`,
      {
        method: "DELETE",
      },
    ),
  startOAuthLogin: (providerId: string) =>
    fetchJSON<OAuthStartResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/start`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      },
    ),
  submitOAuthCode: (providerId: string, sessionId: string, code: string) =>
    fetchJSON<OAuthSubmitResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/submit`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, code }),
      },
    ),
  pollOAuthSession: (providerId: string, sessionId: string) =>
    fetchJSON<OAuthPollResponse>(
      `/api/providers/oauth/${encodeURIComponent(providerId)}/poll/${encodeURIComponent(sessionId)}`,
    ),
  cancelOAuthSession: (sessionId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/providers/oauth/sessions/${encodeURIComponent(sessionId)}`,
      {
        method: "DELETE",
      },
    ),

  // Messaging platforms (gateway channels)
  getMessagingPlatforms: () =>
    fetchJSON<MessagingPlatformsResponse>("/api/messaging/platforms"),
  updateMessagingPlatform: (id: string, body: MessagingPlatformUpdate) =>
    fetchJSON<{ ok: boolean; platform: string }>(
      `/api/messaging/platforms/${encodeURIComponent(id)}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  testMessagingPlatform: (id: string) =>
    fetchJSON<MessagingPlatformTestResult>(
      `/api/messaging/platforms/${encodeURIComponent(id)}/test`,
      { method: "POST" },
    ),
  startTelegramOnboarding: (body: { bot_name?: string }) =>
    fetchJSON<TelegramOnboardingStartResponse>(
      "/api/messaging/telegram/onboarding/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getTelegramOnboardingStatus: (pairingId: string) =>
    fetchJSON<TelegramOnboardingStatusResponse>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}`,
    ),
  applyTelegramOnboarding: (
    pairingId: string,
    body: { allowed_user_ids: string[]; profile?: string },
  ) =>
    fetchJSON<TelegramOnboardingApplyResponse>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  cancelTelegramOnboarding: (pairingId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/messaging/telegram/onboarding/${encodeURIComponent(pairingId)}`,
      { method: "DELETE" },
    ),
  startWhatsAppOnboarding: (body: {
    mode?: "bot" | "self-chat";
    allowed_users?: string;
  }) =>
    fetchJSON<WhatsAppOnboardingStartResponse>(
      "/api/messaging/whatsapp/onboarding/start",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  getWhatsAppOnboardingStatus: (pairingId: string) =>
    fetchJSON<WhatsAppOnboardingStatusResponse>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}`,
    ),
  applyWhatsAppOnboarding: (
    pairingId: string,
    body: { mode?: "bot" | "self-chat"; allowed_users?: string; profile?: string },
  ) =>
    fetchJSON<WhatsAppOnboardingApplyResponse>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}/apply`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  cancelWhatsAppOnboarding: (pairingId: string) =>
    fetchJSON<{ ok: boolean }>(
      `/api/messaging/whatsapp/onboarding/${encodeURIComponent(pairingId)}`,
      { method: "DELETE" },
    ),

  // Gateway / update actions
  restartGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/restart", { method: "POST" }),
  updateHermes: () =>
    fetchJSON<ActionResponse>("/api/hermes/update", { method: "POST" }),
  checkHermesUpdate: (force = false) =>
    fetchJSON<UpdateCheckResponse>(
      `/api/hermes/update/check${force ? "?force=true" : ""}`,
    ),
  getActionStatus: (name: string, lines = 200) =>
    fetchJSON<ActionStatusResponse>(
      `/api/actions/${encodeURIComponent(name)}/status?lines=${lines}`,
    ),

  // Dashboard plugins
  getPlugins: () =>
    fetchJSON<PluginManifestResponse[]>("/api/dashboard/plugins"),
  rescanPlugins: () =>
    fetchJSON<{ ok: boolean; count: number }>("/api/dashboard/plugins/rescan"),

  getPluginsHub: () => fetchJSON<PluginsHubResponse>("/api/dashboard/plugins/hub"),

  installAgentPlugin: (body: AgentPluginInstallRequest) =>
    fetchJSON<AgentPluginInstallResponse>("/api/dashboard/agent-plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body }),
    }),

  enableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/enable`,
      { method: "POST" },
    ),

  disableAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string; unchanged?: boolean }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/disable`,
      { method: "POST" },
    ),

  updateAgentPlugin: (name: string) =>
    fetchJSON<AgentPluginUpdateResponse>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}/update`,
      { method: "POST" },
    ),

  removeAgentPlugin: (name: string) =>
    fetchJSON<{ ok: boolean; name: string }>(
      `/api/dashboard/agent-plugins/${pluginPath(name)}`,
      { method: "DELETE" },
    ),

  savePluginProviders: (body: PluginProvidersPutRequest) =>
    fetchJSON<{ ok: boolean }>("/api/dashboard/plugin-providers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),

  setPluginVisibility: (name: string, hidden: boolean) =>
    fetchJSON<{ ok: boolean; name: string; hidden: boolean }>(
      `/api/dashboard/plugins/${pluginPath(name)}/visibility`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hidden }),
      },
    ),

  // Dashboard themes
  getThemes: () =>
    fetchJSON<DashboardThemesResponse>("/api/dashboard/themes"),
  setTheme: (name: string) =>
    fetchJSON<{ ok: boolean; theme: string }>("/api/dashboard/theme", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    }),
  getFontPref: () =>
    fetchJSON<DashboardFontResponse>("/api/dashboard/font"),
  setFontPref: (font: string) =>
    fetchJSON<{ ok: boolean; font: string }>("/api/dashboard/font", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ font }),
    }),

  // ── Admin: MCP servers ──────────────────────────────────────────────
  getMcpServers: () => fetchJSON<{ servers: McpServer[] }>("/api/mcp/servers"),
  addMcpServer: (body: McpServerCreate) =>
    fetchJSON<McpServer>("/api/mcp/servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  removeMcpServer: (name: string) =>
    fetchJSON<{ ok: boolean }>(`/api/mcp/servers/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  testMcpServer: (name: string) =>
    fetchJSON<McpTestResult>(
      `/api/mcp/servers/${encodeURIComponent(name)}/test`,
      { method: "POST" },
    ),
  setMcpServerEnabled: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/mcp/servers/${encodeURIComponent(name)}/enabled`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    ),
  getMcpCatalog: () =>
    fetchJSON<{ entries: McpCatalogEntry[]; diagnostics: McpCatalogDiagnostic[] }>(
      "/api/mcp/catalog",
    ),
  installMcpCatalogEntry: (
    name: string,
    env: Record<string, string> = {},
    enable = true,
  ) =>
    fetchJSON<{ ok: boolean; name: string; background: boolean; action?: string }>(
      "/api/mcp/catalog/install",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, env, enable }),
      },
    ),

  // ── Admin: Pairing ──────────────────────────────────────────────────
  getPairing: () => fetchJSON<PairingResponse>("/api/pairing"),
  approvePairing: (platform: string, code: string) =>
    fetchJSON<{ ok: boolean; user: PairingUser }>("/api/pairing/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, code }),
    }),
  revokePairing: (platform: string, user_id: string) =>
    fetchJSON<{ ok: boolean }>("/api/pairing/revoke", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ platform, user_id }),
    }),
  clearPendingPairing: () =>
    fetchJSON<{ ok: boolean; cleared: number }>("/api/pairing/clear-pending", {
      method: "POST",
    }),

  // ── Admin: Webhooks ─────────────────────────────────────────────────
  getWebhooks: () => fetchJSON<WebhooksResponse>("/api/webhooks"),
  enableWebhooks: () =>
    fetchJSON<WebhookEnableResponse>("/api/webhooks/enable", { method: "POST" }),
  createWebhook: (body: WebhookCreate) =>
    fetchJSON<WebhookRoute & { secret: string }>("/api/webhooks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  deleteWebhook: (name: string) =>
    fetchJSON<{ ok: boolean }>(`/api/webhooks/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),
  setWebhookEnabled: (name: string, enabled: boolean) =>
    fetchJSON<{ ok: boolean; name: string; enabled: boolean }>(
      `/api/webhooks/${encodeURIComponent(name)}/enabled`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      },
    ),

  // ── Admin: Credential pool ──────────────────────────────────────────
  getCredentialPool: () =>
    fetchJSON<{ providers: CredentialPoolProvider[] }>("/api/credentials/pool"),
  addCredentialPoolEntry: (
    provider: string,
    api_key: string,
    label?: string,
  ) =>
    fetchJSON<{ ok: boolean; provider: string; count: number }>(
      "/api/credentials/pool",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, api_key, label }),
      },
    ),
  removeCredentialPoolEntry: (provider: string, index: number) =>
    fetchJSON<{ ok: boolean; provider: string; count: number }>(
      `/api/credentials/pool/${encodeURIComponent(provider)}/${index}`,
      { method: "DELETE" },
    ),

  // ── Admin: Memory provider ──────────────────────────────────────────
  getMemory: () => fetchJSON<MemoryStatus>("/api/memory"),
  getMemoryProviderConfig: (provider: string) =>
    fetchJSON<MemoryProviderConfig>(
      `/api/memory/providers/${encodeURIComponent(provider)}/config`,
    ),
  updateMemoryProviderConfig: (provider: string, values: Record<string, unknown>) =>
    fetchJSON<{ ok: boolean; active: string }>(
      `/api/memory/providers/${encodeURIComponent(provider)}/config`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      },
    ),
  setupMemoryProvider: (provider: string, values: Record<string, unknown> = {}) =>
    fetchJSON<MemoryProviderSetupResponse>(
      `/api/memory/providers/${encodeURIComponent(provider)}/setup`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ values }),
      },
    ),
  setMemoryProvider: (provider: string) =>
    fetchJSON<{ ok: boolean; active: string }>("/api/memory/provider", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider }),
    }),
  resetMemory: (target: "all" | "memory" | "user") =>
    fetchJSON<{ ok: boolean; deleted: string[] }>("/api/memory/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    }),

  // ── Admin: Gateway lifecycle ────────────────────────────────────────
  startGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/start", { method: "POST" }),
  stopGateway: () =>
    fetchJSON<ActionResponse>("/api/gateway/stop", { method: "POST" }),

  // ── Admin: Operations ───────────────────────────────────────────────
  runDoctor: () =>
    fetchJSON<ActionResponse>("/api/ops/doctor", { method: "POST" }),
  runSecurityAudit: () =>
    fetchJSON<ActionResponse>("/api/ops/security-audit", { method: "POST" }),
  runBackup: (output?: string) =>
    fetchJSON<ActionResponse>("/api/ops/backup", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output }),
    }),
  downloadBackup: (archive: string) =>
    authedFetch(
      `/api/ops/backup/download?archive=${encodeURIComponent(archive)}`,
    ),
  runImport: (archive: string, force = false) =>
    fetchJSON<ActionResponse>("/api/ops/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ archive, force }),
    }),
  runImportUpload: (file: File, force = false) => {
    const form = new FormData();
    form.append("force", String(force));
    form.append("file", file, file.name);
    return fetchJSON<ActionResponse>("/api/ops/import-upload", {
      method: "POST",
      body: form,
    });
  },
  getHooks: () => fetchJSON<HooksResponse>("/api/ops/hooks"),
  createHook: (body: HookCreate) =>
    fetchJSON<{ ok: boolean; event: string; command: string; approved: boolean }>(
      "/api/ops/hooks",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
    ),
  deleteHook: (event: string, command: string) =>
    fetchJSON<{ ok: boolean }>("/api/ops/hooks", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event, command }),
    }),
  getSystemStats: () => fetchJSON<SystemStats>("/api/system/stats"),

  // ── Admin: Curator ──────────────────────────────────────────────────
  getCurator: () => fetchJSON<CuratorStatus>("/api/curator"),
  setCuratorPaused: (paused: boolean) =>
    fetchJSON<{ ok: boolean; paused: boolean }>("/api/curator/paused", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paused }),
    }),
  runCurator: () =>
    fetchJSON<ActionResponse>("/api/curator/run", { method: "POST" }),

  // ── Admin: Portal ───────────────────────────────────────────────────
  getPortal: () => fetchJSON<PortalStatus>("/api/portal"),

  // ── Admin: Diagnostics (backgrounded) ───────────────────────────────
  runPromptSize: () =>
    fetchJSON<ActionResponse>("/api/ops/prompt-size", { method: "POST" }),
  runDump: () => fetchJSON<ActionResponse>("/api/ops/dump", { method: "POST" }),
  runConfigMigrate: () =>
    fetchJSON<ActionResponse>("/api/ops/config-migrate", { method: "POST" }),
  runDebugShare: (opts?: { redact?: boolean; lines?: number }) =>
    fetchJSON<DebugShareResponse>("/api/ops/debug-share", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        redact: opts?.redact ?? true,
        lines: opts?.lines ?? 200,
      }),
    }),


  getCheckpoints: () => fetchJSON<CheckpointsResponse>("/api/ops/checkpoints"),
  pruneCheckpoints: () =>
    fetchJSON<ActionResponse>("/api/ops/checkpoints/prune", { method: "POST" }),

  // ── Admin: Skills hub ───────────────────────────────────────────────
  // ``profile`` scopes install/uninstall/update and the installed-state
  // annotations to that profile (omitted = the dashboard's own profile).
  installSkillFromHub: (identifier: string, profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ identifier, profile: profile || undefined }),
    }),
  uninstallSkillFromHub: (name: string, profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/uninstall", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, profile: profile || undefined }),
    }),
  updateSkillsFromHub: (profile?: string) =>
    fetchJSON<ActionResponse>("/api/skills/hub/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ profile: profile || undefined }),
    }),
  searchSkillsHub: (q: string, source = "all", limit = 20, profile?: string) =>
    fetchJSON<SkillHubSearchResponse>(
      `/api/skills/hub/search?q=${encodeURIComponent(q)}&source=${encodeURIComponent(source)}&limit=${limit}${profile ? `&profile=${encodeURIComponent(profile)}` : ""}`,
    ),
  getSkillHubSources: (profile?: string) =>
    fetchJSON<SkillHubSourcesResponse>(
      `/api/skills/hub/sources${profileQuery(profile)}`,
    ),
  previewSkillFromHub: (identifier: string) =>
    fetchJSON<SkillHubPreview>(
      `/api/skills/hub/preview?identifier=${encodeURIComponent(identifier)}`,
    ),
  scanSkillFromHub: (identifier: string) =>
    fetchJSON<SkillHubScan>(
      `/api/skills/hub/scan?identifier=${encodeURIComponent(identifier)}`,
    ),

  // ── Finance (Loop.md §5.9) ──────────────────────────────────────────
  // The dashboard reverse-proxies /api/finance/* to the Finance service's
  // /v1/* (trader/swing_trader/api.py). The service may be offline — read
  // calls then throw (proxy 502/503) and the Finance page renders its
  // offline panel instead of the data sections.
  financeHealth: () => fetchJSON<FinanceHealth>("/api/finance/v1/health"),
  financeAccount: (mode?: FinanceMode) =>
    fetchJSON<FinanceAccountResponse>(
      `/api/finance/v1/account${financeQuery({ mode })}`,
    ),
  financeOrders: (activeOnly = false, mode?: FinanceMode) =>
    fetchJSON<FinanceOrder[]>(
      `/api/finance/v1/orders${financeQuery({ active_only: activeOnly || undefined, mode })}`,
    ),
  financeFills: (mode?: FinanceMode) =>
    fetchJSON<FinanceFill[]>(`/api/finance/v1/fills${financeQuery({ mode })}`),
  financeTrades: (openOnly = false, mode?: FinanceMode) =>
    fetchJSON<FinanceTrade[]>(
      `/api/finance/v1/trades${financeQuery({ open_only: openOnly || undefined, mode })}`,
    ),
  financeStats: (mode?: FinanceMode) =>
    fetchJSON<FinanceStats>(`/api/finance/v1/stats${financeQuery({ mode })}`),
  financeSnapshots: (limit = 90, mode?: FinanceMode) =>
    fetchJSON<FinanceSnapshot[]>(
      `/api/finance/v1/snapshots${financeQuery({ limit, mode })}`,
    ),
  financeMarket: () =>
    fetchJSON<FinanceMarketSnapshot>("/api/finance/v1/market"),
  financeWatchlist: () =>
    fetchJSON<FinanceWatchlistItem[]>("/api/finance/v1/watchlist"),
  financeLatestReports: () =>
    fetchJSON<FinanceReports>("/api/finance/v1/reports/latest"),
  // ── On-demand market data (Phase 0.75; READ/ANALYSIS-ONLY, Loop.md §3) ──
  // Powers the read-only cross-asset watch modules. yfinance may 404 a
  // symbol intermittently (GC=F/^TNX/518880.SS) — callers handle the throw
  // per-symbol with an inline "no data" note and never crash the panel.
  /** Latest (delayed) quote for one symbol — current price feedback. */
  financeQuote: (symbol: string) =>
    fetchJSON<FinanceQuote>(
      `/api/finance/v1/quote${financeQuery({ symbol })}`,
    ),
  /** K-line / candlestick OHLCV bars for one symbol (inline-SVG charting). */
  financeBars: (
    symbol: string,
    { timeframe = "1d", limit = 120 }: { timeframe?: string; limit?: number } = {},
  ) =>
    fetchJSON<FinanceBars>(
      `/api/finance/v1/bars${financeQuery({ symbol, timeframe, limit })}`,
    ),
  /** One-shot multi-agent analysis of one symbol (verdict + per-agent
   * signals + cited sources). READ-ONLY — forms a thesis, never an order. */
  financeAnalyze: (symbol: string) =>
    fetchJSON<FinanceAnalyze>(
      `/api/finance/v1/analyze${financeQuery({ symbol })}`,
    ),
  /**
   * Daily Investment Research brief (Loop.md §7 Phase 0.5). The endpoint
   * always answers while the service is up — a degraded brief carries
   * freshness warnings and nulls instead of failing.
   *
   * `market` selects the desk: the default US brief, or the China/HK
   * (Asia/Shanghai) morning brief (`?market=cn`), which is research-only
   * (risk is null, no pending candidates) and returns a valid degraded
   * brief before the CN session runs each day.
   */
  financeResearchBrief: (market: FinanceResearchMarket = "us") =>
    fetchJSON<FinanceResearchBrief>(
      `/api/finance/v1/research/brief${market === "us" ? "" : `?market=${market}`}`,
    ),
  /**
   * Manually re-run a market's RESEARCH session NOW (the "refresh research"
   * button), refreshing that desk's brief with fresh data instead of just
   * re-reading the cached one. Read-only (no orders) so it is ungated. 404s
   * when that research market's session is disabled.
   */
  financeRunResearch: (market: Exclude<FinanceResearchMarket, "us">) =>
    fetchJSON<FinanceRunResearchResult>(
      `/api/finance/v1/research/run?market=${market}`,
      { method: "POST" },
    ),
  /**
   * Source-linked semantic research search (Loop.md §5.10). The service
   * fails closed with 503 when the vector index is down; that case is
   * rethrown as {@link FinanceKnowledgeOfflineError} so the UI can render
   * a calm "research search offline" note instead of an error state.
   */
  financeKnowledgeSearch: async (
    q: string,
    k = 5,
  ): Promise<FinanceKnowledgeHit[]> => {
    try {
      return await fetchJSON<FinanceKnowledgeHit[]>(
        `/api/finance/v1/knowledge/search${financeQuery({ q, k })}`,
      );
    } catch (err) {
      if (err instanceof Error && err.message.startsWith("503:")) {
        throw new FinanceKnowledgeOfflineError(err.message);
      }
      throw err;
    }
  },
  financeCandidates: (status?: string, mode?: FinanceMode) =>
    fetchJSON<FinanceCandidate[]>(
      `/api/finance/v1/candidates${financeQuery({ status, mode })}`,
    ),
  financePendingCandidates: () =>
    fetchJSON<FinancePendingCandidate[]>("/api/finance/v1/candidates/pending"),
  financeAudit: (candidateId?: string) =>
    fetchJSON<FinanceAuditEvent[]>(
      `/api/finance/v1/audit${financeQuery({ candidate_id: candidateId })}`,
    ),
  /**
   * POST a human approve/reject/edit action for a pending candidate.
   *
   * Unlike the read endpoints this does NOT throw on non-2xx: the service
   * expresses domain outcomes as status codes with a structured body
   * (403 window_closed, 409 terminal/version_conflict, 422 invalid edit,
   * 404 unknown, 503 service inactive) that the approval UI must render
   * as notices rather than crashes. Network/proxy failures still throw so
   * callers can retry with the SAME idempotency key.
   */
  financeCandidateAction: async (
    id: string,
    body: FinanceActionRequest,
  ): Promise<FinanceActionOutcome> => {
    const res = await authedFetch(
      `/api/finance/v1/candidates/${encodeURIComponent(id)}/action`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Finance-Surface": "web",
        },
        body: JSON.stringify(body),
      },
    );
    let parsed: unknown = null;
    try {
      parsed = await res.json();
    } catch {
      /* non-JSON body (e.g. proxy 502 page) — normalized below */
    }
    if (parsed && typeof parsed === "object" && "code" in parsed) {
      const p = parsed as {
        ok?: boolean;
        code: string;
        message?: string;
        version?: number | null;
        candidate?: FinanceCandidate | null;
      };
      return {
        status: res.status,
        ok: p.ok === true,
        code: p.code,
        message: p.message ?? "",
        version: p.version ?? null,
        candidate: p.candidate ?? null,
      };
    }
    // FastAPI HTTPException ({"detail": ...}) or an opaque proxy error.
    let detail = res.statusText || `HTTP ${res.status}`;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const d = (parsed as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    }
    return {
      status: res.status,
      ok: false,
      code: res.status === 503 ? "service_unavailable" : `http_${res.status}`,
      message: detail,
      version: null,
      candidate: null,
    };
  },

  // ── Session catch-up (manual monitor→decide→push + finalize) ─────────
  // Human-only actions (a human web/desktop surface + a human actor; the
  // service 403s a system surface or an LLM/system actor, and 503s when the
  // trading loop is not attached). Both reuse ``financePortfolioWrite`` so a
  // domain 403/503 comes back as a structured outcome (never throws) that the
  // queue view renders as a clear notice; network/proxy failures still reject.
  /**
   * Manually run the full monitor→decide→push session NOW — a human catch-up
   * for a missed scheduled session (e.g. the 11:30 ET run). Risk-approved
   * candidates are pushed into a fresh approval window (``cutoff_et``); this
   * does NOT place orders — the human then approves/rejects each candidate in
   * the queue as usual. ``windowMinutes`` (5..240, default 60 on the service)
   * sizes the new window.
   */
  financeSessionRun: ({
    actor,
    windowMinutes,
  }: {
    actor: string;
    windowMinutes?: number;
  }) =>
    financePortfolioWrite<FinanceSessionRunResult>(
      "/api/finance/v1/session/run",
      {
        actor,
        ...(windowMinutes !== undefined
          ? { window_minutes: windowMinutes }
          : {}),
      },
    ),
  /**
   * Place the human-APPROVED candidates from the current window and expire the
   * rest. Same human-surface/human-actor (403) and loop-attached (503) guards
   * as {@link api.financeSessionRun}.
   */
  financeSessionFinalize: ({ actor }: { actor: string }) =>
    financePortfolioWrite<FinanceSessionFinalizeResult>(
      "/api/finance/v1/session/finalize",
      { actor },
    ),

  // ── Portfolio (Phase 0.9): real multi-account holdings ──────────────
  // Separate from the paper-trading account above. READ + DRAFT only: the
  // only writes are creating a draft and the human confirm/edit/reject
  // action (mirrors financeCandidateAction — a human surface + human actor,
  // never "system"/"hermes"). All routes proxy to the Finance service's
  // /v1/portfolio/* (trader/swing_trader/api.py); read calls throw when the
  // service is offline so the Portfolio tab renders its offline/error note.
  financePortfolioAccounts: () =>
    fetchJSON<FinancePortfolioAccount[]>("/api/finance/v1/portfolio/accounts"),
  financePortfolioAccount: (id: string) =>
    fetchJSON<FinancePortfolioAccount>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}`,
    ),
  financePortfolioHoldings: (id: string) =>
    fetchJSON<FinancePortfolioHoldings>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/holdings`,
    ),
  financePortfolioEvents: (id: string) =>
    fetchJSON<FinancePortfolioEvent[]>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/events`,
    ),
  financePortfolioReconcile: (id: string) =>
    fetchJSON<FinancePortfolioReconcile>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/reconcile`,
    ),
  financePortfolioAggregate: (includeInRiskOnly?: boolean) =>
    fetchJSON<FinancePortfolioAggregate>(
      `/api/finance/v1/portfolio/aggregate${financeQuery({
        include_in_risk_only: includeInRiskOnly || undefined,
      })}`,
    ),
  /**
   * Market-value + P&L valuation. With an `accountId` it values that single
   * account; without one it values the aggregate across accounts (and the
   * response carries an `accounts[]` id→name list + per-holding
   * `account_names`). `includeInRiskOnly` only applies to the aggregate.
   * Reader — throws when the Finance service is offline so the tab shows its
   * offline/error note.
   */
  financePortfolioValuation: (accountId?: string, includeInRiskOnly?: boolean) =>
    fetchJSON<FinancePortfolioValuation>(
      accountId
        ? `/api/finance/v1/portfolio/accounts/${encodeURIComponent(accountId)}/valuation`
        : `/api/finance/v1/portfolio/valuation${financeQuery({
            include_in_risk_only: includeInRiskOnly || undefined,
          })}`,
    ),
  financePortfolioAudit: (accountId?: string) =>
    fetchJSON<FinancePortfolioAudit[]>(
      `/api/finance/v1/portfolio/audit${financeQuery({ account_id: accountId })}`,
    ),
  financePortfolioDrafts: (accountId?: string, status?: string) =>
    fetchJSON<FinancePortfolioDraft[]>(
      `/api/finance/v1/portfolio/drafts${financeQuery({
        account_id: accountId,
        status,
      })}`,
    ),
  financePortfolioDraft: (id: string) =>
    fetchJSON<FinancePortfolioDraft>(
      `/api/finance/v1/portfolio/drafts/${encodeURIComponent(id)}`,
    ),
  /** Instrument type-ahead. `degraded` warns the caller results may be
   * partial (upstream provider slow/unavailable) — the UI shows a note. */
  financeInstrumentSearch: (q: string, market?: string, limit = 8) =>
    fetchJSON<FinanceInstrumentSearchResult>(
      `/api/finance/v1/instruments/search${financeQuery({ q, market, limit })}`,
    ),
  /** CSV dry-run: always 200 with a per-row verdict (no mutation, no surface
   * header). Throws only on service/proxy failure. */
  financePortfolioImportPreview: (id: string, csv: string) =>
    fetchJSON<FinanceImportPreview>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/import/preview`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ csv }),
      },
    ),

  // Writers — authedFetch with the human web surface header; each returns a
  // structured outcome (never throws on a domain non-2xx) so the forms
  // render server errors inline instead of blanking. Network/proxy failures
  // still reject so callers can catch + retry.
  financePortfolioCreateAccount: (body: FinancePortfolioAccountCreate) =>
    financePortfolioWrite<FinancePortfolioAccount>(
      "/api/finance/v1/portfolio/accounts",
      body,
    ),
  financePortfolioUpdateAccount: (
    id: string,
    body: FinancePortfolioAccountUpdate,
  ) =>
    financePortfolioWrite<FinancePortfolioAccount>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/update`,
      body,
    ),
  financePortfolioCreateDraft: (body: FinancePortfolioDraftCreate) =>
    financePortfolioWrite<FinancePortfolioDraft>(
      "/api/finance/v1/portfolio/drafts",
      { surface: "web", ...body },
    ),
  financePortfolioImportCommit: (id: string, csv: string, actor: string) =>
    financePortfolioWrite<FinanceImportCommit>(
      `/api/finance/v1/portfolio/accounts/${encodeURIComponent(id)}/import/commit`,
      { csv, actor },
    ),
  /** Refresh marks from live quotes for held EXCHANGE symbols. 场外基金 (bare
   * fund codes) have no live feed and come back in `skipped`. */
  financeRefreshMarks: () =>
    financePortfolioWrite<FinancePortfolioMarksRefresh>(
      "/api/finance/v1/portfolio/marks/refresh",
      {},
    ),
  /** Set/override the current price (mark) for one symbol — used to update a
   * 场外基金 NAV by hand. Currency defaults to CNY and source to `manual` on
   * the service when omitted. */
  financeSetMark: (body: FinancePortfolioSetMarkRequest) =>
    financePortfolioWrite<FinancePortfolioMark>(
      "/api/finance/v1/portfolio/marks",
      body,
    ),
  /**
   * POST a human confirm/edit/reject action for a portfolio draft. Mirrors
   * {@link api.financeCandidateAction}: does NOT throw on a domain non-2xx
   * (403 not_human, 422 incomplete/invalid_edit, 409 terminal/
   * version_conflict, 404 unknown) — those render as notices. The web
   * surface header + a human actor are mandatory or the service 403s.
   */
  financePortfolioDraftAction: async (
    id: string,
    body: FinancePortfolioDraftActionRequest,
  ): Promise<FinancePortfolioDraftActionOutcome> => {
    const res = await authedFetch(
      `/api/finance/v1/portfolio/drafts/${encodeURIComponent(id)}/action`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Finance-Surface": "web",
        },
        body: JSON.stringify(body),
      },
    );
    let parsed: unknown = null;
    try {
      parsed = await res.json();
    } catch {
      /* non-JSON body (e.g. proxy 502 page) — normalized below */
    }
    if (parsed && typeof parsed === "object" && "code" in parsed) {
      const p = parsed as {
        ok?: boolean;
        code: string;
        message?: string;
        version?: number | null;
        draft?: FinancePortfolioDraft | null;
        event?: FinancePortfolioEvent | null;
      };
      return {
        status: res.status,
        ok: p.ok === true,
        code: p.code,
        message: p.message ?? "",
        version: p.version ?? null,
        draft: p.draft ?? null,
        event: p.event ?? null,
      };
    }
    let detail = res.statusText || `HTTP ${res.status}`;
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const d = (parsed as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    }
    return {
      status: res.status,
      ok: false,
      code: res.status === 503 ? "service_unavailable" : `http_${res.status}`,
      message: detail,
      version: null,
      draft: null,
      event: null,
    };
  },
};

/**
 * POST a portfolio write (create account / draft, account update, import
 * commit) via {@link authedFetch} with the human web surface header,
 * normalized to a {@link FinancePortfolioWriteOutcome} that never throws on a
 * domain non-2xx (the forms render the server message inline). Network/proxy
 * failures reject so callers can catch them.
 */
async function financePortfolioWrite<T>(
  url: string,
  body: unknown,
): Promise<FinancePortfolioWriteOutcome<T>> {
  const res = await authedFetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Finance-Surface": "web",
    },
    body: JSON.stringify(body),
  });
  let parsed: unknown = null;
  try {
    parsed = await res.json();
  } catch {
    /* non-JSON body — normalized below */
  }
  if (res.ok) {
    return {
      ok: true,
      status: res.status,
      data: (parsed as T) ?? null,
      error: "",
    };
  }
  let detail = res.statusText || `HTTP ${res.status}`;
  if (parsed && typeof parsed === "object") {
    if ("detail" in parsed) {
      const d = (parsed as { detail: unknown }).detail;
      detail = typeof d === "string" ? d : JSON.stringify(d);
    } else if ("message" in parsed) {
      detail = String((parsed as { message: unknown }).message);
    }
  }
  return { ok: false, status: res.status, data: null, error: detail };
}

/** Build a query string from defined params only ("" when none). */
function financeQuery(
  params: Record<string, string | number | boolean | undefined>,
): string {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined) qs.set(k, String(v));
  }
  const s = qs.toString();
  return s ? `?${s}` : "";
}

/** Identity payload returned by ``GET /api/auth/me`` (Phase 7).
 *
 * Returned by the dashboard's gated middleware when a valid session cookie
 * is attached. ``email`` and ``display_name`` are empty strings under the
 * Nous Portal contract V1 (the access token has no email/name claims —
 * see Contract Anchor C4 in the plan). The AuthWidget surfaces a
 * truncated ``user_id`` instead.
 */
export interface AuthMeResponse {
  user_id: string;
  email: string;
  display_name: string;
  org_id: string;
  provider: string;
  expires_at: number;
}

export interface ActionResponse {
  archive?: string;
  name: string;
  ok: boolean;
  pid: number | null;
  error?: string;
  message?: string;
  uploaded_bytes?: number;
  update_command?: string;
}

export interface DebugShareResponse {
  ok: boolean;
  // label -> paste URL, e.g. { Report: "https://paste.rs/abc", "agent.log": "..." }
  urls: Record<string, string>;
  // "label: error" strings for optional full-log uploads that failed.
  failures: string[];
  redacted: boolean;
  auto_delete_seconds: number;
}

export interface SessionStoreStats {
  total: number;
  active_store: number;
  archived: number;
  messages: number;
  by_source: Record<string, number>;
}

export interface SessionImportResponse {
  ok: boolean;
  imported: number;
  skipped: number;
  detached: number;
  imported_ids: string[];
  skipped_ids: string[];
  errors: Array<Record<string, unknown>>;
}

export interface SkillHubResult {
  name: string;
  description: string;
  source: string;
  identifier: string;
  trust_level: string;
  repo: string | null;
  tags: string[];
}

/** Lock-entry summary for an already-installed hub skill (keyed by identifier). */
export interface SkillHubInstalledEntry {
  name: string | null;
  trust_level: string | null;
  scan_verdict: string | null;
}

export interface SkillHubSearchResponse {
  results: SkillHubResult[];
  /** source_id -> number of results returned by that source. */
  source_counts: Record<string, number>;
  /** source ids that didn't return within the parallel-search timeout. */
  timed_out: string[];
  /** identifier -> installed lock entry (for "already installed" badges). */
  installed: Record<string, SkillHubInstalledEntry>;
}

export interface SkillHubSource {
  id: string;
  label: string;
  /** GitHub only: whether the API is currently rate-limited. */
  rate_limited?: boolean;
  /** hermes-index only: whether the centralized index loaded. */
  available?: boolean;
}

export interface SkillHubSourcesResponse {
  sources: SkillHubSource[];
  index_available: boolean;
  /** Featured/popular skills from the centralized index (zero extra API calls). */
  featured: SkillHubResult[];
  installed: Record<string, SkillHubInstalledEntry>;
}

export interface SkillHubPreview {
  name: string;
  description: string;
  source: string;
  identifier: string;
  trust_level: string;
  repo: string | null;
  tags: string[];
  /** Rendered SKILL.md content (the actual skill text). */
  skill_md: string;
  /** Relative paths of every file in the bundle. */
  files: string[];
}

export interface SkillHubScanFinding {
  severity: string;
  category: string;
  file: string;
  line: number;
  description: string;
}

export interface SkillHubScan {
  name: string;
  identifier: string;
  source: string;
  trust_level: string;
  /** "safe" | "caution" | "dangerous". */
  verdict: string;
  summary: string;
  /** Install-policy decision for this trust+verdict combo. */
  policy: "allow" | "ask" | "block";
  policy_reason: string;
  findings: SkillHubScanFinding[];
  severity_counts: Record<string, number>;
}

// ── Admin types ───────────────────────────────────────────────────────

export interface McpServer {
  name: string;
  transport: "http" | "stdio" | "unknown";
  url: string | null;
  command: string | null;
  args: string[];
  env: Record<string, string>;
  auth: string | null;
  enabled: boolean;
  tools: string[] | null;
}

export interface McpCatalogEntry {
  name: string;
  description: string;
  source: string;
  transport: "http" | "stdio";
  auth_type: "api_key" | "oauth" | "none";
  required_env: Array<{ name: string; prompt: string; required: boolean }>;
  // Transport details — what actually connects (http) or runs (stdio).
  command: string | null;
  args: string[];
  url: string | null;
  // Git bootstrap (only set for entries that clone + build locally).
  install_url: string | null;
  install_ref: string | null;
  bootstrap: string[];
  // Default tool pre-selection (null = all tools pre-checked) + guidance text.
  default_enabled: string[] | null;
  post_install: string;
  needs_install: boolean;
  installed: boolean;
  enabled: boolean;
}

export interface McpCatalogDiagnostic {
  name: string;
  kind: string;
  message: string;
}


export interface McpServerCreate {
  name: string;
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  auth?: string;
}

export interface McpTestResult {
  ok: boolean;
  error?: string;
  tools: Array<{ name: string; description: string }>;
}

export interface MessagingPlatformEnvVar {
  key: string;
  required: boolean;
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  prompt: string;
  help: string;
  url: string | null;
  is_password: boolean;
  advanced: boolean;
}

export interface MessagingPlatform {
  id: string;
  name: string;
  description: string;
  docs_url: string;
  enabled: boolean;
  configured: boolean;
  gateway_running: boolean;
  /**
   * "connected" | "disabled" | "not_configured" | "pending_restart" |
   * "gateway_stopped" | "startup_failed" | "disconnected" | "fatal" | string
   */
  state: string;
  error_code: string | null;
  error_message: string | null;
  updated_at: string | null;
  home_channel: { platform: string; chat_id: string; name: string; thread_id?: string } | null;
  whatsapp_setup?: {
    mode?: string;
    allowed_users_set?: boolean;
    home_channel_set?: boolean;
  } | null;
  env_vars: MessagingPlatformEnvVar[];
}

export interface MessagingPlatformsResponse {
  env_path: string;
  gateway_start_command: string;
  platforms: MessagingPlatform[];
}

export interface MessagingPlatformUpdate {
  enabled?: boolean;
  env?: Record<string, string>;
  clear_env?: string[];
}

export interface MessagingPlatformTestResult {
  ok: boolean;
  state: string;
  message: string;
}

export interface PairingUser {
  platform: string;
  user_id: string;
  user_name?: string;
  code?: string;
  age_minutes?: number;
}

export interface PairingResponse {
  pending: PairingUser[];
  approved: PairingUser[];
}

export interface WebhookRoute {
  name: string;
  description: string;
  events: string[];
  deliver: string;
  deliver_only: boolean;
  prompt: string;
  skills: string[];
  created_at: string | null;
  url: string;
  secret_set: boolean;
  enabled: boolean;
}

export interface WebhooksResponse {
  enabled: boolean;
  base_url: string;
  subscriptions: WebhookRoute[];
}

export interface WebhookEnableResponse {
  ok: boolean;
  platform: "webhook";
  enabled: true;
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface WebhookCreate {
  name: string;
  description?: string;
  events?: string[];
  prompt?: string;
  skills?: string[];
  deliver?: string;
  deliver_only?: boolean;
  deliver_chat_id?: string;
}

export interface CredentialPoolEntry {
  index: number;
  id: string | null;
  label: string | null;
  auth_type: string | null;
  source: string | null;
  priority: number;
  last_status: string | null;
  request_count: number;
  token_preview: string;
  has_refresh: boolean;
}

export interface CredentialPoolProvider {
  provider: string;
  entries: CredentialPoolEntry[];
}

export interface MemoryProviderInfo {
  name: string;
  description: string;
  available: boolean;
  configured: boolean;
  status: "ready" | "needs_config" | "unavailable" | "missing";
  setup?: MemoryProviderSetupInfo;
}

export interface MemoryStatus {
  active: string;
  providers: MemoryProviderInfo[];
  builtin_files: { memory: number; user: number };
}

export interface MemoryProviderExternalDependency {
  name: string;
  install: string;
  check: string;
}

export interface MemoryProviderSetupInfo {
  pip_dependencies: string[];
  external_dependencies: MemoryProviderExternalDependency[];
  required_env: string[];
  dependencies_installed: boolean;
}

export interface MemoryProviderSetupResult {
  kind: string;
  name: string;
  status: string;
  command: string;
  returncode: number | null;
  stdout: string;
  stderr: string;
}

export interface MemoryProviderSetupResponse {
  ok: boolean;
  provider: string;
  results: MemoryProviderSetupResult[];
  status?: MemoryProviderInfo | null;
}

export interface MemoryProviderFieldOption {
  value: string;
  label: string;
  description?: string;
}

export interface MemoryProviderField {
  key: string;
  label: string;
  kind: "text" | "secret" | "select" | "boolean";
  description: string;
  placeholder: string;
  required: boolean;
  value: string | boolean;
  is_set: boolean;
  options: MemoryProviderFieldOption[];
  url: string;
  when?: Record<string, string | boolean | number> | null;
}

export interface MemoryProviderConfig {
  name: string;
  label: string;
  fields: MemoryProviderField[];
  setup?: MemoryProviderSetupInfo;
}

export interface HookEntry {
  event: string;
  matcher: string | null;
  command: string | null;
  timeout: number | null;
  allowed: boolean;
  approved_at?: string | null;
  executable?: boolean;
}

export interface HooksResponse {
  hooks: HookEntry[];
  valid_events: string[];
}

export interface HookCreate {
  event: string;
  command: string;
  matcher?: string;
  timeout?: number;
  approve?: boolean;
}

export interface UpdateCheckResponse {
  install_method: string;
  current_version: string;
  // commits behind: >=1 known count, 0 up to date, -1 behind by unknown
  // count (nix/pypi), or null when the check could not run.
  behind: number | null;
  update_available: boolean;
  can_apply: boolean;
  update_command: string;
  message: string | null;
}

export interface SystemStats {
  os: string;
  os_release: string;
  os_version: string;
  platform: string;
  arch: string;
  hostname: string;
  python_version: string;
  python_impl: string;
  hermes_version: string;
  cpu_count: number | null;
  psutil: boolean;
  cpu_percent?: number;
  load_avg?: number[];
  uptime_seconds?: number;
  memory?: { total: number; available: number; used: number; percent: number };
  disk?: { total: number; used: number; free: number; percent: number };
  process?: { pid: number; rss: number; create_time: number; num_threads: number };
}

export interface CuratorStatus {
  enabled: boolean;
  paused: boolean;
  interval_hours: number | null;
  last_run_at: string | null;
  min_idle_hours: number | null;
  stale_after_days: number | null;
  archive_after_days: number | null;
}

export interface PortalFeature {
  label: string;
  state: string;
}

export interface PortalStatus {
  logged_in: boolean;
  portal_url: string | null;
  inference_url: string | null;
  provider: string;
  subscription_url: string;
  features: PortalFeature[];
}

export interface CheckpointSession {
  session: string;
  files: number;
  bytes: number;
}

export interface CheckpointsResponse {
  sessions: CheckpointSession[];
  total_bytes: number;
}

/** Per-call overrides for {@link fetchJSON}. */
interface FetchJSONOptions {
  /** When true, a 401 response is surfaced as a normal thrown error rather
   *  than triggering the loopback stale-token page reload. Use for probes
   *  whose 401 is an expected signal (e.g. /api/auth/me in non-gated mode)
   *  rather than evidence of a rotated session token. */
  allowUnauthorized?: boolean;
}

export interface ActionStatusResponse {
  exit_code: number | null;
  lines: string[];
  name: string;
  pid: number | null;
  running: boolean;
}

export interface PlatformStatus {
  error_code?: string;
  error_message?: string;
  state: string;
  updated_at: string;
}

export interface StatusResponse {
  active_sessions: number;
  /** Phase 7: ``true`` when the dashboard's OAuth gate is engaged
   * (public bind, no ``--insecure``). Read alongside ``auth_providers``
   * to render a "gated / loopback" badge. */
  auth_required?: boolean;
  /** Phase 7: registered ``DashboardAuthProvider`` names (e.g. ``["nous"]``).
   * Empty in loopback mode; empty + ``auth_required=true`` is a
   * fail-closed state (the dashboard will refuse to bind). */
  auth_providers?: string[];
  /** False when the dashboard is running in a hosted/managed layout where
   * updates are handled by the outer launcher instead of ``hermes update``. */
  can_update_hermes?: boolean;
  config_path: string;
  config_version: number;
  env_path: string;
  gateway_exit_reason: string | null;
  gateway_health_url: string | null;
  gateway_pid: number | null;
  gateway_platforms: Record<string, PlatformStatus>;
  gateway_running: boolean;
  gateway_state: string | null;
  gateway_updated_at: string | null;
  hermes_home: string;
  latest_config_version: number;
  release_date: string;
  version: string;
}

export interface SessionInfo {
  id: string;
  source: string | null;
  model: string | null;
  title: string | null;
  started_at: number;
  ended_at: number | null;
  last_active: number;
  is_active: boolean;
  message_count: number;
  tool_call_count: number;
  input_tokens: number;
  output_tokens: number;
  preview: string | null;
  parent_session_id?: string | null;
}

export interface SessionLatestDescendantResponse {
  requested_session_id: string;
  session_id: string;
  path: string[];
  changed: boolean;
}

export interface PaginatedSessions {
  sessions: SessionInfo[];
  total: number;
  limit: number;
  offset: number;
}

export interface EnvVarInfo {
  is_set: boolean;
  redacted_value: string | null;
  description: string;
  url: string | null;
  category: string;
  is_password: boolean;
  tools: string[];
  advanced: boolean;
  /** True when this var is a messaging-platform credential owned by the Channels page. */
  channel_managed?: boolean;
  /** True when this key is set in .env but not in any catalog (user-added custom key). */
  custom?: boolean;
}

export interface TelegramOnboardingStartResponse {
  pairing_id: string;
  suggested_username: string;
  deep_link: string;
  qr_payload: string;
  expires_at: string;
}

export type TelegramOnboardingStatusResponse =
  | { status: "waiting"; expires_at: string }
  | {
      status: "ready";
      bot_username: string;
      owner_user_id?: string;
      expires_at: string;
    };

export interface TelegramOnboardingApplyResponse {
  ok: boolean;
  platform: "telegram";
  bot_username?: string;
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface WhatsAppOnboardingStartResponse {
  pairing_id: string;
  status:
    | "starting"
    | "installing"
    | "waiting"
    | "connected"
    | "error"
    | "expired"
    | "cancelled";
  qr_payload?: string | null;
  expires_at: string;
  mode: "bot" | "self-chat";
  allowed_users: string;
  account_id?: string | null;
  account_name?: string | null;
  account_phone?: string | null;
  error?: string | null;
}

export type WhatsAppOnboardingStatusResponse = WhatsAppOnboardingStartResponse;

export interface WhatsAppOnboardingApplyResponse {
  ok: boolean;
  platform: "whatsapp";
  needs_restart: boolean;
  restart_started?: boolean;
  restart_action?: string;
  restart_pid?: number | null;
  restart_error?: string;
}

export interface SessionMessage {
  role: "user" | "assistant" | "system" | "tool";
  content: string | null;
  tool_calls?: Array<{
    id: string;
    function: { name: string; arguments: string };
  }>;
  tool_name?: string;
  tool_call_id?: string;
  timestamp?: number;
}

export interface SessionMessagesResponse {
  session_id: string;
  messages: SessionMessage[];
}

export interface LogsResponse {
  file: string;
  lines: string[];
}

export interface ManagedFileEntry {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
  mtime: number;
  mime_type: string | null;
}

export interface ManagedFilesResponse {
  root: string | null;
  path: string;
  parent: string | null;
  locked_root: string | null;
  can_change_path: boolean;
  entries: ManagedFileEntry[];
}

export interface ManagedFileReadResponse {
  name: string;
  path: string;
  size: number;
  mime_type: string;
  data_url: string;
  root: string | null;
  locked_root: string | null;
  can_change_path: boolean;
}

export interface ManagedFileWriteResponse {
  ok: boolean;
  path: string;
  entry: ManagedFileEntry;
  root: string | null;
  locked_root: string | null;
  can_change_path: boolean;
}

export interface AnalyticsDailyEntry {
  day: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsModelEntry {
  model: string;
  input_tokens: number;
  output_tokens: number;
  estimated_cost: number;
  sessions: number;
  api_calls: number;
}

export interface AnalyticsSkillEntry {
  skill: string;
  view_count: number;
  manage_count: number;
  total_count: number;
  percentage: number;
  last_used_at: number | null;
}

export interface AnalyticsSkillsSummary {
  total_skill_loads: number;
  total_skill_edits: number;
  total_skill_actions: number;
  distinct_skills_used: number;
}

export interface AnalyticsResponse {
  daily: AnalyticsDailyEntry[];
  by_model: AnalyticsModelEntry[];
  totals: {
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  skills: {
    summary: AnalyticsSkillsSummary;
    top_skills: AnalyticsSkillEntry[];
  };
}

export interface ActiveProfileInfo {
  active: string;
  current: string;
}

export interface ProfileDescribeAutoResult {
  ok: boolean;
  reason: string;
  description: string | null;
  description_auto: boolean;
}

export interface ProfileInfo {
  name: string;
  path: string;
  is_default: boolean;
  model: string | null;
  provider: string | null;
  has_env: boolean;
  skill_count: number;
  gateway_running: boolean;
  description: string;
  description_auto: boolean;
  distribution_name: string | null;
  distribution_version: string | null;
  distribution_source: string | null;
  has_alias: boolean;
}

export interface ModelsAnalyticsModelEntry {
  model: string;
  provider: string;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  reasoning_tokens: number;
  estimated_cost: number;
  actual_cost: number;
  sessions: number;
  api_calls: number;
  tool_calls: number;
  last_used_at: number;
  avg_tokens_per_session: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

export interface ModelsAnalyticsResponse {
  models: ModelsAnalyticsModelEntry[];
  totals: {
    distinct_models: number;
    total_input: number;
    total_output: number;
    total_cache_read: number;
    total_reasoning: number;
    total_estimated_cost: number;
    total_actual_cost: number;
    total_sessions: number;
    total_api_calls: number;
  };
  period_days: number;
}

export interface CronJobRepeat {
  times: number | null;
  completed?: number;
}

export interface CronJobMutation {
  name?: string;
  prompt?: string;
  schedule?: string;
  deliver?: string;
  skills?: string[];
  provider?: string | null;
  model?: string | null;
  base_url?: string | null;
  script?: string | null;
  no_agent?: boolean;
  context_from?: string[] | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
}

export interface CronJob {
  id: string;
  profile?: string | null;
  profile_name?: string | null;
  hermes_home?: string | null;
  is_default_profile?: boolean;
  name?: string | null;
  prompt?: string | null;
  script?: string | null;
  skills?: string[] | null;
  schedule?: { kind?: string; expr?: string; run_at?: string; display?: string };
  schedule_display?: string | null;
  repeat?: CronJobRepeat | null;
  enabled: boolean;
  state?: string | null;
  deliver?: string | null;
  model?: string | null;
  provider?: string | null;
  base_url?: string | null;
  no_agent?: boolean | null;
  context_from?: string[] | string | null;
  enabled_toolsets?: string[] | null;
  workdir?: string | null;
  last_run_at?: string | null;
  next_run_at?: string | null;
  last_status?: string | null;
  last_error?: string | null;
  last_delivery_error?: string | null;
}

export interface CronDeliveryTarget {
  id: string;
  name: string;
  home_target_set: boolean;
  home_env_var: string | null;
}

export interface AutomationBlueprintField {
  name: string;
  type: "time" | "enum" | "text" | "weekdays";
  label: string;
  default: string | null;
  options: string[];
  optional: boolean;
  /** When false, options are suggestions — any value is accepted. */
  strict?: boolean;
  help: string;
}

export interface AutomationBlueprint {
  key: string;
  title: string;
  description: string;
  category: string;
  tags: string[];
  fields: AutomationBlueprintField[];
  command: string;
  appUrl: string;
}

export interface SkillInfo {
  name: string;
  description: string;
  category: string;
  enabled: boolean;
}

export interface SkillContent {
  name: string;
  content: string;
  path: string;
}

export interface SkillWriteResult {
  success: boolean;
  message?: string;
  path?: string;
  error?: string;
}

export interface ToolsetInfo {
  name: string;
  label: string;
  description: string;
  enabled: boolean;
  configured: boolean;
  tools: string[];
}

export interface ToolsetProviderEnvVar {
  key: string;
  prompt: string;
  url: string | null;
  default: string | null;
  is_set: boolean;
}

export interface ToolsetProvider {
  name: string;
  badge: string;
  tag: string;
  env_vars: ToolsetProviderEnvVar[];
  post_setup: string | null;
  requires_nous_auth: boolean;
  is_active: boolean;
}

export interface ToolsetConfig {
  name: string;
  has_category: boolean;
  providers: ToolsetProvider[];
  active_provider: string | null;
}

export interface ToolsetEnvResult {
  ok: boolean;
  name: string;
  saved: string[];
  skipped: string[];
  is_set: Record<string, boolean>;
}

export interface SessionSearchResult {
  session_id: string;
  snippet: string;
  role: string | null;
  source: string | null;
  model: string | null;
  session_started: number | null;
}

export interface SessionSearchResponse {
  results: SessionSearchResult[];
}

// ── Model info types ──────────────────────────────────────────────────

export interface ModelInfoResponse {
  model: string;
  provider: string;
  auto_context_length: number;
  config_context_length: number;
  effective_context_length: number;
  capabilities: {
    supports_tools?: boolean;
    supports_vision?: boolean;
    supports_reasoning?: boolean;
    context_window?: number;
    max_output_tokens?: number;
    model_family?: string;
  };
}

// ── Model options / assignment types ──────────────────────────────────

export interface ModelOptionProvider {
  name: string;
  slug: string;
  models?: string[];
  total_models?: number;
  is_current?: boolean;
  is_user_defined?: boolean;
  source?: string;
  warning?: string;
  authenticated?: boolean;
}

export interface ModelOptionsResponse {
  model?: string;
  provider?: string;
  providers?: ModelOptionProvider[];
}

export interface AuxiliaryTaskAssignment {
  task: string;
  provider: string;
  model: string;
  base_url: string;
}

export interface AuxiliaryModelsResponse {
  tasks: AuxiliaryTaskAssignment[];
  main: { provider: string; model: string };
}

export interface MoaModelSlot {
  provider: string;
  model: string;
}

export interface MoaConfigResponse {
  default_preset: string;
  active_preset: string;
  presets: Record<string, {
    reference_models: MoaModelSlot[];
    aggregator: MoaModelSlot;
    reference_temperature: number;
    aggregator_temperature: number;
    max_tokens: number;
    enabled: boolean;
  }>;
  reference_models: MoaModelSlot[];
  aggregator: MoaModelSlot;
  reference_temperature: number;
  aggregator_temperature: number;
  max_tokens: number;
  enabled: boolean;
}

export interface ModelAssignmentRequest {
  confirm_expensive_model?: boolean;
  scope: "main" | "auxiliary";
  provider: string;
  model: string;
  /** Optional OpenAI-compatible endpoint URL for custom/local main providers. */
  base_url?: string;
  /** For auxiliary: task slot name, "" for all, "__reset__" to reset all. */
  task?: string;
}

/** An auxiliary task still pinned to a provider that differs from the
 *  newly-selected main provider after a main-model switch. */
export interface StaleAuxAssignment {
  task: string;
  provider: string;
  model: string;
}

export interface ModelAssignmentResponse {
  confirm_message?: string;
  confirm_required?: boolean;
  ok: boolean;
  scope?: string;
  provider?: string;
  model?: string;
  tasks?: string[];
  reset?: boolean;
  /** Auxiliary slots still pinned to a different provider than the new main.
   *  Switching main never clears aux pins; this lets the UI warn the user
   *  their helper tasks aren't following the switch. Only set on scope:'main'. */
  stale_aux?: StaleAuxAssignment[];
}

// ── OAuth provider types ────────────────────────────────────────────────

export interface OAuthProviderStatus {
  logged_in: boolean;
  source?: string | null;
  source_label?: string | null;
  token_preview?: string | null;
  expires_at?: string | null;
  has_refresh_token?: boolean;
  last_refresh?: string | null;
  error?: string;
}

export interface OAuthProvider {
  id: string;
  name: string;
  /** "pkce" (browser redirect + paste code), "device_code" (show code + URL),
   *  or "external" (delegated to a separate CLI like Claude Code or Qwen). */
  flow: "pkce" | "device_code" | "external";
  cli_command: string;
  docs_url: string;
  status: OAuthProviderStatus;
}

export interface OAuthProvidersResponse {
  providers: OAuthProvider[];
}

/** Discriminated union — the shape of /start depends on the flow. */
export type OAuthStartResponse =
  | {
      session_id: string;
      flow: "pkce";
      auth_url: string;
      expires_in: number;
    }
  | {
      session_id: string;
      flow: "device_code";
      user_code: string;
      verification_url: string;
      expires_in: number;
      poll_interval: number;
    };

export interface OAuthSubmitResponse {
  ok: boolean;
  status: "approved" | "error";
  message?: string;
}

export interface OAuthPollResponse {
  session_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "error";
  error_message?: string | null;
  expires_at?: number | null;
}

// ── Dashboard theme types ──────────────────────────────────────────────

export interface DashboardThemeSummary {
  description: string;
  label: string;
  name: string;
  /** Full theme definition for user themes; undefined for built-ins
   *  (which the frontend already has locally). */
  definition?: DashboardTheme;
}

export interface DashboardThemesResponse {
  active: string;
  themes: DashboardThemeSummary[];
}

export interface DashboardFontResponse {
  /** Active font-override id, or "theme" when no override is set. */
  font: string;
}

// ── Dashboard plugin types ─────────────────────────────────────────────

export interface PluginManifestResponse {
  name: string;
  label: string;
  description: string;
  icon: string;
  version: string;
  tab: {
    path: string;
    position?: string;
    override?: string;
    hidden?: boolean;
  };
  slots?: string[];
  entry: string;
  css?: string | null;
  has_api: boolean;
  source: string;
}

export interface HubAgentPluginRow {
  name: string;
  version: string;
  description: string;
  source: string;
  runtime_status: "disabled" | "enabled" | "inactive";
  has_dashboard_manifest: boolean;
  dashboard_manifest: PluginManifestResponse | null;
  path: string;
  can_remove: boolean;
  can_update_git: boolean;
  auth_required: boolean;
  auth_command: string;
  user_hidden: boolean;
}

export interface PluginsHubProviders {
  memory_provider: string;
  memory_options: MemoryProviderInfo[];
  context_engine: string;
  context_options: Array<{ name: string; description: string }>;
}

export interface PluginsHubResponse {
  plugins: HubAgentPluginRow[];
  orphan_dashboard_plugins: PluginManifestResponse[];
  providers: PluginsHubProviders;
}

export interface AgentPluginInstallRequest {
  identifier: string;
  force?: boolean;
  enable?: boolean;
}

export interface AgentPluginInstallResponse {
  ok: boolean;
  plugin_name?: string;
  warnings?: string[];
  missing_env?: string[];
  after_install_path?: string | null;
  enabled?: boolean;
  error?: string;
}

export interface AgentPluginUpdateResponse {
  ok: boolean;
  name?: string;
  output?: string;
  unchanged?: boolean;
  error?: string;
}

export interface PluginProvidersPutRequest {
  memory_provider?: string;
  context_engine?: string;
}

// ── Finance types (Loop.md §5.9; shapes from trader/swing_trader/api.py) ─

export type FinanceMode = "paper" | "live";

/** Research desk for the Investment Research brief: the default US desk or
 * the China/HK (Asia/Shanghai) morning desk. The CN brief is research-only
 * (risk null, no pending candidates). */
export type FinanceResearchMarket = "us" | "cn" | "kr";

/** Result of POST /v1/research/run (ResearchSession.run_now summary). */
export interface FinanceRunResearchResult {
  market: string;
  market_label: string;
  ran_at: string;
  signals: number;
  sent: boolean;
  brief_ready: boolean;
}

export type FinanceBreakerState = "NORMAL" | "TRIPPED";

export interface FinanceHealth {
  status: string;
  mode: FinanceMode;
  loop_attached: boolean;
  breaker: FinanceBreakerState | "UNKNOWN";
  ts: string;
}

export interface FinancePosition {
  symbol: string;
  qty: number;
  avg_px: number;
  mkt_px: number | null;
  upnl: number | null;
  pool: string;
}

export interface FinanceOpenOrder {
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  order_type: string;
  limit: number | null;
  stop: number | null;
  status: string;
}

export interface FinanceStats {
  n_closed: number;
  n_wins: number;
  win_rate: number;
  avg_win: number | null;
  avg_loss: number | null;
  payoff_ratio: number | null;
  expectancy: number;
  total_pnl: number;
  avg_hold_days: number | null;
  max_drawdown_pct: number;
}

export interface FinanceSnapshot {
  ts: string;
  mode: FinanceMode;
  equity: number;
  cash: number;
  upnl: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: FinanceBreakerState;
}

/** Full account view when the daily loop is attached to the service. */
export interface FinanceAccountView {
  mode: FinanceMode;
  ts: string;
  equity: number;
  cash: number;
  upnl: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: FinanceBreakerState;
  positions: FinancePosition[];
  open_orders: FinanceOpenOrder[];
  stats: FinanceStats;
}

/** Ledger-only fallback when the loop is idle (evenings, weekends). */
export interface FinanceAccountLedger {
  mode: FinanceMode;
  snapshot: FinanceSnapshot | null;
  stats: FinanceStats;
  source: "ledger";
}

export type FinanceAccountResponse = FinanceAccountView | FinanceAccountLedger;

export interface FinanceOrder {
  id: string;
  ts: string;
  mode: FinanceMode;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  order_type: string;
  limit: number | null;
  stop: number | null;
  tp: number | null;
  tif: string;
  status: string;
  broker_ref: string | null;
  parent_order_id: string | null;
  oca_group: string | null;
  filled_qty: number;
  avg_fill_px: number | null;
}

export interface FinanceFill {
  id: string;
  ts: string;
  order_id: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  px: number;
  commission: number;
  mode: FinanceMode;
}

export interface FinanceTrade {
  id: string;
  mode: FinanceMode;
  symbol: string;
  qty: number;
  entry_order_id: string;
  exit_order_id: string | null;
  entry_px: number;
  exit_px: number | null;
  pnl: number | null;
  r_multiple: number | null;
  hold_days: number | null;
  rationale: string;
  ts?: string;
  entry_ts?: string;
  exit_ts: string | null;
  is_open?: boolean;
}

/** Latest MarketSnapshot dump, or ``{status: "no snapshot yet"}``. */
export interface FinanceMarketSnapshot {
  ts?: string;
  risk_on_off?: "risk_on" | "neutral" | "risk_off";
  vix?: number | null;
  breadth_pct_above_50dma?: number;
  indices?: Record<string, Record<string, number | null>>;
  status?: string;
}

export interface FinanceWatchlistItem {
  symbol: string;
  theme: string;
  ai_phase: string;
  role: string;
  enabled: boolean;
}

/** kind -> plain-text report (e.g. ``{morning: "..."}``). */
export type FinanceReports = Record<string, string>;

export type FinanceCandidateStatus =
  | "proposed"
  | "risk_approved"
  | "risk_vetoed"
  | "pushed"
  | "approved"
  | "edited"
  | "rejected"
  | "expired"
  | "placed";

export interface FinanceCandidate {
  id: string;
  ts: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  order_type: "LMT" | "STP" | "MOC" | "LOC" | "BRACKET";
  limit: number | null;
  stop: number | null;
  tp: number | null;
  sl: number | null;
  tif: string;
  rationale: string;
  confidence: number;
  signal_ids: string[];
  ref_px: number | null;
  valid_until: string | null;
  status: FinanceCandidateStatus;
  risk_note: string;
  pool: string;
}

export interface FinancePendingCandidate {
  candidate: FinanceCandidate;
  version: number;
  window_open: boolean;
}

export interface FinanceAuditEvent {
  ts: string;
  mode: string;
  candidate_id: string;
  action: string;
  actor: string;
  surface: string;
  version: number;
  idempotency_key: string;
  prev_status: string;
  new_status: string;
  applied: boolean;
  detail: string;
}

/** Human edits allowed on a pending candidate (server re-validates). */
export interface FinanceCandidateEdits {
  qty?: number;
  limit?: number;
  stop?: number;
  sl?: number;
  tp?: number;
}

export interface FinanceActionRequest {
  action: "approve" | "reject" | "edit";
  actor: string;
  idempotency_key: string;
  expected_version?: number;
  edits?: FinanceCandidateEdits;
}

/**
 * Normalized outcome of {@link api.financeCandidateAction}: service result
 * codes (``applied``/``replayed``/``window_closed``/``terminal``/
 * ``version_conflict``/``unknown_candidate``/``invalid_edit``) plus the
 * synthesized ``service_unavailable``/``http_*`` codes for non-envelope
 * error responses.
 */
export interface FinanceActionOutcome {
  status: number;
  ok: boolean;
  code: string;
  message: string;
  version: number | null;
  candidate: FinanceCandidate | null;
}

/**
 * Result of {@link api.financeSessionRun}: a manual monitor→decide→push run
 * that opens a fresh approval window. ``pushed`` risk-approved candidates now
 * await confirmation until ``cutoff_et`` (ET, ``"HH:MM"``); ``entries_halted``
 * means the dead-man's switch blocked NEW entries (stale data / drift) — exits
 * still flow. This does NOT place orders.
 */
export interface FinanceSessionRunResult {
  ran_at: string;
  risk_approved: number;
  pushed: number;
  cutoff_et: string;
  entries_halted: boolean;
  health_level: string | null;
  actor: string;
  surface: string;
}

/**
 * Result of {@link api.financeSessionFinalize}: places the human-APPROVED
 * candidates (``orders_added`` newly placed, ``orders_now_active`` total) and
 * expires the rest (``expired``).
 */
export interface FinanceSessionFinalizeResult {
  ran_at: string;
  approved: number;
  expired: number;
  orders_now_active: number;
  orders_added: number;
  actor: string;
  surface: string;
}

// ── Research brief types (Loop.md §7 Phase 0.5; trader/swing_trader/brief.py) ─

/** Per-source as-of times, ages, and stale flags (`FreshnessInfo`). */
export interface FinanceBriefFreshness {
  market_as_of: string | null;
  news_as_of: string | null;
  portfolio_as_of: string | null;
  market_age_minutes: number | null;
  news_age_minutes: number | null;
  portfolio_age_minutes: number | null;
  market_stale: boolean;
  news_stale: boolean;
  portfolio_stale: boolean;
  warnings: string[];
}

/** Dated market pulse (`RegimeView`); indices values carry `last` and
 * `sma50_dist_pct` per index symbol. */
export interface FinanceBriefRegime {
  risk_on_off: string;
  vix: number | null;
  breadth_pct_above_50dma: number;
  indices: Record<string, Record<string, number | null>>;
}

/** Account risk pulse with actionable warnings (`RiskView`). `stats` keys:
 * n_closed, win_rate, expectancy, max_drawdown_pct. */
export interface FinanceBriefRisk {
  equity: number;
  cash: number;
  day_pnl: number;
  drawdown_pct: number;
  breaker_state: string;
  pool_exposure_pct: Record<string, number>;
  warnings: string[];
  stats: Record<string, number>;
}

export interface FinanceBriefMover {
  symbol: string;
  last: number;
  dist_sma20_pct: number;
  dist_sma50_pct: number | null;
  theme: string;
  ai_phase: string;
  role: string;
  /** Market region from the symbol suffix (CN/HK/KR/US); null on older briefs. */
  region?: string | null;
}

export interface FinanceBriefTheme {
  theme: string;
  avg_dist_sma50_pct: number;
  n_symbols: number;
  leaders: string[];
}

export interface FinanceBriefNewsItem {
  headline: string;
  source: string;
  url: string;
  sentiment: number | null;
  symbol: string | null;
}

export interface FinanceBriefSignal {
  symbol: string;
  direction: string;
  confidence: number;
  source_agent: string;
  thesis: string;
}

/** Compact "actions requiring attention" row (`PendingCandidate`). */
export interface FinanceBriefPendingCandidate {
  symbol: string;
  side: string;
  qty: number;
  confidence: number;
  status: string;
}

export interface FinanceProvenanceLink {
  label: string;
  url: string;
}

/** The daily Investment Research brief (`ResearchBrief`). Always answered
 * by the service — a degraded brief has freshness warnings + null sections. */
export interface FinanceResearchBrief {
  as_of: string;
  trading_date: string;
  mode: FinanceMode;
  freshness: FinanceBriefFreshness;
  regime: FinanceBriefRegime | null;
  risk: FinanceBriefRisk | null;
  movers: { top: FinanceBriefMover[]; bottom: FinanceBriefMover[] };
  themes: FinanceBriefTheme[];
  events: { earnings: unknown[]; notes: string[] };
  news: {
    items: FinanceBriefNewsItem[];
    per_symbol_sentiment: Record<string, number>;
  };
  signals_today: FinanceBriefSignal[];
  candidates_today: {
    counts: Record<string, number>;
    pending: FinanceBriefPendingCandidate[];
  };
  uncertainty: string[];
  provenance: FinanceProvenanceLink[];
}

// ── On-demand market-data types (Phase 0.75; trader/swing_trader/api.py) ─

/** Latest delayed quote from GET /quote. `note` is the honest data-delay
 * disclaimer (`MARKET_DATA_NOTE`). */
export interface FinanceQuote {
  symbol: string;
  last: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  as_of: string;
  note: string;
}

/** One OHLCV bar from GET /bars. */
export interface FinanceBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** K-line series from GET /bars. */
export interface FinanceBars {
  symbol: string;
  timeframe: string;
  bars: FinanceBar[];
  as_of: string;
  note: string;
}

/** One sub-agent (or debate verdict) signal in a GET /analyze response. */
export interface FinanceAnalyzeSignal {
  source_agent: string;
  direction: string;
  confidence: number;
  thesis: string;
  features: Record<string, unknown>;
}

/** One scored headline in a GET /analyze response. */
export interface FinanceAnalyzeNewsItem {
  headline: string;
  source: string;
  url: string;
  sentiment: number | null;
}

/** One cited research source in a GET /analyze response. */
export interface FinanceAnalyzeResearchSource {
  title: string | null;
  url: string;
  publisher: string | null;
  trading_date: string | null;
}

/** Multi-agent analysis from GET /analyze. `verdict` is the bull/bear
 * debate synthesis (null when no sub-agent produced a signal). READ-ONLY. */
export interface FinanceAnalyze {
  symbol: string;
  last: number | null;
  verdict: FinanceAnalyzeSignal | null;
  signals: FinanceAnalyzeSignal[];
  news: FinanceAnalyzeNewsItem[];
  research: FinanceAnalyzeResearchSource[];
  as_of: string;
  note: string;
}

/** One hit from GET /knowledge/search (`search_knowledge`). */
export interface FinanceKnowledgeHit {
  document_id: string;
  title: string;
  snippet: string;
  source_url: string;
  publisher: string;
  score: number;
  trading_date: string;
}

/** Thrown by {@link api.financeKnowledgeSearch} when the knowledge index is
 * down (service 503, fail-closed) — render a calm offline note. */
export class FinanceKnowledgeOfflineError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "FinanceKnowledgeOfflineError";
  }
}

// ── Portfolio types (Phase 0.9: real multi-account US/HK/CN holdings) ────
// Separate from the paper-trading account shapes above. READ + DRAFT only.

export type FinancePortfolioMarket = "US" | "HK" | "CN";
export type FinancePortfolioProvider = "manual" | "ibkr";
export type FinancePortfolioAccountType = "cash" | "margin";
export type FinanceInstrumentSecurityType = "stock" | "etf" | "fund";
export type FinancePortfolioDraftStatus =
  | "draft"
  | "confirmed"
  | "rejected"
  | "expired";
export type FinancePortfolioAuthority = "broker" | "manual";

export interface FinancePortfolioAccount {
  id: string;
  name: string;
  provider: string;
  market_scope: FinancePortfolioMarket;
  account_type: FinancePortfolioAccountType;
  base_currency: string;
  include_in_risk: boolean;
  note: string;
  created_at: string;
  updated_at: string;
}

export interface FinancePortfolioHolding {
  symbol: string;
  /** Instrument display name (e.g. "华夏全球科技先锋混合(QDII)A"); null when unknown. */
  display_name?: string | null;
  market: string;
  currency: string;
  qty: number;
  /** Nullable: when unknown, `cost_basis_known` is false — render a
   * localized "unknown", never a fabricated 0. */
  avg_cost: number | null;
  cost_basis_known: boolean;
}

export interface FinancePortfolioCash {
  currency: string;
  amount: number | null;
  known: boolean;
}

export interface FinancePortfolioHoldings {
  account_id: string;
  as_of: string;
  n_events: number;
  holdings: FinancePortfolioHolding[];
  cash: FinancePortfolioCash[];
}

export interface FinancePortfolioEvent {
  event_type: string;
  symbol: string | null;
  market: string | null;
  currency: string | null;
  qty: number | null;
  price: number | null;
  commission: number | null;
  amount: number | null;
  occurred_at: string;
  source: string;
  external_id: string | null;
  note: string;
}

export interface FinancePortfolioDrift {
  symbol: string;
  portfolio_qty: number;
  broker_qty: number;
}

export interface FinancePortfolioReconcile {
  account_id: string;
  ok: boolean;
  authority: FinancePortfolioAuthority;
  summary: string;
  note: string;
  as_of: string;
  drifts: FinancePortfolioDrift[];
}

export interface FinancePortfolioAggregateHolding
  extends FinancePortfolioHolding {
  /** Ids of the accounts this aggregate row rolls up. */
  accounts: string[];
}

export interface FinancePortfolioAggregate {
  accounts: number;
  as_of: string;
  holdings: FinancePortfolioAggregateHolding[];
  cash: FinancePortfolioCash[];
}

// ── Valuation (market value + P&L) ──
// Where the source price came from: a live quote, an imported CSV mark, a
// hand-entered manual mark, or `none` (unpriced — e.g. a 场外基金 whose NAV
// has no live feed). Render "unknown"/dash for `none`, never a fabricated 0.
export type FinancePortfolioPriceSource = "live" | "csv" | "manual" | "none";

export interface FinancePortfolioValuationHolding {
  symbol: string;
  /** Instrument display name (e.g. "华夏全球科技先锋混合(QDII)A"); null when unknown. */
  display_name?: string | null;
  market: FinancePortfolioMarket | null;
  currency: string;
  qty: number;
  /** Nullable — see `cost_basis_known`. */
  avg_cost: number | null;
  cost_basis_known: boolean;
  /** Nullable when unpriced (`price_source === "none"`). */
  price: number | null;
  price_as_of: string | null;
  price_source: FinancePortfolioPriceSource;
  /** qty × price. Null when the price is unknown. */
  market_value: number | null;
  /** qty × avg_cost. Null when the cost basis is unknown. */
  cost: number | null;
  /** market_value − cost. Null when either is unknown. */
  unrealized_pnl: number | null;
  /** unrealized_pnl / cost, as a FRACTION (0.2 = +20%). Null when unknown. */
  pnl_pct: number | null;
  /** Account ids this row rolls up (aggregate); a single id per-account. */
  accounts: string[];
  /** Human account names parallel to `accounts` (aggregate only; [] per-account). */
  account_names: string[];
}

export interface FinancePortfolioValuationTotal {
  currency: string;
  /** holdings_value + cash. */
  market_value: number;
  holdings_value: number;
  cash: number;
  cost: number;
  unrealized_pnl: number;
  /** Fraction (0.2 = +20%); null when total cost is 0/unknown. */
  pnl_pct: number | null;
  n_priced: number;
  n_unpriced: number;
}

/** id → name pair for the accounts an aggregate valuation rolls up. */
export interface FinancePortfolioAccountRef {
  id: string;
  name: string;
}

export interface FinancePortfolioValuation {
  as_of: string;
  /** Present only on the aggregate endpoint. */
  accounts?: FinancePortfolioAccountRef[];
  totals: FinancePortfolioValuationTotal[];
  holdings: FinancePortfolioValuationHolding[];
}

/** Body for POST /portfolio/marks (set/override one symbol's current price). */
export interface FinancePortfolioSetMarkRequest {
  symbol: string;
  price: number;
  currency?: string;
  source?: "manual" | "csv" | "live";
  actor: string;
}

/** Result of POST /portfolio/marks. */
export interface FinancePortfolioMark {
  symbol: string;
  price: number;
  currency: string;
  as_of: string;
  source: string;
}

/** Result of POST /portfolio/marks/refresh. */
export interface FinancePortfolioMarksRefresh {
  refreshed: string[];
  failed: string[];
  skipped: string[];
}

export interface FinancePortfolioAudit {
  ts: string;
  action: string;
  actor: string;
  surface: string;
  applied: boolean;
  detail: string;
}

export interface FinancePortfolioDraft {
  id: string;
  account_id: string;
  event_type: string;
  symbol: string | null;
  market: string | null;
  currency: string | null;
  qty: number | null;
  price: number | null;
  commission: number | null;
  amount: number | null;
  occurred_at: string | null;
  source: string;
  note: string;
  status: FinancePortfolioDraftStatus;
  version: number;
  original_text: string;
  missing: string[];
  ambiguities: string[];
  created_by: string;
  confirmed_by: string | null;
  confirmed_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface FinanceInstrumentMatch {
  canonical_symbol: string;
  display_name: string;
  market: string;
  exchange: string;
  currency: string;
  security_type: FinanceInstrumentSecurityType;
  provider_id: string;
}

export interface FinanceInstrumentSearchResult {
  query: string;
  degraded: boolean;
  source: string;
  matches: FinanceInstrumentMatch[];
}

export interface FinanceImportRow {
  line: number;
  duplicate: boolean;
  errors: string[];
  ok: boolean;
  event_type: string;
  symbol: string;
  qty: number | null;
  price: number | null;
  amount: number | null;
}

export interface FinanceImportPreview {
  header_error: string | null;
  n_valid: number;
  n_invalid: number;
  n_duplicate: number;
  committable: boolean;
  rows: FinanceImportRow[];
}

export interface FinanceImportCommit {
  n_committed: number;
  n_duplicate: number;
  n_skipped: number;
  event_ids: string[];
}

// ── Portfolio write request bodies + outcomes ──

export interface FinancePortfolioAccountCreate {
  name: string;
  market_scope: FinancePortfolioMarket;
  base_currency: string;
  provider?: FinancePortfolioProvider;
  account_type?: FinancePortfolioAccountType;
  include_in_risk?: boolean;
  note?: string;
  actor: string;
}

export interface FinancePortfolioAccountUpdate {
  name?: string;
  include_in_risk?: boolean;
  note?: string;
  account_type?: FinancePortfolioAccountType;
  actor: string;
}

export interface FinancePortfolioDraftCreate {
  account_id: string;
  event_type: string;
  symbol?: string;
  market?: string;
  currency?: string;
  qty?: number;
  price?: number;
  commission?: number;
  amount?: number;
  occurred_at?: string;
  note?: string;
  original_text?: string;
  created_by?: string;
  surface?: string;
}

export interface FinancePortfolioDraftEdits {
  event_type?: string;
  symbol?: string;
  market?: string;
  currency?: string;
  qty?: number;
  price?: number;
  commission?: number;
  amount?: number;
  occurred_at?: string;
  note?: string;
}

export interface FinancePortfolioDraftActionRequest {
  action: "confirm" | "edit" | "reject";
  actor: string;
  idempotency_key: string;
  expected_version?: number;
  edits?: FinancePortfolioDraftEdits;
}

/** Structured result of a portfolio write; `ok` mirrors the HTTP 2xx. */
export interface FinancePortfolioWriteOutcome<T> {
  ok: boolean;
  status: number;
  data: T | null;
  error: string;
}

/**
 * Normalized outcome of {@link api.financePortfolioDraftAction}: service
 * result codes (``applied``/``replayed``/``not_human``/``incomplete``/
 * ``invalid_edit``/``terminal``/``version_conflict``/``unknown``) plus the
 * synthesized ``service_unavailable``/``http_*`` codes for non-envelope
 * error responses.
 */
export interface FinancePortfolioDraftActionOutcome {
  status: number;
  ok: boolean;
  code: string;
  message: string;
  version: number | null;
  draft: FinancePortfolioDraft | null;
  event: FinancePortfolioEvent | null;
}
