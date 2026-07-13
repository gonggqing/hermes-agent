export type Locale =
  | "en"
  | "zh"
  | "zh-hant"
  | "ja"
  | "de"
  | "es"
  | "fr"
  | "tr"
  | "uk"
  | "af"
  | "ko"
  | "it"
  | "ga"
  | "pt"
  | "ru"
  | "hu";

/**
 * Finance tab (research-first, Loop.md §7 Phase 0.5). Kept as a named
 * interface so the Finance pages can type their fallback resolution: the
 * section is optional on {@link Translations} (newer-section convention —
 * locales that have not translated it yet fall back to the English catalog
 * entry via `useFinanceT()` instead of hard-coded literals).
 */
export interface FinanceTranslations {
  page: {
    breakerTrippedTitle: string;
    breakerTrippedBody: string;
    loopAttached: string;
    loopIdle: string;
    offline: string;
    online: string;
    updatedAt: string; // "updated {time}"
    serviceOfflineTitle: string;
    serviceOfflineBody: string;
    serviceOfflineStartLabel: string;
    modePaper: string;
    modeLive: string;
    // Bottom status/utility bar.
    breakerLabel: string; // "Breaker"
    breakerNormal: string; // "Normal"
    breakerTripped: string; // "Tripped"
    toggleMode: string; // aria-label for the paper/live toggle
  };
  brief: {
    title: string;
    tradingDate: string;
    asOf: string; // "as of {time}"
    unavailable: string;
    staleWarningsTitle: string;
    risk: {
      title: string;
      equity: string;
      cash: string;
      dayPnl: string;
      drawdown: string;
      breaker: string;
      poolExposure: string;
      winRate: string;
      expectancy: string;
      maxDrawdown: string;
      closedTrades: string; // "{n} closed trades"
      unavailable: string;
    };
    regime: {
      title: string;
      vix: string;
      breadth: string;
      unavailable: string;
    };
    movers: {
      title: string;
      top: string;
      bottom: string;
      symbol: string;
      last: string;
      vsSma20: string;
      vsSma50: string;
      theme: string;
      role: string;
      empty: string;
    };
    themes: {
      title: string;
      symbols: string; // "{n} symbols"
      leaders: string;
      empty: string;
    };
    news: {
      title: string;
      empty: string;
      sentiment: string;
    };
    signals: {
      title: string;
      empty: string;
      confidence: string; // "confidence {pct}%"
    };
    uncertainty: {
      title: string;
      empty: string;
    };
    provenance: {
      title: string;
    };
    search: {
      title: string;
      placeholder: string;
      searching: string;
      offline: string;
      noResults: string;
    };
    // Market toggle on the Investment Research view (US vs China/HK).
    markets: {
      label: string;
      us: string;
      cn: string;
      researchOnly: string; // "Research only — no orders"
    };
  };
  queue: {
    title: string;
    pendingCount: string; // "{count} pending"
    earliestExpiry: string; // "earliest expiry {time}"
    expand: string;
    collapse: string;
    // Approval queue (Loop.md §5.6) — the only execution surface.
    approvalTitle: string;
    noPending: string;
    windowClosed: string; // badge
    windowClosedHint: string;
    confidenceVersion: string; // "confidence {pct}% · v{version}"
    noRationale: string;
    riskNote: string; // "Risk: {note}"
    metaLine: string; // "ref {ref} · valid until {valid} · proposed {proposed}"
    approve: string;
    reject: string;
    edit: string;
    saveApprove: string;
    cancel: string;
    fields: {
      qty: string;
      limit: string;
      stop: string;
      tp: string;
      sl: string;
    };
    errPositive: string; // "{field} must be a positive number"
    errQtyRequired: string;
    verbApproved: string;
    verbRejected: string;
    verbEdited: string;
    outcome: {
      requestFailed: string; // "{symbol}: request failed ({error}) — retry to resend"
      applied: string; // "{symbol} {verb}"
      replayed: string; // "{symbol}: already processed — previous result replayed"
      windowClosed: string; // "{symbol}: {hint}"
      versionConflict: string; // "{symbol}: candidate changed on the server — refreshing"
      terminal: string; // "{symbol}: candidate already finalized ({message})"
      terminalState: string; // fallback message for `terminal`
      unknownCandidate: string; // "{symbol}: unknown candidate — refreshing"
      invalid: string; // "{symbol}: {message}"
      invalidFallback: string; // "invalid request"
      serviceUnavailable: string; // "Finance confirmation service is not active ({message})"
      unexpected: string; // "{symbol}: {message}"
      unexpectedFallback: string; // "unexpected response (HTTP {status})"
    };
  };
  history: {
    title: string;
    empty: string;
    expand: string; // aria-label on the expand column
    auditLoading: string;
    auditError: string;
    auditEmpty: string;
    auditBy: string; // "by {actor} via {surface} (v{version})"
    refused: string;
    colTime: string;
    colSymbol: string;
    colSide: string;
    colQty: string;
    colType: string;
    colConf: string;
    colStatus: string;
    fillsTitle: string;
    fillsEmpty: string;
    colPrice: string;
    colComm: string;
    statsTitle: string;
    statsEmpty: string;
    winRate: string;
    expectancy: string;
    payoff: string;
    maxDd: string;
    summary: string; // "{closed} closed trades · {wins} wins · total"
    avgHold: string; // " · avg hold {days}d"
  };
  account: {
    title: string;
    ledgerFallback: string;
    empty: string;
    emptyWithStats: string;
    equity: string;
    cash: string;
    upnl: string;
    dayPnl: string;
    drawdown: string;
    equityCurve: string;
    notEnoughSnapshots: string;
  };
  positions: {
    title: string;
    loopOnly: string;
    empty: string;
    symbol: string;
    qty: string;
    avgPx: string;
    mktPx: string;
    upnl: string;
    pool: string;
  };
  orders: {
    title: string;
    loopOnly: string;
    empty: string;
    symbol: string;
    side: string;
    qty: string;
    type: string;
    limit: string;
    stop: string;
    status: string;
  };
  market: {
    title: string;
    vix: string;
    breadth: string;
    asOf: string; // "as of {time}"
    noSnapshot: string;
  };
  reports: {
    title: string;
    empty: string;
  };
  // Master-detail shell: top tabs, grouped sidebar, and the bottom
  // paper/live mode switcher shared by all three views.
  layout: {
    tabResearch: string;
    tabQueue: string;
    tabPortfolio: string;
    tabHoldings: string; // Phase 0.9 real multi-account holdings sub-area
    // Research sidebar sections + entries.
    marketsGroup: string; // "Markets"
    watchGroup: string; // "Watch"
    marketUs: string;
    marketChina: string;
    marketHk: string;
    marketUk: string;
    marketKorea: string;
    marketJapan: string;
    comingSoon: string; // disabled placeholder badge, e.g. "Phase 0.9"
    perRegionNote: string; // China/HK derived from one CN brief note
    selectDesk: string; // aria-label for the narrow-screen desk select
    // Bottom mode switcher.
    modeLabel: string; // aria-label "Trading mode"
    modePaper: string;
    modeLive: string;
    modeFollowsService: string; // "follows service ({mode})"
    // Queue master-detail.
    queueSidebarTitle: string;
    queueRowStatus: string; // "{status}"
    queueSelectHint: string; // right-pane hint when nothing selected
    queueNoneForMode: string; // "No {mode} actions pending."
    // Portfolio master-detail.
    portfolioAccountGroup: string;
    portfolioPositionsGroup: string;
    rowAccount: string;
    rowOrders: string;
    rowHistory: string;
    rowMarket: string;
    rowReports: string;
    positionsEmpty: string;
    selectPositionHint: string; // right-pane hint when nothing selected
  };
  // Read-only cross-asset watch modules (Gold/Oil/Rates/Crypto).
  watch: {
    gold: string;
    oil: string;
    rates: string;
    crypto: string;
    readOnlyNote: string; // "Read-only watch module — no orders (Loop.md §3)"
    price: string;
    bid: string;
    ask: string;
    volume: string;
    // Localized price-unit words (rendered as "… / <unit>"). Percent stays "%".
    units: {
      oz: string; // troy ounce (盎司)
      share: string; // per share (股)
      bbl: string; // barrel (桶)
      gram: string; // gram (克)
    };
    // AU9999 / SGE domestic spot gold (a derived symbol in the Gold module).
    au9999Label: string; // "AU9999 · Domestic Spot Gold"
    au9999Note: string; // "Derived from international gold × CNY"
    asOf: string; // "as of {time}"
    noData: string; // per-symbol inline error
    chartUnavailable: string;
    chartLoading: string; // aria-label for the chart skeleton
    // Crosshair tooltip (K-line hover details).
    date: string;
    open: string;
    high: string;
    low: string;
    close: string;
    // Timeframe presets (segmented switcher on the K-line chart).
    timeframe: {
      label: string; // aria-label for the switcher group
      intraday: string; // "1D" — one intraday session
      fiveDay: string; // "5D"
      day: string; // "D" (default)
      week: string; // "M" — ~5y of weekly candles
      month: string; // "Y" — ~20y of monthly candles
    };
    // Educational title/tooltip per timeframe: resolution + span shown.
    timeframeHint: {
      intraday: string;
      fiveDay: string;
      day: string;
      week: string;
      month: string;
    };
    // K-line chart controls (symbol dropdown + indicators menu).
    selectSymbol: string; // aria-label for the symbol dropdown
    priceScale: string; // caption for the price axis unit chip
    // Indicators toggle menu — each carries a short educational description.
    indicators: {
      title: string; // "Indicators" (button + panel title)
      hint: string; // one-line panel intro
      overlay: string; // tag: drawn on the price (main) pane
      pane: string; // tag: drawn in its own sub-pane
      ma: string; // "MA"
      maDesc: string;
      ema: string; // "EMA"
      emaDesc: string;
      boll: string; // "BOLL"
      bollDesc: string;
      vol: string; // "VOL"
      volDesc: string;
      macd: string; // "MACD"
      macdDesc: string;
      rsi: string; // "RSI"
      rsiDesc: string;
      kdj: string; // "KDJ"
      kdjDesc: string;
    };
    // Moving-average overlay legend (均线).
    ma20: string; // "MA20"
    ma30: string; // "MA30"
    analyze: string;
    analyzing: string;
    analysisFor: string; // "Analysis · {symbol}"
    hideAnalysis: string;
    verdict: string;
    noVerdict: string;
    signals: string;
    sources: string;
    direction: string;
    confidence: string; // "confidence {pct}%"
    dataDelay: string; // honest delay disclaimer (fallback if note absent)
    loadingQuote: string;
  };
  // Phase 0.9 real multi-account holdings sub-area (US/HK/CN). READ + DRAFT
  // only — the only writes are creating a draft + the human confirm/edit/
  // reject action. Separate from the paper-trading account above.
  portfolio: {
    // Shell + sidebar.
    allAccounts: string;
    drafts: string;
    addAccount: string;
    accountsGroup: string;
    reviewGroup: string;
    loading: string;
    loadError: string;
    selectHint: string;
    noAccounts: string;
    includeInRisk: string; // "In risk" badge
    // Enum labels (web has no finance.enums catalog — localized here).
    markets: { US: string; HK: string; CN: string };
    providers: { manual: string; ibkr: string };
    accountTypes: { cash: string; margin: string };
    securityTypes: { stock: string; etf: string; fund: string };
    eventTypes: {
      buy: string;
      sell: string;
      dividend: string;
      deposit: string;
      withdraw: string;
      fee: string;
      opening: string;
      split: string;
    };
    draftStatus: {
      draft: string;
      confirmed: string;
      rejected: string;
      expired: string;
    };
    authority: { broker: string; manual: string };
    // Account-detail sub-tabs.
    tabs: {
      holdings: string;
      record: string;
      activity: string;
      reconcile: string;
      import: string;
      settings: string;
    };
    // Add-account form.
    form: {
      title: string;
      name: string;
      namePlaceholder: string;
      market: string;
      baseCurrency: string;
      baseCurrencyPlaceholder: string;
      provider: string;
      accountType: string;
      includeInRisk: string;
      includeInRiskHint: string;
      note: string;
      notePlaceholder: string;
      submit: string;
      submitting: string;
      errName: string;
      errCurrency: string;
      created: string; // "Account “{name}” created."
      failed: string; // "Could not create the account ({message})."
    };
    // Holdings table + cash balances (per-account and aggregate).
    holdings: {
      title: string;
      asOf: string; // "as of {time}"
      nEvents: string; // "{n} events"
      symbol: string;
      market: string;
      qty: string;
      avgCost: string;
      currency: string;
      accounts: string; // aggregate column
      unknownCost: string; // localized "unknown"
      empty: string;
      emptyAggregate: string;
      cashTitle: string;
      cashEmpty: string;
      cashAmount: string;
      unknownCash: string;
    };
    aggregate: {
      title: string;
      riskOnly: string;
      accountsCount: string; // "{n} accounts"
    };
    // Record trade / opening position form (instrument type-ahead + confirm).
    record: {
      title: string;
      subtitle: string;
      instrument: string;
      instrumentPlaceholder: string;
      searching: string;
      noMatches: string;
      degraded: string;
      selected: string;
      clearInstrument: string;
      eventType: string;
      qty: string;
      price: string;
      priceHint: string;
      commission: string;
      occurredAt: string;
      note: string;
      notePlaceholder: string;
      submit: string;
      submitting: string;
      errInstrument: string;
      errQty: string;
      errPrice: string;
      errCommission: string;
      recorded: string; // "{symbol} recorded."
      failedDraft: string; // "Could not create the draft ({message})."
      failedConfirm: string; // "Draft created but confirmation failed ({message})."
      requestFailed: string; // "Request failed ({error})."
    };
    // Drafts review (human-confirmation surface).
    draftsView: {
      title: string;
      subtitle: string;
      empty: string;
      filterStatus: string;
      missing: string; // "Missing: {fields}"
      ambiguities: string; // "Ambiguous: {items}"
      original: string; // "From: “{text}”"
      createdBy: string; // "by {actor}"
      confirm: string;
      reject: string;
      edit: string;
      save: string;
      cancel: string;
      fields: { qty: string; price: string; commission: string; note: string };
      errQty: string;
      errPrice: string;
      errCommission: string;
      outcome: {
        requestFailed: string;
        confirmed: string; // "{symbol} confirmed"
        rejected: string; // "{symbol} rejected"
        edited: string; // "{symbol} updated"
        replayed: string;
        notHuman: string;
        incomplete: string;
        invalidEdit: string;
        terminal: string;
        versionConflict: string;
        unknown: string;
        serviceUnavailable: string;
        unexpected: string;
        unexpectedFallback: string;
      };
    };
    // CSV import (paste → preview → commit).
    import: {
      title: string;
      columns: string;
      placeholder: string;
      preview: string;
      previewing: string;
      commit: string;
      committing: string;
      emptyCsv: string;
      headerError: string; // "Header error: {message}"
      summary: string; // "{valid} valid · {invalid} invalid · {duplicate} duplicate"
      notCommittable: string;
      colLine: string;
      colStatus: string;
      colType: string;
      colSymbol: string;
      colQty: string;
      colPrice: string;
      colAmount: string;
      colErrors: string;
      rowOk: string;
      rowDuplicate: string;
      rowInvalid: string;
      committed: string; // "{committed} rows committed ({duplicate} duplicate, {skipped} skipped)."
      previewFailed: string;
      failed: string;
    };
    // Per-account reconciliation status.
    reconcile: {
      title: string;
      authority: string;
      asOf: string;
      inSync: string;
      drift: string;
      noDrift: string;
      driftSymbol: string;
      portfolioQty: string;
      brokerQty: string;
      unavailable: string;
    };
    // Activity / event ledger.
    activity: {
      title: string;
      empty: string;
      colTime: string;
      colType: string;
      colSymbol: string;
      colQty: string;
      colPrice: string;
      colAmount: string;
      colSource: string;
      colNote: string;
    };
    // Account settings (update).
    settings: {
      title: string;
      name: string;
      accountType: string;
      includeInRisk: string;
      note: string;
      save: string;
      saving: string;
      saved: string;
      failed: string;
      meta: string; // "Provider {provider} · {market} · base {currency}"
      created: string; // "Created {time}"
    };
  };
}

