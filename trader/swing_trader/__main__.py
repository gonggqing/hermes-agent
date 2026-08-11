"""CLI entry point.

- ``python -m swing_trader simulate --days 22``   offline E2E paper demo
- ``python -m swing_trader serve``                real Phase-0 paper loop +
  Finance service API on :9319 (the Hermes dashboard proxies /api/finance/*
  here; Telegram attaches when TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set).

Paper positions, resting orders, fills and latest market marks are rehydrated
from the durable ledger when ``serve`` restarts.
"""

from __future__ import annotations

import argparse
import threading

from swing_trader.config import load_settings
from swing_trader.log import get_logger, setup_logging

logger = get_logger(__name__)


def _runtime_brief(runtime, market: str):
    key = market.lower()
    return runtime.latest_brief if key == "us" else runtime.latest_briefs.get(key)


def _restore_latest_briefs(runtime, markets=("us", "hk", "cn", "kr")) -> list[str]:
    """Hydrate volatile FinanceRuntime slots from the durable brief archive."""
    store = getattr(runtime, "brief_store", None)
    restored: list[str] = []
    if store is None:
        return restored
    for market in markets:
        key = market.lower()
        try:
            payload = store.get_latest(key)
        except Exception:  # archive damage must not prevent service startup
            logger.warning("brief restore failed", extra={"market": key})
            continue
        if not payload:
            continue
        if key == "us":
            runtime.latest_brief = payload
        else:
            runtime.latest_briefs[key] = payload
            if key == "cn":
                runtime.latest_brief_cn = payload
        restored.append(key)
    if restored:
        logger.info("research briefs restored", extra={"markets": restored})
    return restored


