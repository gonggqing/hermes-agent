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

    from swing_trader.api import DEFAULT_SERVICE_PORT, FinanceRuntime, create_app
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
    # Phase 0.9 (portfolio): instrument type-ahead behind a cached, offline
    # provider (a live adapter can slot behind the same port later).
    # Append-only Portfolio Journal + human-confirmation draft service, sharing
    # the ledger's DB file but none of its tables (Loop.md P0.9 boundary #1).
    from swing_trader.portfolio_draft import PortfolioDraftService
    from swing_trader.portfolio_journal import PortfolioJournal

    runtime.portfolio = PortfolioJournal(url=db_url)
    runtime.portfolio_drafts = PortfolioDraftService(runtime.portfolio, clock=runtime.clock)
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
        notify = lambda text: report_transport.send_message(chat_id, text)
        logger.info("reporter bot attached (outbound-only)",
                    extra={"using_shared": bool(shared_token)})
    else:
        logger.warning("no reporter bot configured; reports go to logs only")

    if chat_id and dedicated_token:
        approval_transport = HttpTransport(SecretStr(dedicated_token))
        telegram = TelegramSurfaceAdapter(
            approval_transport, chat_id, interactive=True, allowed_users=allowed,
        )
        logger.info("finance gatekeeper bot attached (interactive approvals)",
                    extra={"n_allowed_users": len(allowed)})
    else:
        logger.warning("no dedicated finance bot; approvals via portal only")

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
    loop = DailyLoop(
        feed, broker, ledger, mode=settings.mode,
        live_orders_allowed=settings.live_orders_allowed,
        runtime=runtime, telegram=telegram, notify=notify,
        fundamentals=fundamentals,  # real fundamentals for the scheduled loop
        earnings_provider=earnings_provider,  # earnings calendar (Phase 0.75)
        llm_analyst=llm_analyst,
        knowledge=knowledge, knowledge_index=knowledge_index,
    )
    if rehydration.performed:
        loop.execution.seed_synced_fills(rehydration.fill_ids)
        loop.execution.seed_protective_stops(broker.get_orders(active_only=True))
    runner = DailyLoopRunner(loop.callbacks(), clock=runtime.clock)
    # Manual missed-session catch-up (Loop.md §4b): expose the trading session's
    # run + finalize so /v1/session/* can trigger them on demand (human-gated).
    runtime.run_session = loop.run_session_now
    runtime.finalize_session = loop.finalize_session_now

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
        logger.info("cn research session enabled",
                    extra={"n_symbols": len(cn_wl.symbols)})

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
        print("check done — report sent; Finance tab now has live data.", flush=True)

    def _poll_extra() -> None:
        # inside the confirmation window, poll Telegram callbacks frequently
        loop.on_confirm_poll()

    import time as _time
    try:
        while True:
            runner.run_pending()
            if cn_runner is not None:
                cn_runner.run_pending()
            _poll_extra()
            _time.sleep(30)
    except KeyboardInterrupt:
        server.should_exit = True
        print("bye")


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

    args = parser.parse_args()
    setup_logging(level=args.log_level)
    args.func(args)


if __name__ == "__main__":
    main()