export interface Translations {
  // ── Common ──
  common: {
    save: string;
    saving: string;
    cancel: string;
    close: string;
    confirm: string;
    delete: string;
    refresh: string;
    retry: string;
    search: string;
    loading: string;
    create: string;
    creating: string;
    set: string;
    replace: string;
    clear: string;
    live: string;
    off: string;
    enabled: string;
    disabled: string;
    active: string;
    inactive: string;
    unknown: string;
    untitled: string;
    none: string;
    form: string;
    noResults: string;
    of: string;
    page: string;
    msgs: string;
    tools: string;
    match: string;
    other: string;
    configured: string;
    removed: string;
    failedToToggle: string;
    failedToRemove: string;
    failedToReveal: string;
    collapse: string;
    expand: string;
    general: string;
    messaging: string;
    // Optional: non-English locales fall back to the English literal in the
    // component until translated, matching the enriched-profiles keys.
    gateway?: string;
    gatewayHint?: string;
    pluginLoadFailed: string;
    pluginNotRegistered: string;
  };

  // ── App shell ──
  app: {
    brand: string;
    brandShort: string;
    closeNavigation: string;
    closeModelTools: string;
    footer: {
      org: string;
    };
    activeSessionsLabel: string;
    gatewayStatusLabel: string;
    gatewayStrip: {
      failed: string;
      off: string;
      running: string;
      starting: string;
      stopped: string;
    };
    nav: {
      analytics: string;
      chat: string;
      config: string;
      cron: string;
      documentation: string;
      finance: string;
      keys: string;
      logs: string;
      models: string;
      profiles: string;
      plugins: string;
      sessions: string;
      skills: string;
    };
    modelToolsSheetSubtitle: string;
    modelToolsSheetTitle: string;
    navigation: string;
    openDocumentation: string;
    openNavigation: string;
    pluginNavSection: string;
    sessionsActiveCount: string;
    statusOverview: string;
    system: string;
    webUi: string;
    /** Optional — fall back to English literals until translated. */
    managingProfile?: string;
    currentProfileOption?: string;
    managingProfileBanner?: string;
  };