def _markets_missing_today(runtime, market_timezones: dict[str, str]) -> list[str]:
    """Markets needing startup evidence refresh.

    A current 09:00/21:00 narrative wins over a market-local date rollover. For
    example, Beijing morning is already the next US calendar date even though
    the latest useful US evidence is still the prior close. Re-fetching there
    would erase/rebuild a valid scheduled edition and spend four primary-model
    calls after every midday container restart.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from swing_trader.brief_cycle import latest_due_brief_slot

    missing = []
    now = runtime.clock()
    _, due = latest_due_brief_slot(now)
    for market, tz_name in market_timezones.items():
        payload = _runtime_brief(runtime, market) or {}
        narrative = payload.get("narrative")
        generated_raw = narrative.get("generated_at") if isinstance(narrative, dict) else None
        try:
            generated = datetime.fromisoformat(str(generated_raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            generated = None
        if (
            generated is not None
            and generated.tzinfo is not None
            and generated >= due.astimezone(generated.tzinfo)
        ):
            continue
        today = now.astimezone(ZoneInfo(tz_name)).date().isoformat()
        if str(payload.get("trading_date") or "") != today:
            missing.append(market)
    return missing


def _backfill_brief_narratives(runtime, writer, markets) -> list[str]:
    """Add model-written analysis to volatile restored briefs after upgrade.

    Older archives intentionally remain valid without ``narrative``.  On the
    first startup with the writer enabled, enrich only briefs that do not need
    a full market-data refresh; this avoids leaving today's page blank until
    the next scheduled research event and avoids fetching Yahoo data twice.
    This compatibility enrichment is deliberately NOT archived or scored;
    only BriefCycleCoordinator publishes canonical morning/evening editions.
    """

    from swing_trader.brief import ResearchBrief

    labels = {
        "us": "United States",
        "cn": "Mainland China",
        "hk": "Hong Kong",
        "kr": "Korea semiconductors",
    }
    enriched: list[str] = []
    for raw_market in markets:
        market = raw_market.lower()
        payload = _runtime_brief(runtime, market)
        if not payload or payload.get("narrative") is not None:
            continue
        try:
            brief = ResearchBrief.model_validate(payload)
            narrative = writer.write(
                brief,
                market_id=market.upper(),
                market_label=labels.get(market, market.upper()),
                language="zh-CN",
            )
            if narrative is None:
                continue
            brief.narrative = narrative
            dump = brief.model_dump(mode="json")
            if market == "us":
                runtime.latest_brief = dump
            else:
                runtime.latest_briefs[market] = dump
                if market == "cn":
                    runtime.latest_brief_cn = dump
            enriched.append(market)
        except Exception:
            logger.exception("restored brief narrative backfill failed", extra={"market": market})
    return enriched


def _cmd_simulate(args: argparse.Namespace) -> None:
    from swing_trader.schemas import Mode
    from swing_trader.simulate import run_simulation

    result = run_simulation(
        n_days=args.days,
        db_url=f"sqlite:///{args.db}",
        crash_day=args.crash_day,
    )
    stats = result.ledger.stats(Mode.PAPER)
    account = result.broker.get_account()
    print(f"simulated {len(result.days)} trading days ({result.days[0]} → {result.days[-1]})")
    print(f"final equity: {account.equity:.2f} (cash {account.cash:.2f})")
    print(
        f"closed trades: {stats.n_closed}  win rate: {stats.win_rate:.0%}  "
        f"expectancy: {stats.expectancy:.2f}  max DD: {stats.max_drawdown_pct:.2f}%"
    )
    print(f"ledger: {args.db}")
    if result.morning_reports:
        print("\n--- last morning report ---\n")
        print(result.morning_reports[-1])


def _cmd_serve(args: argparse.Namespace) -> None:
    from pathlib import Path

    import uvicorn
    from dotenv import load_dotenv

    from swing_trader.api import FinanceRuntime, create_app
    from swing_trader.dailyloop import DailyLoop, TelegramSurfaceAdapter
    from swing_trader.datafeed import RetryingFeed, YFinanceFeed
    from swing_trader.ledger import Ledger
    from swing_trader.llm import LLMAnalyst, llm_settings_from_env
    from swing_trader.scheduler import (
        BEIJING_BRIEF_SCHEDULE,
        DailyLoopRunner,
        Event,
    )
    from swing_trader.telegram_gateway import HttpTransport

    # ~/.hermes/.env is the SINGLE source of truth for secrets (Telegram
    # token + finance group chat id, DeepSeek/GLM keys) — shared with the
    # Hermes gateway/dashboard. Loaded into the process env first, so these
    # values take precedence over any optional local trader/.env overrides.
    load_dotenv(Path.home() / ".hermes" / ".env", override=False)
    settings = load_settings()

    from swing_trader.broker_factory import build_broker
    from swing_trader.rehydrate import rehydrate_from_ledger

    db_url = f"sqlite:///{args.db or settings.db_path}"
    ledger = Ledger(url=db_url)
    # Broker selection (Loop.md §5.1): paper by default; ibkr once the account
    # arrives. The factory derives the paper/live ACCOUNT flag from the triple
    # gate (HUMAN_CONFIRM && BROKER!=paper && !DRY_RUN) and refuses a live IBKR
    # port under an un-gated config — so a partial config fails closed here, not
    # at order time. Live *orders* stay separately gated in the ExecutionEngine.
    try:
        broker = build_broker(
            settings,
            starting_cash=args.starting_cash,
            starting_cash_hkd=16_000.0,
        )
    except ImportError as exc:  # ib_async not installed (pip install '.[ibkr]')
        raise SystemExit(
            f"BROKER=ibkr but ib_async is not installed: {exc}. "
            "Install it with: pip install 'swing-trader[ibkr]'"
        ) from exc
    print(
        f"broker: {settings.broker.value} "
        f"(live orders {'ALLOWED' if settings.live_orders_allowed else 'blocked'})",
        flush=True,
    )
    rehydration = rehydrate_from_ledger(broker, ledger, settings.mode)
    print(rehydration.summary(), flush=True)
    # Phase 0.8 (resilience): wrap the live feed in RetryingFeed so transient
    # yfinance errors (rate limits / network blips) retry with backoff instead
    # of surfacing as a hard DataFeedError to the loop and /v1/analyze.
    # The on-demand API and US loop share this feed.  Prefer Yahoo's bounded
    # query2 chart endpoint so K-line requests do not wait on yfinance's
    # crumb/cookie path; yfinance remains the adapter's fallback.
    api_feed = YFinanceFeed(prefer_chart=True, chart_only=True)
    feed = RetryingFeed(YFinanceFeed(prefer_chart=True))
    # Real fundamentals (Loop.md Phase 0.75 thrust A): yfinance-backed, cached,
    # fail-None. Feeds the scheduled FundamentalAgent AND on-demand /v1/analyze.
    from swing_trader.earnings import YFinanceEarnings
    from swing_trader.fundamentals import YFinanceFundamentals

    fundamentals = YFinanceFundamentals()
    earnings_provider = YFinanceEarnings()  # earnings calendar (Phase 0.75)
    runtime = FinanceRuntime(ledger=ledger, broker=broker, mode=settings.mode)
    # On-demand market analysis for the conversational agent (thrust B):
    # UI/chat reads are bounded to query2 (the dashboard proxy has a 15s
    # timeout); scheduled monitors keep the retrying yfinance fallback above.
    runtime.feed = api_feed
    runtime.fundamentals = fundamentals
    # Manual kill-switch (Loop.md §3 / Phase 0.95): a filesystem HALT flag next
    # to the DB. Engaged → the loop's dead-man's switch vetoes NEW entries (the
    # RiskEngine enforces it); exits/protection are untouched. Survives restart
    # and is `touch`-able out-of-band if the service wedges.
    from swing_trader.killswitch import KillSwitch, kill_switch_path

    kill_switch = KillSwitch(kill_switch_path(args.db or settings.db_path), clock=runtime.clock)
    runtime.kill_switch = kill_switch
    if kill_switch.engaged():
        print(
            f"⚠️  KILL-SWITCH ENGAGED at startup: {kill_switch.state().reason or '(no reason)'}",
            flush=True,
        )
    # Phase 0.9 (portfolio): instrument type-ahead behind a cached, offline
    # provider (a live adapter can slot behind the same port later).
    # Append-only Portfolio Journal + human-confirmation draft service, sharing
    # the ledger's DB file but none of its tables (Loop.md P0.9 boundary #1).
    from swing_trader.portfolio_draft import PortfolioDraftService
    from swing_trader.portfolio_controls import PortfolioControlStore
    from swing_trader.portfolio_journal import PortfolioJournal
    from swing_trader.risk import RiskParams

    runtime.portfolio = PortfolioJournal(url=db_url)
    runtime.portfolio_controls = PortfolioControlStore(url=db_url)
    portfolio_controls = runtime.portfolio_controls.get()
    portfolio_risk_params = RiskParams(
        per_trade_risk_pct=portfolio_controls.per_trade_risk_pct,
        max_new_entries_per_day=portfolio_controls.max_new_positions_per_day,
        max_invested_pct=portfolio_controls.invested_ceiling_pct,
        max_agent_managed_pct=portfolio_controls.agent_ceiling_pct,
        max_position_pct=portfolio_controls.max_position_pct,
    )
    # Personal research groups share the durable Finance DB but have their own
    # tables and never flow into the trading watchlist/candidate pipeline.
    from swing_trader.research_watchlists import ResearchWatchlistStore

    runtime.research_watchlists = ResearchWatchlistStore(url=db_url)
    if broker.get_account().mode.value == "paper":
        paper_account = runtime.portfolio.ensure_default_paper_account()
        logger.info(
            "default paper portfolio account ready",
            extra={"account_id": paper_account.id, "account_name": paper_account.name},
        )
    runtime.portfolio_drafts = PortfolioDraftService(runtime.portfolio, clock=runtime.clock)
    # Durable research-brief history (own DB file, own MetaData — never the
    # ledger's tables): archives each published brief and hydrates the volatile
    # runtime cache immediately after a rebuild.
    from pathlib import Path as _DbPath

    from swing_trader.brief_store import BriefStore

    _briefs_url = f"sqlite:///{_DbPath(args.db or settings.db_path).parent / 'briefs.db'}"
    runtime.brief_store = BriefStore(url=_briefs_url)
    from swing_trader.prediction_ledger import PredictionLedger, backfill_brief_history

    _predictions_url = (
        f"sqlite:///{_DbPath(args.db or settings.db_path).parent / 'prediction_evaluation.db'}"
    )
    runtime.prediction_ledger = PredictionLedger(url=_predictions_url)
    from swing_trader.prediction_evaluator import PredictionCloseEvaluator

    runtime.prediction_evaluator = PredictionCloseEvaluator(
        runtime.prediction_ledger,
        feed,
        clock=runtime.clock,
    )
    prediction_backfill = backfill_brief_history(
        runtime.brief_store, runtime.prediction_ledger
    )
    logger.info(
        "prediction history backfilled",
        extra={
            "snapshots": prediction_backfill.snapshots,
            "new_runs": prediction_backfill.new_runs,
            "replayed_runs": prediction_backfill.replayed_runs,
            "revisions": prediction_backfill.revisions,
            "unchanged_revisions": prediction_backfill.unchanged_revisions,
        },
    )
    _restore_latest_briefs(runtime)
    # User-set display-name overrides (finance-bot DM "改名"): own DB, highest
    # precedence in _symbol_names. Lets the user fix a name with no code deploy.
    from swing_trader.name_override import NameOverrideStore

    _names_url = f"sqlite:///{_DbPath(args.db or settings.db_path).parent / 'name_overrides.db'}"
    runtime.name_overrides = NameOverrideStore(url=_names_url)
    # Instrument type-ahead: the curated static catalog for discovery PLUS the
    # user's actually-held instruments (searchable by code or note keyword, so a
    # held Chinese fund/ETF is always findable — Loop.md P0.9).
    from swing_trader.instruments import (
        CachedInstrumentSearch,
        CompositeInstrumentProvider,
        EastmoneyInstrumentProvider,
        PortfolioInstrumentProvider,
        StaticInstrumentProvider,
        YFinanceInstrumentProvider,
    )

    runtime.instrument_search = CachedInstrumentSearch(
        CompositeInstrumentProvider(
            [
                StaticInstrumentProvider(),
                PortfolioInstrumentProvider(runtime.portfolio),
                EastmoneyInstrumentProvider(),
                YFinanceInstrumentProvider(),
            ]
        )
    )
    # 场外基金 NAV (Loop.md P0.9 #41): real net-asset-value for open-end funds so
    # they can be valued (market value + P&L) instead of showing 未知.
    from swing_trader.fund_nav import CachedNavProvider, EastmoneyFundNav

    runtime.nav_provider = CachedNavProvider(EastmoneyFundNav())
    # Real domestic gold (SGE Au99.99) so the chart's 国内金价 can use a real
    # spot instead of the derived GC=F×CNY value (#41).
    from swing_trader.sge_gold import CachedGoldProvider, SinaSgeGold

    runtime.gold_provider = CachedGoldProvider(SinaSgeGold())

    telegram = None
    notify = None
    import os

    from pydantic import SecretStr

    # Two bots, DISTINCT roles in the shared group (Loop.md two-session
    # extension). Both post to the same chat id.
    #   REPORTER  = shared gateway bot (TELEGRAM_BOT_TOKEN), OUTBOUND-ONLY:
    #               daily summaries + CN/US research briefs. Never long-polls
    #               (a second getUpdates consumer 409-kicks the Hermes gateway).
    #   GATEKEEPER = dedicated finance bot (FINANCE_TELEGRAM_BOT_TOKEN),
    #               INTERACTIVE: only asks for approval; long-polls its OWN
    #               token so it never conflicts with the gateway.
    chat_id = settings.telegram_chat_id
    shared_token = (
        settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    ).strip()
    dedicated_token = (
        settings.finance_telegram_bot_token.get_secret_value()
        if settings.finance_telegram_bot_token
        else os.environ.get("FINANCE_TELEGRAM_BOT_TOKEN", "")
    ).strip()
    allowed = {u.strip() for u in settings.telegram_allowed_users.split(",") if u.strip()}

    if chat_id and (shared_token or dedicated_token):
        # Reporter prefers the shared gateway token; falls back to the finance
        # token only if the gateway one is absent.
        report_transport = HttpTransport(SecretStr(shared_token or dedicated_token))

        def notify(text: str) -> None:
            report_transport.send_message(chat_id, text)

        logger.info(
            "reporter bot attached (outbound-only)", extra={"using_shared": bool(shared_token)}
        )
    else:
        logger.warning("no reporter bot configured; reports go to logs only")

    if chat_id and dedicated_token:
        approval_transport = HttpTransport(SecretStr(dedicated_token))
        telegram = TelegramSurfaceAdapter(
            approval_transport,
            chat_id,
            interactive=True,
            allowed_users=allowed,
            candidate_account_label=(
                "IBHK Paper（模拟盘）" if settings.mode.value == "paper" else "IBKR（实盘）"
            ),
        )
        # Let tapped draft cards confirm/reject real-holdings drafts IN Telegram
        # (Loop.md P0.9 boundary #4: authenticated human confirms, LLM never).
        telegram.set_draft_service(runtime.portfolio_drafts)
        logger.info(
            "finance gatekeeper bot attached (interactive approvals)",
            extra={"n_allowed_users": len(allowed)},
        )
    else:
        logger.warning("no dedicated finance bot; approvals via portal only")

    # Give the API thread a handle to push draft cards on create (None when no
    # interactive bot; the push is a guarded no-op then).
    runtime.telegram = telegram

    # Knowledge store (Loop.md §5.10 / Phase 0.5): embedded Qdrant under
    # trader/data/knowledge by default; FINANCE_QDRANT_URL switches to the
    # hermes-finance-vector container. Vector down => (knowledge, None) and
    # research search fails closed while facts/documents keep working.
    from pathlib import Path as _Path

    from swing_trader.knowledge import (
        OPENAI_COLLECTION_NAME,
        OPENAI_EMBEDDING_DIM,
        OPENAI_EMBEDDING_MODEL,
        OpenAIEmbedder,
    )
    from swing_trader.knowledge_pipeline import KnowledgeConfig, build_knowledge
    from swing_trader.vector_migration import migrate_document_store

    openai_embedding_key = os.environ.get("OPENAI_API_KEY", "").strip()
    embedding_profile = "hashing-blake2b-v1"
    knowledge_config = KnowledgeConfig(
        root_dir=_Path("data/knowledge"),
        qdrant_url=os.environ.get("FINANCE_QDRANT_URL") or None,
    )
    if openai_embedding_key:
        knowledge_config.collection = OPENAI_COLLECTION_NAME
        knowledge_config.embedder = OpenAIEmbedder(
            openai_embedding_key,
            model=OPENAI_EMBEDDING_MODEL,
            dimensions=OPENAI_EMBEDDING_DIM,
        )
        embedding_profile = f"{OPENAI_EMBEDDING_MODEL}:{OPENAI_EMBEDDING_DIM}"

    knowledge, knowledge_index = build_knowledge(
        knowledge_config
    )
    vector_migration = None
    if knowledge_index is not None:
        try:
            source_documents = knowledge.documents.count()
            indexed_documents = knowledge_index.count()
            if indexed_documents != source_documents:
                vector_migration = migrate_document_store(
                    knowledge.documents,
                    knowledge_index,
                    batch_size=32,
                )
                if not vector_migration.verified:
                    raise RuntimeError("vector migration verification failed")
        except Exception as exc:
            # A partial collection must never masquerade as complete research.
            # Keep authoritative documents/facts online but disable retrieval.
            logger.warning(
                "knowledge vector synchronization failed — semantic search disabled",
                extra={"embedding_profile": embedding_profile, "error": str(exc)},
            )
            knowledge = knowledge.storage_only()
            knowledge_index = None
    logger.info(
        "knowledge store ready",
        extra={
            "vector_ok": knowledge_index is not None,
            "embedding_profile": embedding_profile,
            "collection": knowledge_config.collection,
            "migration": vector_migration.to_dict() if vector_migration else None,
        },
    )

    search_llm_settings = llm_settings_from_env(role="search")
    brief_llm_settings = llm_settings_from_env(role="decision")
    llm_analyst = LLMAnalyst(search_llm_settings) if search_llm_settings else None
    if brief_llm_settings:
        from swing_trader.candidate_review import LLMCandidateReviewer

        order_reviewer = LLMCandidateReviewer(brief_llm_settings)
    else:
        order_reviewer = None
    if brief_llm_settings:
        from dataclasses import replace

        from swing_trader.brief_writer import ResearchBriefWriter

        brief_writer = ResearchBriefWriter(
            replace(
                brief_llm_settings,
                # Primary reasoning models need materially longer than the
                # compact search/extraction tier to synthesize a cited 4-7
                # section report. This runs on the isolated brief worker, so
                # the larger bound cannot delay Telegram or order execution.
                timeout=max(180.0, brief_llm_settings.timeout),
            ),
            history_loader=lambda market, before, limit: (
                runtime.brief_store.get_recent_distinct(
                    market,
                    before_generated_at=before,
                    limit=limit,
                )
                if runtime.brief_store is not None
                else []
            ),
        )
    else:
        brief_writer = None
    if llm_analyst:
        logger.info(
            "llm routing enabled",
            extra={
                "search_model": search_llm_settings.model,
                "brief_model": brief_llm_settings.model if brief_llm_settings else None,
            },
        )
    else:
        logger.info("no LLM key found; rule-based analysis only")

    if telegram is not None and search_llm_settings is not None:
        from swing_trader.candidate_reply import CandidateReasonSummarizer

        telegram.set_candidate_reasoner(
            CandidateReasonSummarizer(search_llm_settings).summarize
        )

    runtime.knowledge = knowledge
    runtime.knowledge_index = knowledge_index
    runtime.llm_analyst = llm_analyst  # optional voice for on-demand /v1/analyze

    # Finance bot (GATEKEEPER) text replies: it stays quiet in the group except
    # for confirmations, and answers ONLY on a DM or an @mention (human
    # directive). A mentioned/DMed ticker gets a quick multi-agent read; no
    # ticker gets brief guidance. Read-only — no order/approve here (Loop.md §3).
    if telegram is not None:

        def _finance_responder(text: str):
            from swing_trader.on_demand import (
                analyze_symbol,
                extract_symbols,
                render_analysis_zh,
            )

            syms = extract_symbols(text)
            if not syms:
                return (
                    "我是财经确认机器人 📈 —— 发我一个股票代码（如 NVDA、0700.HK、"
                    "600519.SS）即可获得快速多因子分析。完整对话/分析请 @ 主机器人；"
                    "下单需在门户人工确认。"
                )
            sym = syms[0]
            try:
                result = analyze_symbol(
                    feed,
                    sym,
                    fundamentals=fundamentals,
                    llm_analyst=llm_analyst,
                    knowledge=knowledge,
                    knowledge_index=knowledge_index,
                    now=runtime.clock(),
                )
            except Exception:
                return f"没找到 {sym} 的行情数据，换个代码试试？"
            return render_analysis_zh(result)

        telegram.set_text_responder(_finance_responder)
        # Recording (记账) is the finance bot's job (user decision 2026-07-14):
        # a DM trade message ("卖了 159813 200股 @1.893") → draft → confirm card
        # in the DM. Analysis DMs still fall through to _finance_responder above.
        from swing_trader.trade_record import make_trade_recorder
        from swing_trader.trade_llm import LLMTradeExtractor

        if search_llm_settings:
            from dataclasses import replace

            # Reasoning-capable high-speed models may spend several seconds in
            # <think> before their small JSON answer. This is a stateless,
            # infrequent human DM path; give it enough room and retry once.
            trade_settings = replace(
                search_llm_settings,
                timeout=max(30.0, search_llm_settings.timeout),
            )
            trade_extractor = LLMTradeExtractor(trade_settings, attempts=2)
        else:
            trade_extractor = None
        if trade_extractor is None:
            logger.warning("finance trade extraction disabled: no high-speed LLM configured")
        telegram.set_trade_recorder(
            make_trade_recorder(runtime, extractor=trade_extractor, require_llm=True)
        )

        # DM "/" command menu (/持仓 /研究 /记账 /帮助) + update-holdings:
        # 改名/现价 apply immediately (display override / manual mark); 成本/数量/
        # 账户 are FINANCIAL corrections that go through a confirm card (built
        # next — until then they get a clear "coming" reply, never a bad guess).
        from swing_trader.finance_commands import (
            COMMAND_MENU,
            make_command_handler,
        )
        from swing_trader.update_holdings import market_currency, parse_update

        telegram.set_command_handler(make_command_handler(runtime))

        def _holding_accounts(symbol: str) -> list:
            j = runtime.portfolio
            return [
                ac
                for ac in j.list_accounts()
                if any(h.symbol == symbol for h in j.holdings(ac.id).holdings)
            ]

        def _match_account(name: str):
            for ac in runtime.portfolio.list_accounts():
                if name and (
                    ac.name == name or name in ac.name or (len(name) >= 2 and name[:2] in ac.name)
                ):
                    return ac
            return None

        def _update_handler(text: str):
            parsed = parse_update(text)
            if parsed is None:
                return None
            field, symbol, value = parsed
            if field == "name":
                runtime.name_overrides.set(symbol, value)
                return f"✅ 已把 {symbol} 的显示名改为「{value}」（成本/历史不变）"
            if field == "mark":
                try:
                    price = float(value)
                except ValueError:
                    return f"没看懂价格 {value!r}，例如「{symbol} 现价 1.15」"
                _, ccy = market_currency(symbol)
                runtime.portfolio.set_mark(
                    symbol,
                    price,
                    currency=ccy,
                    source="manual",
                    actor="telegram",
                    as_of=runtime.clock(),
                )
                return (
                    f"✅ 已把 {symbol} 现价标记为 {price:g} {ccy}（用于市值/盈亏显示，不影响成本）"
                )
            # cost / qty / account → financial correction via a confirm card
            holders = _holding_accounts(symbol)
            if len(holders) != 1:
                where = "当前无持仓" if not holders else "在多个账户持有"
                return f"{symbol} {where}，成本/数量/账户更正请在门户处理或指明账户。"
            from_acct = holders[0]
            drafts = runtime.portfolio_drafts
            if field == "account":
                tgt = _match_account(value)
                if tgt is None:
                    return f"没找到目标账户「{value}」。"
                r = drafts.propose_restate(
                    account_id=from_acct.id,
                    symbol=symbol,
                    field="account",
                    target_account_id=tgt.id,
                    original_text=text,
                )
                label = tgt.name
            else:
                try:
                    num = float(value)
                except ValueError:
                    return f"没看懂数值 {value!r}。"
                r = drafts.propose_restate(
                    account_id=from_acct.id,
                    symbol=symbol,
                    field=field,
                    value=num,
                    original_text=text,
                )
                label = from_acct.name
            if not r.ok:
                return r.message  # refusal (未知价格 / 数量<=0 / 目标账户无效 …)
            telegram.push_draft_card(r.draft, account_label=label)
            return "📝 已生成更正草稿，请核对下方卡片并确认 ✅ / 拒绝 ❌"

        telegram.set_update_handler(_update_handler)
        telegram.register_commands(COMMAND_MENU)
    from swing_trader.discovery import (
        CompositeDiscoveryUniverse,
        DiscoveryTheme,
        JsonDiscoveryUniverse,
        KnowledgeDiscoveryUniverse,
        MarketDiscoveryScanner,
    )

    discovery_universe = CompositeDiscoveryUniverse(
        [
            JsonDiscoveryUniverse(
                _DbPath(args.db or settings.db_path).parent / "discovery-seeds.json"
            ),
            KnowledgeDiscoveryUniverse(
                knowledge,
                runtime.instrument_search,
                themes=[
                    DiscoveryTheme(
                        name="robotics-and-embodied-ai",
                        query="robotics embodied AI supply chain technical barrier orders capex",
                    ),
                    DiscoveryTheme(
                        name="ai-infrastructure",
                        query="AI infrastructure supply chain bottleneck capacity capex orders",
                    ),
                    DiscoveryTheme(
                        name="power-and-cooling",
                        query="data center power cooling grid supply chain capacity orders",
                    ),
                    DiscoveryTheme(
                        name="semiconductor-enablers",
                        query="semiconductor equipment materials packaging supply chain technical barrier",
                    ),
                    DiscoveryTheme(
                        name="biotechnology-platforms",
                        query="biotechnology platform manufacturing supply chain clinical catalyst moat",
                    ),
                ],
            ),
        ]
    )
    from swing_trader.portfolio_research import portfolio_research_holdings

    def _research_holdings(market: str):
        try:
            return portfolio_research_holdings(
                runtime.portfolio,
                market,
                name_overrides=runtime.name_overrides,
            )
        except Exception:
            logger.exception(
                "real portfolio research projection failed",
                extra={"market": market},
            )
            return []

    loop = DailyLoop(
        feed,
        broker,
        ledger,
        mode=settings.mode,
        live_orders_allowed=settings.live_orders_allowed,
        risk_params=portfolio_risk_params,
        runtime=runtime,
        telegram=telegram,
        notify=notify,
        fundamentals=fundamentals,  # real fundamentals for the scheduled loop
        earnings_provider=earnings_provider,  # earnings calendar (Phase 0.75)
        llm_analyst=llm_analyst,
        order_reviewer=order_reviewer,
        # Production approvals always require the fresh primary-model review;
        # a missing provider therefore fails closed instead of silently using
        # the first approval at cutoff.
        order_review_required=True,
        knowledge=knowledge,
        knowledge_index=knowledge_index,
        kill_switch=kill_switch,  # Phase 0.95 manual HALT
        discovery_scanner=MarketDiscoveryScanner(
            feed,
            discovery_universe,
            benchmark_symbol="SPY",
            min_adv=5_000_000,
            clock=runtime.clock,
        ),
        holdings_provider=_research_holdings,
    )
    runtime.apply_portfolio_controls = loop.apply_portfolio_controls
    if rehydration.performed:
        loop.execution.seed_synced_fills(rehydration.fill_ids)
        loop.execution.seed_protective_stops(broker.get_orders(active_only=True))
    runner = DailyLoopRunner(loop.callbacks(), clock=runtime.clock)
    # Manual missed-session catch-up (Loop.md §4b): expose the trading session's
    # run + finalize so /v1/session/* can trigger them on demand (human-gated).
    runtime.run_session = loop.run_session_now
    runtime.finalize_session = loop.finalize_session_now
    runtime.run_session_by_market["us"] = loop.run_session_now
    runtime.finalize_session_by_market["us"] = loop.finalize_session_now
    runtime.execution = loop.execution  # Phase 0.95: /orders/cancel-all
    runtime.run_research["us"] = loop.run_research_now

    # CN MORNING research session (Loop.md two-session extension): a lighter,
    # technology-focused research brief on the China/HK market, on the CN
    # calendar/clock. Report-only — NO orders — so it never touches the broker,
    # confirmation service, or ledger; it just publishes a brief the REPORTER
    # bot sends and the Finance tab shows (?market=cn).
    cn_runner = None
    cn_session = None
    if settings.cn_session_enabled:
        from zoneinfo import ZoneInfo

        from swing_trader.cn_watchlist import (
            CN_MAINLAND_INDEX_SYMBOLS,
            build_mainland_watchlist,
        )
        from swing_trader.research_session import ResearchSession
        from swing_trader.scheduler import CN_SCHEDULE

        cn_wl = build_mainland_watchlist(settings.cn_symbols)
        cn_feed = RetryingFeed(YFinanceFeed())
        cn_session = ResearchSession(
            market_id="CN",
            market_label="Mainland China",
            feed=cn_feed,
            ledger=ledger,  # never read (research-only); satisfies brief signature
            symbols=cn_wl.symbols,
            watchlist_lookup=cn_wl.lookup,
            trading_tz=ZoneInfo(settings.cn_market_tz),
            index_symbols=list(CN_MAINLAND_INDEX_SYMBOLS),
            mode=settings.mode,
            runtime=runtime,
            notify=notify,  # REPORTER bot (outbound-only)
            llm_analyst=(
                LLMAnalyst(search_llm_settings) if search_llm_settings else None
            ),
            knowledge=knowledge,
            knowledge_index=knowledge_index,
            focus_note="聚焦科技: 半导体 / 电子 / AI (其他板块仅作参考)",
            lang="zh",
            clock=runtime.clock,
            discovery_scanner=MarketDiscoveryScanner(
                cn_feed,
                discovery_universe,
                benchmark_symbol="000001.SS",
                min_adv=10_000_000,
                clock=runtime.clock,
            ),
            holdings_provider=_research_holdings,
        )
        cn_runner = DailyLoopRunner(
            {
                event: callback
                for event, callback in cn_session.callbacks().items()
                if event is not Event.PUSH_CANDIDATES
            },
            clock=runtime.clock,
            schedule=CN_SCHEDULE,
        )
        runtime.run_research["cn"] = cn_session.run_now  # manual refresh button
        logger.info("cn research session enabled", extra={"n_symbols": len(cn_wl.symbols)})

    # HK is independent from mainland CN: own universe, indices, calendar,
    # freshness and persisted brief. It remains research-only.
    hk_runner = None
    hk_session = None
    hk_loop = None
    if settings.hk_session_enabled:
        from zoneinfo import ZoneInfo

        from swing_trader.hk_watchlist import HK_INDEX_SYMBOLS, build_hk_watchlist
        from swing_trader.research_session import ResearchSession
        from swing_trader.scheduler import HK_SCHEDULE, HK_TRADING_SCHEDULE

        hk_wl = build_hk_watchlist(settings.hk_symbols)
        hk_feed = RetryingFeed(YFinanceFeed())
        if settings.hk_orders_enabled:
            # ORDER-CAPABLE HK session on the SHARED (HKD-funded) broker + ledger,
            # behind the SAME §3 boundaries as US: RiskEngine authoritative,
            # human confirms, DAY-entry fill-chain lifecycle, SEHK tick/lot/fee
            # rules. Its own 10:30-11:30 Asia/Hong_Kong window (HK_TRADING_SCHEDULE)
            # sits ~12h from the US window, so the single Telegram poller and the
            # per-candidate service registry route each market's cards correctly;
            # the market discriminator keeps US and HK candidates from colliding.
            hk_loop = DailyLoop(
                hk_feed,
                broker,
                ledger,
                mode=settings.mode,
                market_id="HK",
                schedule=HK_TRADING_SCHEDULE,
                live_orders_allowed=settings.live_orders_allowed,
                risk_params=portfolio_risk_params,
                symbols=hk_wl.symbols,
                # HK-native market regime: Hang Seng indices + HK volatility
                # index, not the US SPY/QQQ/^VIX tape.
                index_symbols=list(HK_INDEX_SYMBOLS),
                anchor_symbol="^HSI",
                vix_symbol="",  # no reliable HK vol index on the free feed; regime uses ^HSI trend
                runtime=runtime,
                telegram=telegram,
                notify=notify,
                fundamentals=fundamentals,
                earnings_provider=earnings_provider,
                llm_analyst=llm_analyst,
                order_reviewer=order_reviewer,
                order_review_required=True,
                knowledge=knowledge,
                knowledge_index=knowledge_index,
                kill_switch=kill_switch,
                discovery_scanner=MarketDiscoveryScanner(
                    hk_feed,
                    discovery_universe,
                    benchmark_symbol="^HSI",
                    min_adv=5_000_000,
                    clock=runtime.clock,
                ),
                holdings_provider=_research_holdings,
            )
            hk_runner = DailyLoopRunner(
                hk_loop.callbacks(), clock=runtime.clock,
                schedule=HK_TRADING_SCHEDULE,
            )
            runtime.run_research["hk"] = hk_loop.run_research_now
            runtime.run_session_by_market["hk"] = hk_loop.run_session_now
            runtime.finalize_session_by_market["hk"] = hk_loop.finalize_session_now
            runtime.order_capable_markets.add("hk")  # UI drops research-only badge
            logger.info(
                "hk ORDER-CAPABLE session enabled (paper)",
                extra={"n_symbols": len(hk_wl.symbols)},
            )
        else:
            hk_session = ResearchSession(
                market_id="HK",
                market_label="Hong Kong",
                feed=hk_feed,
                ledger=ledger,
                symbols=hk_wl.symbols,
                watchlist_lookup=hk_wl.lookup,
                trading_tz=ZoneInfo(settings.hk_market_tz),
                index_symbols=list(HK_INDEX_SYMBOLS),
                mode=settings.mode,
                runtime=runtime,
                notify=notify,
                llm_analyst=(
                    LLMAnalyst(search_llm_settings) if search_llm_settings else None
                ),
                knowledge=knowledge,
                knowledge_index=knowledge_index,
                focus_note="香港独立研究: 科技 / 平台 / 半导体供应链",
                lang="zh",
                clock=runtime.clock,
                discovery_scanner=MarketDiscoveryScanner(
                    hk_feed,
                    discovery_universe,
                    benchmark_symbol="^HSI",
                    min_adv=5_000_000,
                    clock=runtime.clock,
                ),
                holdings_provider=_research_holdings,
            )
            hk_runner = DailyLoopRunner(
                {
                    event: callback
                    for event, callback in hk_session.callbacks().items()
                    if event is not Event.PUSH_CANDIDATES
                },
                clock=runtime.clock,
                schedule=HK_SCHEDULE,
            )
            runtime.run_research["hk"] = hk_session.run_now
            logger.info("hk research session enabled", extra={"n_symbols": len(hk_wl.symbols)})

    # KR (Korea) semiconductor RESEARCH session (human directive 2026-07-14): a
    # narrow semi-only read (memory giants + HBM chain) whose sentiment leads/
    # transfers to the CN tape. Same research-only shape as CN — no orders.
    kr_runner = None
    kr_session = None
    if settings.kr_session_enabled:
        from zoneinfo import ZoneInfo

        from swing_trader.kr_watchlist import KR_INDEX_SYMBOLS, build_kr_watchlist
        from swing_trader.research_session import ResearchSession
        from swing_trader.scheduler import KR_SCHEDULE

        kr_wl = build_kr_watchlist(settings.kr_symbols)
        kr_session = ResearchSession(
            market_id="KR",
            market_label="Korea semiconductors",
            feed=RetryingFeed(YFinanceFeed()),
            ledger=ledger,  # never read (research-only); satisfies brief signature
            symbols=kr_wl.symbols,
            watchlist_lookup=kr_wl.lookup,
            trading_tz=ZoneInfo(settings.kr_market_tz),
            index_symbols=list(KR_INDEX_SYMBOLS),
            mode=settings.mode,
            runtime=runtime,
            notify=notify,  # REPORTER bot (outbound-only)
            llm_analyst=(
                LLMAnalyst(search_llm_settings) if search_llm_settings else None
            ),
            knowledge=knowledge,
            knowledge_index=knowledge_index,
            focus_note="仅半导体: 存储巨头(三星/海力士) + HBM 封装链; 关注财报 / news, "
            "情绪领先 A 股半导体",
            lang="zh",
            clock=runtime.clock,
            holdings_provider=_research_holdings,
        )
        kr_runner = DailyLoopRunner(
            {
                event: callback
                for event, callback in kr_session.callbacks().items()
                if event is not Event.PUSH_CANDIDATES
            },
            clock=runtime.clock,
            schedule=KR_SCHEDULE,
        )
        runtime.run_research["kr"] = kr_session.run_now  # manual refresh button
        logger.info("kr research session enabled", extra={"n_symbols": len(kr_wl.symbols)})

    # Human-facing research delivery has one canonical cadence, independent of
    # every market's intraday monitor schedule and of the US execution state
    # machine.  The coordinator refreshes US/HK/CN/KR with the same pipeline,
    # asks the Hermes primary model for the final synthesis, persists it, then
    # sends it.  Model/network work stays on its own daemon thread so Telegram
    # callbacks and order execution cannot be starved by a slow brief.
    brief_cycle = None
    brief_runner = None
    if brief_writer is not None:
        from swing_trader.brief_cycle import BriefCycleCoordinator

        brief_cycle = BriefCycleCoordinator(runtime, brief_writer, notify=notify)
        runtime.brief_coordinator = brief_cycle  # for the manual regenerate endpoint
        brief_runner = DailyLoopRunner(
            {
                Event.MORNING_BRIEF: lambda: brief_cycle.trigger("morning"),
                Event.EVENING_BRIEF: lambda: brief_cycle.trigger("evening"),
            },
            clock=runtime.clock,
            schedule=BEIJING_BRIEF_SCHEDULE,
        )
        logger.info(
            "all-market brief schedule enabled",
            extra={"timezone": "Asia/Shanghai", "times": ["09:00", "21:00"]},
        )
    else:
        logger.warning("all-market brief schedule disabled: primary LLM unavailable")

    app = create_app(runtime)
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    )
    api_thread = threading.Thread(target=server.run, daemon=True, name="finance-api")
    api_thread.start()
    logger.info("finance service listening", extra={"port": args.port})
    print(
        f"Finance service on http://127.0.0.1:{args.port} "
        f"(dashboard proxies /api/finance/*). Ctrl-C to stop.",
        flush=True,
    )

    # Rebuilds can happen after a market's scheduled research event. Restore
    # archived briefs synchronously above so the portal is never blank, then
    # catch up ONLY markets without a brief for their current local date. This
    # is read-only and sequential to avoid hammering the free Yahoo endpoint.
    if not args.check_now:
        market_timezones = {
            "us": settings.market_tz,
            "cn": settings.cn_market_tz,
            "hk": settings.hk_market_tz,
            "kr": settings.kr_market_tz,
        }
        missing_markets = [
            market
            for market in _markets_missing_today(runtime, market_timezones)
            if market in runtime.run_research
        ]
        if missing_markets:
            logger.info("startup research refresh queued", extra={"markets": missing_markets})

        def _startup_recovery() -> None:
            # Candidate recovery runs before research catch-up so a restart in
            # the confirmation/execution window first restores the durable
            # human decisions.  It never replays post-close approvals.
            try:
                loop.recover_confirmation_state()
            except Exception:
                logger.exception("startup confirmation recovery failed")
            if hk_loop is not None:
                try:
                    hk_loop.recover_confirmation_state()
                except Exception:
                    logger.exception("startup HK confirmation recovery failed")
            for market in missing_markets:
                if market in runtime.research_running:
                    continue
                runtime.research_running.add(market)
                try:
                    runtime.run_research[market]()
                except Exception:
                    logger.exception("startup research refresh failed", extra={"market": market})
                finally:
                    runtime.research_running.discard(market)
            if brief_cycle is not None:
                brief_cycle.catch_up_if_due()
            # Rebuilds after a regional close automatically fill any still-due
            # forecast checkpoints. This is non-blocking and never touches the
            # broker, confirmation service, or trading ledger.
            runtime.prediction_evaluator.trigger_due()

        threading.Thread(
            target=_startup_recovery,
            daemon=True,
            name="finance-startup-recovery",
        ).start()

    if args.check_now:
        # One-shot finance check (demo/verification): poll monitors, record a
        # snapshot, send the morning-style report to Telegram, populate the
        # portal. Read-only — no candidates, no orders. Runs AFTER the API is
        # up so the tab is reachable while yfinance data loads.
        print("running one-time finance check (monitors + report)...", flush=True)
        loop.on_monitor()
        loop.on_morning_report()
        if cn_session is not None:
            # Populate the CN research brief (?market=cn) for the tab; the
            # scheduled 11:30 CN send handles the group push, so this does not
            # spam the chat.
            cn_session.on_monitor()
            cn_session.on_research()
        if kr_session is not None:
            # Populate the KR semiconductor brief (?market=kr) for the tab; the
            # scheduled 15:00 KST send handles the group push.
            kr_session.on_monitor()
            kr_session.on_research()
        if hk_session is not None:
            hk_session.on_monitor()
            hk_session.on_research()
        print("check done — report sent; Finance tab now has live data.", flush=True)

    def _poll_extra() -> None:
        # Poll Telegram callbacks frequently AND drive each order-capable loop's
        # confirmation window. EVERY order-capable loop must run its own
        # on_confirm_poll so its post-approval review is launched and its window
        # is watched — otherwise its approved candidates expire unreviewed at
        # cutoff and never place (the shared adapter offset makes the concurrent
        # polls safe; the per-candidate service registry routes callbacks to the
        # owning market).
        loop.on_confirm_poll()
        if hk_loop is not None:
            hk_loop.on_confirm_poll()

    import time as _time

    # Run the schedulers on a ~30s tick, but poll Telegram every ~3s so a tapped
    # card (draft confirm/reject, candidate approval) responds in seconds rather
    # than on the slow scheduler tick — otherwise the inline button spins.
    _TG_POLL_S = 3
    _TICKS_PER_CYCLE = 10  # 10 * 3s ≈ 30s scheduler cadence
    try:
        while True:
            runner.run_pending()
            if cn_runner is not None:
                cn_runner.run_pending()
            if hk_runner is not None:
                hk_runner.run_pending()
            if kr_runner is not None:
                kr_runner.run_pending()
            if brief_runner is not None:
                brief_runner.run_pending()
            runtime.prediction_evaluator.trigger_due()
            for _ in range(_TICKS_PER_CYCLE):
                _poll_extra()
                _time.sleep(_TG_POLL_S)
    except KeyboardInterrupt:
        server.should_exit = True
        print("bye")


def _kill_switch_for(args: argparse.Namespace):
    from swing_trader.killswitch import KillSwitch, kill_switch_path

    settings = load_settings()
    return KillSwitch(kill_switch_path(args.db or settings.db_path))


def _cmd_kill(args: argparse.Namespace) -> None:
    ks = _kill_switch_for(args)
    st = ks.engage(reason=args.reason, actor=args.actor)
    print(f"KILL-SWITCH ENGAGED at {st.since} (actor={st.actor}); reason: {st.reason or '(none)'}")
    print(f"flag file: {ks.path}")
    print("New entries are HALTED. Exits and protective stops are unaffected.")
    print("Release with:  python -m swing_trader release")


def _cmd_release(args: argparse.Namespace) -> None:
    ks = _kill_switch_for(args)
    ks.release(actor=args.actor)
    print(f"kill-switch RELEASED (actor={args.actor}); new entries permitted again.")
    print(f"flag file removed: {ks.path}")


def _cmd_killswitch_status(args: argparse.Namespace) -> None:
    st = _kill_switch_for(args).state()
    if st.engaged:
        print(f"ENGAGED since {st.since} (actor={st.actor}); reason: {st.reason or '(none)'}")
    else:
        print("released (new entries permitted)")


def _cmd_readiness(args: argparse.Namespace) -> None:
    from swing_trader.ledger import Ledger
    from swing_trader.readiness import assess_paper_readiness

    settings = load_settings()
    ledger = Ledger(url=f"sqlite:///{args.db or settings.db_path}")
    report = assess_paper_readiness(ledger, min_days=args.min_days)
    print(report.summary())


def _cmd_migrate_vector(args: argparse.Namespace) -> None:
    """Rebuild the remote vector index from authoritative research documents."""
    import json
    import os

    from swing_trader.knowledge import (
        COLLECTION_NAME,
        OPENAI_COLLECTION_NAME,
        OPENAI_EMBEDDING_DIM,
        DocumentStore,
        HashingEmbedder,
        KnowledgeIndex,
        OpenAIEmbedder,
    )
    from swing_trader.vector_migration import migrate_document_store

    target_url = args.target_url or os.environ.get("FINANCE_QDRANT_URL")
    if not target_url:
        raise SystemExit("--target-url or FINANCE_QDRANT_URL is required")
    if args.embedding_provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise SystemExit("OPENAI_API_KEY is required for --embedding-provider openai")
        embedding_dim = args.embedding_dim or OPENAI_EMBEDDING_DIM
        embedder = OpenAIEmbedder(
            api_key,
            model=args.embedding_model,
            dimensions=embedding_dim,
        )
        collection = args.collection or OPENAI_COLLECTION_NAME
    else:
        embedding_dim = args.embedding_dim or 256
        embedder = HashingEmbedder(dim=embedding_dim)
        collection = args.collection or COLLECTION_NAME
    documents = DocumentStore(f"sqlite:///{args.documents_db}")
    target = KnowledgeIndex(
        url=target_url,
        embedder=embedder,
        collection=collection,
    )
    report = migrate_document_store(documents, target, batch_size=args.batch_size)
    print(json.dumps(report.to_dict(), sort_keys=True))
    if not report.verified:
        raise SystemExit(2)


def main() -> None:
    from swing_trader.knowledge import OPENAI_EMBEDDING_MODEL

    parser = argparse.ArgumentParser(prog="swing_trader")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sim = sub.add_parser("simulate", help="offline multi-day paper demo")
    p_sim.add_argument("--days", type=int, default=22)
    p_sim.add_argument("--crash-day", type=int, default=None)
    p_sim.add_argument("--db", default="sim.db")
    p_sim.set_defaults(func=_cmd_simulate)

    p_serve = sub.add_parser("serve", help="run the paper loop + finance API")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=9319)
    p_serve.add_argument("--db", default=None)
    p_serve.add_argument("--starting-cash", type=float, default=2_000.0)
    p_serve.add_argument(
        "--check-now", action="store_true", help="run monitors + morning report once at startup"
    )
    p_serve.set_defaults(func=_cmd_serve)

    # Out-of-band kill-switch (Loop.md §3 / Phase 0.95): engage/release the HALT
    # flag from the shell even if the service is wedged. Resolves the SAME file
    # the running service reads (next to --db / DB_PATH).
    p_kill = sub.add_parser("kill", help="ENGAGE the kill-switch (halt new entries)")
    p_kill.add_argument("--db", default=None)
    p_kill.add_argument("--reason", default="", help="why (recorded in the flag file)")
    p_kill.add_argument("--actor", default="cli")
    p_kill.set_defaults(func=_cmd_kill)

    p_rel = sub.add_parser("release", help="RELEASE the kill-switch (re-permit entries)")
    p_rel.add_argument("--db", default=None)
    p_rel.add_argument("--actor", default="cli")
    p_rel.set_defaults(func=_cmd_release)

    p_ks = sub.add_parser("killswitch-status", help="print the kill-switch state")
    p_ks.add_argument("--db", default=None)
    p_ks.set_defaults(func=_cmd_killswitch_status)

    p_rd = sub.add_parser("readiness", help="print paper-trading readiness vs the ≥20-day gate")
    p_rd.add_argument("--db", default=None)
    p_rd.add_argument("--min-days", type=int, default=20)
    p_rd.set_defaults(func=_cmd_readiness)

    p_mv = sub.add_parser(
        "migrate-vector",
        help="idempotently rebuild remote Qdrant from documents.db",
    )
    p_mv.add_argument("--documents-db", default="data/knowledge/documents.db")
    p_mv.add_argument("--target-url", default=None)
    p_mv.add_argument("--batch-size", type=int, default=128)
    p_mv.add_argument(
        "--embedding-provider",
        choices=("hashing", "openai"),
        default="hashing",
    )
    p_mv.add_argument("--embedding-model", default=OPENAI_EMBEDDING_MODEL)
    p_mv.add_argument(
        "--embedding-dim",
        type=int,
        default=None,
        help="vector dimension (default: OpenAI 1536, hashing 256)",
    )
    p_mv.add_argument("--collection", default=None)
    p_mv.set_defaults(func=_cmd_migrate_vector)

    args = parser.parse_args()
    setup_logging(level=args.log_level)
    args.func(args)


if __name__ == "__main__":
    main()
