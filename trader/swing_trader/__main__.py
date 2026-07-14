"""CLI entry point.

- ``python -m swing_trader simulate --days 22``   offline E2E paper demo
- ``python -m swing_trader serve``                real Phase-0 paper loop +
  Finance service API on :9319 (the Hermes dashboard proxies /api/finance/*
  here; Telegram attaches when TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are set).

NOTE (Phase 0 limitation, documented): the PaperBroker keeps positions in
memory — restarting ``serve`` resets the paper account (the ledger keeps the
full history). Rehydrating broker state from the ledger is a Phase-0.5 TODO.
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


def _restore_latest_briefs(runtime, markets=("us", "cn", "kr")) -> list[str]:
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
    """Markets with no archived/in-memory brief for their current local date."""
    from zoneinfo import ZoneInfo

    missing = []
    now = runtime.clock()
    for market, tz_name in market_timezones.items():
        payload = _runtime_brief(runtime, market) or {}
        today = now.astimezone(ZoneInfo(tz_name)).date().isoformat()
        if str(payload.get("trading_date") or "") != today:
            missing.append(market)
    return missing


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
    print(f"simulated {len(result.days)} trading days "
          f"({result.days[0]} → {result.days[-1]})")
    print(f"final equity: {account.equity:.2f} (cash {account.cash:.2f})")
    print(f"closed trades: {stats.n_closed}  win rate: {stats.win_rate:.0%}  "
          f"expectancy: {stats.expectancy:.2f}  max DD: {stats.max_drawdown_pct:.2f}%")
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
    from swing_trader.scheduler import DailyLoopRunner
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
        broker = build_broker(settings, starting_cash=args.starting_cash)
    except ImportError as exc:  # ib_async not installed (pip install '.[ibkr]')
        raise SystemExit(
            f"BROKER=ibkr but ib_async is not installed: {exc}. "
            "Install it with: pip install 'swing-trader[ibkr]'"
        ) from exc
    print(f"broker: {settings.broker.value} "
          f"(live orders {'ALLOWED' if settings.live_orders_allowed else 'blocked'})",
          flush=True)
    rehydration = rehydrate_from_ledger(broker, ledger, settings.mode)
    print(rehydration.summary(), flush=True)
    # Phase 0.8 (resilience): wrap the live feed in RetryingFeed so transient
    # yfinance errors (rate limits / network blips) retry with backoff instead
    # of surfacing as a hard DataFeedError to the loop and /v1/analyze.
    feed = RetryingFeed(YFinanceFeed())
    # Real fundamentals (Loop.md Phase 0.75 thrust A): yfinance-backed, cached,
    # fail-None. Feeds the scheduled FundamentalAgent AND on-demand /v1/analyze.
    from swing_trader.earnings import YFinanceEarnings
    from swing_trader.fundamentals import YFinanceFundamentals

    fundamentals = YFinanceFundamentals()
    earnings_provider = YFinanceEarnings()  # earnings calendar (Phase 0.75)
    runtime = FinanceRuntime(ledger=ledger, broker=broker, mode=settings.mode)
    # On-demand market analysis for the conversational agent (thrust B):
    runtime.feed = feed
    runtime.fundamentals = fundamentals
    # Manual kill-switch (Loop.md §3 / Phase 0.95): a filesystem HALT flag next
    # to the DB. Engaged → the loop's dead-man's switch vetoes NEW entries (the
    # RiskEngine enforces it); exits/protection are untouched. Survives restart
    # and is `touch`-able out-of-band if the service wedges.
    from swing_trader.killswitch import KillSwitch, kill_switch_path

    kill_switch = KillSwitch(kill_switch_path(args.db or settings.db_path),
                             clock=runtime.clock)
    runtime.kill_switch = kill_switch
    if kill_switch.engaged():
        print(f"⚠️  KILL-SWITCH ENGAGED at startup: {kill_switch.state().reason or '(no reason)'}",
              flush=True)
    # Phase 0.9 (portfolio): instrument type-ahead behind a cached, offline
    # provider (a live adapter can slot behind the same port later).
    # Append-only Portfolio Journal + human-confirmation draft service, sharing
    # the ledger's DB file but none of its tables (Loop.md P0.9 boundary #1).
    from swing_trader.portfolio_draft import PortfolioDraftService
    from swing_trader.portfolio_journal import PortfolioJournal

    runtime.portfolio = PortfolioJournal(url=db_url)
    runtime.portfolio_drafts = PortfolioDraftService(runtime.portfolio, clock=runtime.clock)
    # Durable research-brief history (own DB file, own MetaData — never the
    # ledger's tables): archives each published brief and hydrates the volatile
    # runtime cache immediately after a rebuild.
    from pathlib import Path as _DbPath

    from swing_trader.brief_store import BriefStore

    _briefs_url = f"sqlite:///{_DbPath(args.db or settings.db_path).parent / 'briefs.db'}"
    runtime.brief_store = BriefStore(url=_briefs_url)
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
        PortfolioInstrumentProvider,
        StaticInstrumentProvider,
    )

    runtime.instrument_search = CachedInstrumentSearch(CompositeInstrumentProvider([
        StaticInstrumentProvider(),
        PortfolioInstrumentProvider(runtime.portfolio),
    ]))
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
        settings.telegram_bot_token.get_secret_value()
        if settings.telegram_bot_token else ""
    ).strip()
    dedicated_token = (
        settings.finance_telegram_bot_token.get_secret_value()
        if settings.finance_telegram_bot_token
        else os.environ.get("FINANCE_TELEGRAM_BOT_TOKEN", "")
    ).strip()
    allowed = {u.strip() for u in settings.telegram_allowed_users.split(",")
               if u.strip()}

    if chat_id and (shared_token or dedicated_token):
        # Reporter prefers the shared gateway token; falls back to the finance
        # token only if the gateway one is absent.
        report_transport = HttpTransport(SecretStr(shared_token or dedicated_token))

        def notify(text: str) -> None:
            report_transport.send_message(chat_id, text)

        logger.info("reporter bot attached (outbound-only)",
                    extra={"using_shared": bool(shared_token)})
    else:
        logger.warning("no reporter bot configured; reports go to logs only")

    if chat_id and dedicated_token:
        approval_transport = HttpTransport(SecretStr(dedicated_token))
        telegram = TelegramSurfaceAdapter(
            approval_transport, chat_id, interactive=True, allowed_users=allowed,
        )
        # Let tapped draft cards confirm/reject real-holdings drafts IN Telegram
        # (Loop.md P0.9 boundary #4: authenticated human confirms, LLM never).
        telegram.set_draft_service(runtime.portfolio_drafts)
        logger.info("finance gatekeeper bot attached (interactive approvals)",
                    extra={"n_allowed_users": len(allowed)})
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

    from swing_trader.knowledge_pipeline import KnowledgeConfig, build_knowledge

    knowledge, knowledge_index = build_knowledge(KnowledgeConfig(
        root_dir=_Path("data/knowledge"),
        qdrant_url=os.environ.get("FINANCE_QDRANT_URL") or None,
    ))
    logger.info("knowledge store ready",
                extra={"vector_ok": knowledge_index is not None})

    llm_settings = llm_settings_from_env()
    llm_analyst = LLMAnalyst(llm_settings) if llm_settings else None
    if llm_analyst:
        logger.info("llm analyst enabled", extra={"model": llm_settings.model})
    else:
        logger.info("no LLM key found; rule-based analysis only")

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
                analyze_symbol, extract_symbols, render_analysis_zh,
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
                    feed, sym, fundamentals=fundamentals,
                    llm_analyst=llm_analyst, knowledge=knowledge,
                    knowledge_index=knowledge_index, now=runtime.clock(),
                )
            except Exception:
                return f"没找到 {sym} 的行情数据，换个代码试试？"
            return render_analysis_zh(result)

        telegram.set_text_responder(_finance_responder)
        # Recording (记账) is the finance bot's job (user decision 2026-07-14):
        # a DM trade message ("卖了 159813 200股 @1.893") → draft → confirm card
        # in the DM. Analysis DMs still fall through to _finance_responder above.
        from swing_trader.trade_record import make_trade_recorder

        telegram.set_trade_recorder(make_trade_recorder(runtime))

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
            return [ac for ac in j.list_accounts()
                    if any(h.symbol == symbol for h in j.holdings(ac.id).holdings)]

        def _match_account(name: str):
            for ac in runtime.portfolio.list_accounts():
                if name and (ac.name == name or name in ac.name
                             or (len(name) >= 2 and name[:2] in ac.name)):
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
                    symbol, price, currency=ccy, source="manual",
                    actor="telegram", as_of=runtime.clock())
                return (f"✅ 已把 {symbol} 现价标记为 {price:g} {ccy}"
                        "（用于市值/盈亏显示，不影响成本）")
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
                    account_id=from_acct.id, symbol=symbol, field="account",
                    target_account_id=tgt.id, original_text=text)
                label = tgt.name
            else:
                try:
                    num = float(value)
                except ValueError:
                    return f"没看懂数值 {value!r}。"
                r = drafts.propose_restate(
                    account_id=from_acct.id, symbol=symbol, field=field,
                    value=num, original_text=text)
                label = from_acct.name
            if not r.ok:
                return r.message  # refusal (未知价格 / 数量<=0 / 目标账户无效 …)
            telegram.push_draft_card(r.draft, account_label=label)
            return "📝 已生成更正草稿，请核对下方卡片并确认 ✅ / 拒绝 ❌"

        telegram.set_update_handler(_update_handler)
        telegram.register_commands(COMMAND_MENU)
    loop = DailyLoop(
        feed, broker, ledger, mode=settings.mode,
        live_orders_allowed=settings.live_orders_allowed,
        runtime=runtime, telegram=telegram, notify=notify,
        fundamentals=fundamentals,  # real fundamentals for the scheduled loop
        earnings_provider=earnings_provider,  # earnings calendar (Phase 0.75)
        llm_analyst=llm_analyst,
        knowledge=knowledge, knowledge_index=knowledge_index,
        kill_switch=kill_switch,  # Phase 0.95 manual HALT
    )
    if rehydration.performed:
        loop.execution.seed_synced_fills(rehydration.fill_ids)
        loop.execution.seed_protective_stops(broker.get_orders(active_only=True))
    runner = DailyLoopRunner(loop.callbacks(), clock=runtime.clock)
    # Manual missed-session catch-up (Loop.md §4b): expose the trading session's
    # run + finalize so /v1/session/* can trigger them on demand (human-gated).
    runtime.run_session = loop.run_session_now
    runtime.finalize_session = loop.finalize_session_now
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

        from swing_trader.cn_watchlist import CN_INDEX_SYMBOLS, build_cn_watchlist
        from swing_trader.research_session import ResearchSession
        from swing_trader.scheduler import CN_SCHEDULE

        cn_wl = build_cn_watchlist(settings.cn_symbols)
        cn_session = ResearchSession(
            market_id="CN",
            market_label="China / HK",
            feed=RetryingFeed(YFinanceFeed()),
            ledger=ledger,  # never read (research-only); satisfies brief signature
            symbols=cn_wl.symbols,
            watchlist_lookup=cn_wl.lookup,
            trading_tz=ZoneInfo(settings.cn_market_tz),
            index_symbols=list(CN_INDEX_SYMBOLS),
            mode=settings.mode,
            runtime=runtime,
            notify=notify,  # REPORTER bot (outbound-only)
            llm_analyst=LLMAnalyst(llm_settings) if llm_settings else None,
            knowledge=knowledge,
            knowledge_index=knowledge_index,
            focus_note="聚焦科技: 半导体 / 电子 / AI (其他板块仅作参考)",
            lang="zh",
            clock=runtime.clock,
        )
        cn_runner = DailyLoopRunner(
            cn_session.callbacks(), clock=runtime.clock, schedule=CN_SCHEDULE
        )
        runtime.run_research["cn"] = cn_session.run_now  # manual refresh button
        logger.info("cn research session enabled",
                    extra={"n_symbols": len(cn_wl.symbols)})

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
            llm_analyst=LLMAnalyst(llm_settings) if llm_settings else None,
            knowledge=knowledge,
            knowledge_index=knowledge_index,
            focus_note="仅半导体: 存储巨头(三星/海力士) + HBM 封装链; 关注财报 / news, "
                       "情绪领先 A 股半导体",
            lang="zh",
            clock=runtime.clock,
        )
        kr_runner = DailyLoopRunner(
            kr_session.callbacks(), clock=runtime.clock, schedule=KR_SCHEDULE
        )
        runtime.run_research["kr"] = kr_session.run_now  # manual refresh button
        logger.info("kr research session enabled",
                    extra={"n_symbols": len(kr_wl.symbols)})

    app = create_app(runtime)
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=args.port, log_level="warning"
    ))
    api_thread = threading.Thread(target=server.run, daemon=True,
                                  name="finance-api")
    api_thread.start()
    logger.info("finance service listening", extra={"port": args.port})
    print(f"Finance service on http://127.0.0.1:{args.port} "
          f"(dashboard proxies /api/finance/*). Ctrl-C to stop.", flush=True)

    # Rebuilds can happen after a market's scheduled research event. Restore
    # archived briefs synchronously above so the portal is never blank, then
    # catch up ONLY markets without a brief for their current local date. This
    # is read-only and sequential to avoid hammering the free Yahoo endpoint.
    if not args.check_now:
        market_timezones = {
            "us": settings.market_tz,
            "cn": settings.cn_market_tz,
            "kr": settings.kr_market_tz,
        }
        missing_markets = [
            market for market in _markets_missing_today(runtime, market_timezones)
            if market in runtime.run_research
        ]
        if missing_markets:
            logger.info("startup research refresh queued",
                        extra={"markets": missing_markets})

            def _refresh_missing_research() -> None:
                for market in missing_markets:
                    if market in runtime.research_running:
                        continue
                    runtime.research_running.add(market)
                    try:
                        runtime.run_research[market]()
                    except Exception:
                        logger.exception("startup research refresh failed",
                                         extra={"market": market})
                    finally:
                        runtime.research_running.discard(market)

            threading.Thread(
                target=_refresh_missing_research,
                daemon=True,
                name="finance-research-catchup",
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
        print("check done — report sent; Finance tab now has live data.", flush=True)

    def _poll_extra() -> None:
        # inside the confirmation window, poll Telegram callbacks frequently
        loop.on_confirm_poll()

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
            if kr_runner is not None:
                kr_runner.run_pending()
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="swing_trader")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sim = sub.add_parser("simulate", help="offline multi-day paper demo")
    p_sim.add_argument("--days", type=int, default=22)
    p_sim.add_argument("--crash-day", type=int, default=None)
    p_sim.add_argument("--db", default="sim.db")
    p_sim.set_defaults(func=_cmd_simulate)

    p_serve = sub.add_parser("serve", help="run the paper loop + finance API")
    p_serve.add_argument("--port", type=int, default=9319)
    p_serve.add_argument("--db", default=None)
    p_serve.add_argument("--starting-cash", type=float, default=2_000.0)
    p_serve.add_argument("--check-now", action="store_true",
                         help="run monitors + morning report once at startup")
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

    p_rd = sub.add_parser("readiness",
                          help="print paper-trading readiness vs the ≥20-day gate")
    p_rd.add_argument("--db", default=None)
    p_rd.add_argument("--min-days", type=int, default=20)
    p_rd.set_defaults(func=_cmd_readiness)

    args = parser.parse_args()
    setup_logging(level=args.log_level)
    args.func(args)


if __name__ == "__main__":
    main()