  // ── Status page ──
  status: {
    actionFailed: string;
    actionFinished: string;
    actions: string;
    agent: string;
    connected: string;
    connectedPlatforms: string;
    disabled?: string;
    disconnected: string;
    error: string;
    failed: string;
    gateway: string;
    gatewayFailedToStart: string;
    lastUpdate: string;
    noneRunning: string;
    notRunning: string;
    pid: string;
    platformDisconnected: string;
    platformError: string;
    activeSessions: string;
    recentSessions: string;
    restartGateway: string;
    restartGatewayConfirmMessage?: string;
    restartGatewayConfirmTitle?: string;
    restartingGateway: string;
    running: string;
    runningRemote: string;
    startFailed: string;
    starting: string;
    startedInBackground: string;
    stopped: string;
    updateHermes: string;
    updateHermesConfirmMessage?: string;
    updateHermesConfirmNow?: string;
    updateHermesConfirmTitle?: string;
    updatingHermes: string;
    waitingForOutput: string;
  };

  // ── Sessions page ──
  sessions: {
    title: string;
    history: string;
    overview: string;
    searchPlaceholder: string;
    noSessions: string;
    noMatch: string;
    startConversation: string;
    noMessages: string;
    untitledSession: string;
    deleteSession: string;
    confirmDeleteTitle: string;
    confirmDeleteMessage: string;
    sessionDeleted: string;
    failedToDelete: string;
    deleteEmpty: string;
    deleteEmptyConfirmTitle: string;
    deleteEmptyConfirmMessage: string;
    emptySessionsDeleted: string;
    failedToDeleteEmpty: string;
    selectSession: string;
    selectAllOnPage: string;
    clearSelection: string;
    selectedCount: string;
    deleteSelected: string;
    deleteSelectedConfirmTitle: string;
    deleteSelectedConfirmMessage: string;
    selectedSessionsDeleted: string;
    failedToDeleteSelected: string;
    resumeInChat: string;
    newChat: string;
    previousPage: string;
    nextPage: string;
    roles: {
      user: string;
      assistant: string;
      system: string;
      tool: string;
    };
  };

  // ── Analytics page ──
  analytics: {
    period: string;
    totalTokens: string;
    totalSessions: string;
    apiCalls: string;
    dailyTokenUsage: string;
    dailyBreakdown: string;
    perModelBreakdown: string;
    topSkills: string;
    skill: string;
    loads: string;
    edits: string;
    lastUsed: string;
    input: string;
    output: string;
    total: string;
    noUsageData: string;
    startSession: string;
    date: string;
    model: string;
    tokens: string;
    perDayAvg: string;
    acrossModels: string;
    inOut: string;
  };

  // ── Models page ──
  models: {
    modelsUsed: string;
    estimatedCost: string;
    tokens: string;
    sessions: string;
    avgPerSession: string;
    apiCalls: string;
    toolCalls: string;
    noModelsData: string;
    startSession: string;
  };

  // ── Logs page ──
  logs: {
    title: string;
    autoRefresh: string;
    file: string;
    level: string;
    component: string;
    lines: string;
    noLogLines: string;
  };

  // ── Cron page ──
  cron: {
    confirmDeleteMessage: string;
    confirmDeleteTitle: string;
    newJob: string;
    nameOptional: string;
    namePlaceholder: string;
    prompt: string;
    promptPlaceholder: string;
    schedule: string;
    schedulePlaceholder: string;
    scheduleMode: string;
    scheduleModes: {
      interval: string;
      daily: string;
      weekly: string;
      monthly: string;
      once: string;
      custom: string;
      intervalEvery: string;
      intervalUnit: string;
      unitMinutes: string;
      unitHours: string;
      unitDays: string;
      timeOfDay: string;
      weekdays: string;
      weekdaysShort: [string, string, string, string, string, string, string];
      dayOfMonth: string;
      onceAt: string;
      customLabel: string;
      customPlaceholder: string;
      customHint: string;
      preview: string;
      previewEmpty: string;
    };
    scheduleDescribe: {
      none: string;
      everyMinutes: string;
      everyHours: string;
      everyDays: string;
      dailyAt: string;
      weeklyAt: string;
      monthlyAt: string;
      onceAt: string;
    };
    deliverTo: string;
    scheduledJobs: string;
    noJobs: string;
    last: string;
    next: string;
    pause: string;
    resume: string;
    triggerNow: string;
    delivery: {
      local: string;
      telegram: string;
      discord: string;
      slack: string;
      email: string;
      needsHomeChannel?: string;
      noneConfigured?: string;
    };
  };

  // ── Plugins page ──
  pluginsPage: {
    contextEngineLabel: string;
    dashboardSlots: string;
    disableRuntime: string;
    enableAfterInstall: string;
    enableRuntime: string;
    forceReinstall: string;
    headline: string;
    identifierLabel: string;
    inactive: string;
    installBtn: string;
    installHeading: string;
    installHint: string;
    memoryProviderLabel: string;
    missingEnvWarn: string;
    noDashboardTab: string;
    openTab: string;
    orphanHeading: string;
    pluginListHeading: string;
    providerDefaults: string;
    providersHeading: string;
    providersHint: string;
    refreshDashboard: string;
    removeConfirm: string;
    removeHint: string;
    rescanHeading: string;
    rescanHint: string;
    runtimeHeading: string;
    saveProviders: string;
    savedProviders: string;
    sourceBadge: string;
    authRequired: string;
    authRequiredHint: string;
    updateGit: string;
    versionBadge: string;
    showInSidebar: string;
    hideFromSidebar: string;
  };

  // ── Profiles page ──
  profiles: {
    newProfile: string;
    name: string;
    namePlaceholder: string;
    nameRequired: string;
    nameRule: string;
    invalidName: string;
    cloneFrom: string;
    cloneFromNone: string;
    allProfiles: string;
    noProfiles: string;
    defaultBadge: string;
    hasEnv: string;
    model: string;
    skills: string;
    rename: string;
    editSoul: string;
    soulSection: string;
    soulPlaceholder: string;
    saveSoul: string;
    soulSaved: string;
    openInTerminal: string;
    commandCopied: string;
    copyFailed: string;
    confirmDeleteTitle: string;
    confirmDeleteMessage: string;
    created: string;
    deleted: string;
    renamed: string;
    // Optional keys added for the enriched profiles experience. Non-English
    // locales fall back to the English literal in the component until
    // translated, so these are optional to avoid churning every locale file.
    activeProfile?: string;
    activeBadge?: string;
    setActive?: string;
    activeSet?: string;
    gatewayRunning?: string;
    gatewayStopped?: string;
    gatewayRunningWarning?: string;
    aliasBadge?: string;
    description?: string;
    descriptionPlaceholder?: string;
    noDescription?: string;
    editDescription?: string;
    descriptionSaved?: string;
    reviewBadge?: string;
    autoGenerate?: string;
    generating?: string;
    describeFailed?: string;
    distribution?: string;
    advancedOptions?: string;
    cloneAll?: string;
    noSkillsOption?: string;
    descriptionOptional?: string;
    modelOptional?: string;
    modelInherit?: string;
    modelLoading?: string;
    modelNone?: string;
    editModel?: string;
    modelSaved?: string;
    modelSelect?: string;
    actions?: string;
    manageSkills?: string;
    activeSetHint?: string;
  };

  // ── Skills page ──
  skills: {
    title: string;
    searchPlaceholder: string;
    enabledOf: string;
    all: string;
    categories: string;
    filters: string;
    noSkills: string;
    noSkillsMatch: string;
    skillCount: string;
    resultCount: string;
    noDescription: string;
    toolsets: string;
    toolsetLabel: string;
    noToolsetsMatch: string;
    setupNeeded: string;
    disabledForCli: string;
    more: string;
    /** Optional — fall back to English literals until translated. */
    profileSelector?: string;
    currentProfile?: string;
    managingProfile?: string;
  };

  // ── Config page ──
  config: {
    configPath: string;
    filters: string;
    sections: string;
    exportConfig: string;
    importConfig: string;
    resetDefaults: string;
    resetScopeTooltip: string;
    confirmResetScope: string;
    resetScopeToast: string;
    rawYaml: string;
    searchResults: string;
    fields: string;
    noFieldsMatch: string;
    configSaved: string;
    yamlConfigSaved: string;
    failedToSave: string;
    failedToSaveYaml: string;
    failedToLoadRaw: string;
    configImported: string;
    invalidJson: string;
    categories: {
      general: string;
      agent: string;
      terminal: string;
      display: string;
      delegation: string;
      memory: string;
      compression: string;
      security: string;
      browser: string;
      voice: string;
      tts: string;
      stt: string;
      logging: string;
      discord: string;
      auxiliary: string;
    };
  };

  // ── Env / Keys page ──
  env: {
    changesNote: string;
    confirmClearMessage: string;
    confirmClearTitle: string;
    description: string;
    enterValue: string;
    getKey: string;
    hideAdvanced: string;
    hideValue: string;
    keysCount: string;
    llmProviders: string;
    notConfigured: string;
    notSet: string;
    providersConfigured: string;
    replaceCurrentValue: string;
    showAdvanced: string;
    showLess: string;
    showMore: string;
    showValue: string;
    customTitle: string;
    customHint: string;
    customConfigured: string;
    addCustomKey: string;
    customKeyName: string;
    customKeyNamePlaceholder: string;
    add: string;
    invalidKeyName: string;
  };

  // ── OAuth ──
  oauth: {
    title: string;
    providerLogins: string;
    description: string;
    connected: string;
    expired: string;
    notConnected: string;
    runInTerminal: string;
    noProviders: string;
    login: string;
    disconnect: string;
    managedExternally: string;
    copied: string;
    copyCode: string;
    copyFailed: string;
    cli: string;
    copyCliCommand: string;
    connect: string;
    sessionExpires: string;
    initiatingLogin: string;
    exchangingCode: string;
    connectedClosing: string;
    loginFailed: string;
    sessionExpired: string;
    reOpenAuth: string;
    reOpenVerification: string;
    submitCode: string;
    pasteCode: string;
    waitingAuth: string;
    enterCodePrompt: string;
    pkceStep1: string;
    pkceStep2: string;
    pkceStep3: string;
    flowLabels: {
      pkce: string;
      device_code: string;
      external: string;
    };
    expiresIn: string;
  };

  // ── Language switcher ──
  language: {
    switchTo: string;
  };

  // ── Theme switcher ──
  theme: {
    title: string;
    switchTheme: string;
    /** Font-override section (optional — locales fall back to English). */
    fontTitle?: string;
    fontDefault?: string;
    fontDefaultHint?: string;
    fontSans?: string;
    fontSerif?: string;
    fontMono?: string;
  };

  // ── Achievements plugin (plugins/hermes-achievements) ──
  achievements: {
    hero: {
      kicker: string;
      title: string;
      subtitle: string;
      scan_subtitle: string;
    };
    actions: {
      rescan: string;
    };
    stats: {
      unlocked: string;
      unlocked_hint: string;
      discovered: string;
      discovered_hint: string;
      secrets: string;
      secrets_hint: string;
      highest_tier: string;
      highest_tier_hint: string;
      latest: string;
      latest_hint_empty: string;
      none_yet: string;
    };
    state: {
      unlocked: string;
      discovered: string;
      secret: string;
    };
    tier: {
      target: string;
      hidden: string;
      complete: string;
      objective: string;
    };
    progress: {
      hidden: string;
    };
    scan: {
      building_headline: string;
      building_detail: string;
      starting_headline: string;
      progress_detail: string;
      idle_detail: string;
    };
    guide: {
      tiers_header: string;
      secret_header: string;
      secret_body: string;
      scan_status_header: string;
      scan_status_body: string;
      what_scanned_header: string;
      what_scanned_body: string;
    };
    card: {
      share_title: string;
      share_label: string;
      share_text: string;
      how_to_reveal: string;
      what_counts: string;
      evidence_label: string;
      evidence_session_fallback: string;
      no_evidence: string;
    };
    latest: {
      header: string;
    };
    empty: {
      no_secrets_header: string;
      no_secrets_body: string;
    };
    filters: {
      all_categories: string;
      visibility_all: string;
      visibility_unlocked: string;
      visibility_discovered: string;
      visibility_secret: string;
    };
    share: {
      dialog_label: string;
      header: string;
      close: string;
      rendering: string;
      card_alt: string;
      error_generic: string;
      x_title: string;
      x_button: string;
      copy_title: string;
      copy_button: string;
      copied: string;
      download_button: string;
      hint: string;
      clipboard_unsupported: string;
      tweet_text: string;
    };
  };

  // ── Finance tab (research-first, Loop.md §7 Phase 0.5) ──
  // Optional: non-English locales fall back to the English catalog entry
  // in `useFinanceT()` until translated (en + zh are maintained).
  finance?: FinanceTranslations;

  // ── Kanban ──
  kanban: {
    loading: string;
    loadFailed: string;
    loadFailedHint: string;
    board: string;
    newBoard: string;
    newBoardTitle: string;
    newBoardDescription: string;
    slug: string;
    slugHint: string;
    displayName: string;
    displayNameHint: string;
    description: string;
    descriptionHint: string;
    icon: string;
    iconHint: string;
    switchAfterCreate: string;
    cancel: string;
    creating: string;
    createBoard: string;
    search: string;
    filterCards: string;
    tenant: string;
    allTenants: string;
    assignee: string;
    allProfiles: string;
    showArchived: string;
    lanesByProfile: string;
    nudgeDispatcher: string;
    refresh: string;
    selected: string;
    complete: string;
    archive: string;
    apply: string;
    clear: string;
    createTask: string;
    noTasks: string;
    unassigned: string;
    needsAssignee?: string;
    needsAssigneeHint?: string;
    untitled: string;
    loadingDetail: string;
    addComment: string;
    comment: string;
    status: string;
    workspace: string;
    skills: string;
    createdBy: string;
    result: string;
    comments: string;
    events: string;
    runHistory: string;
    workerLog: string;
    loadingLog: string;
    noWorkerLog: string;
    noDescription: string;
    noComments: string;
    edit: string;
    save: string;
    dependencies: string;
    parents: string;
    children: string;
    none: string;
    addParent: string;
    addChild: string;
    removeDependency: string;
    block: string;
    unblock: string;
    notifyHomeChannels: string;
    diagnostics: string;
    hide: string;
    show: string;
    attention: string;
    tasksNeedAttention: string;
    taskNeedsAttention: string;
    diagnostic: string;
    open: string;
    close: string;
    reassignTo: string;
    copied: string;
    copyCommand: string;
    reclaim: string;
    reassign: string;
    renderingError: string;
    reloadView: string;
    wsAuthFailed: string;
    markDone: string;
    markArchived: string;
    warning: string;
    phantomIds: string;
    active: string;
    ended: string;
    noProfile: string;
    showAllAttempts: string;
    sendingUpdates: string;
    sendNotifications: string;
    archiveBoardConfirm: string;
    archiveBoardTitle: string;
    boardSwitcherHint: string;
    taskCreatedWarning: string;
    moveFailed: string;
    bulkFailed: string;
    completionBlockedHallucination: string;
    suspectedHallucinatedReferences: string;
    pickProfileFirst: string;
    unblockedMessage: string;
    unblockFailed: string;
    reclaimedMessage: string;
    reclaimFailed: string;
    reassignedMessage: string;
    reassignFailed: string;
    selectForBulk: string;
    clickToEdit: string;
    clickToEditAssignee: string;
    emptyAssignee: string;
    columnLabels: {
      triage: string;
      todo: string;
      scheduled: string;
      ready: string;
      running: string;
      blocked: string;
      done: string;
      archived: string;
    };
    columnHelp: {
      triage: string;
      todo: string;
      scheduled: string;
      ready: string;
      running: string;
      blocked: string;
      done: string;
      archived: string;
    };
    confirmDone: string;
    confirmArchive: string;
    confirmBlocked: string;
    confirmScheduled?: string;
    completionSummary: string;
    completionSummaryRequired: string;
    triagePlaceholder: string;
    taskTitlePlaceholder: string;
    specifier: string;
    assigneePlaceholder: string;
    priority: string;
    skillsPlaceholder: string;
    noParent: string;
    workspacePathDir: string;
    workspacePathOptional: string;
    logTruncated: string;
    logAt: string;
  };
}
