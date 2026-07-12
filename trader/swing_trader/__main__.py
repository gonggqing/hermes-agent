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
    from swing_trader.datafeed import YFinanceFeed
    from swing_trader.ledger import Ledger
    from swing_trader.llm import LLMAnalyst, llm_settings_from_env
    from swing_trader.paper_broker import PaperBroker
    from swing_trader.scheduler import DailyLoopRunner
    from swing_trader.telegram_gateway import HttpTransport

    # ~/.hermes/.env is the SINGLE source of truth for secrets (Telegram
    # token + finance group chat id, DeepSeek/GLM keys) — shared with the
    # Hermes gateway/dashboard. Loaded into the process env first, so these
    # values take precedence over any optional local trader/.env overrides.
    load_dotenv(Path.home() / ".hermes" / ".env", override=False)
    settings = load_settings()
    if settings.broker.value != "paper":
        # Phase 0 hard stop (Loop.md §7): only the PaperBroker exists.
        raise SystemExit("Phase 0 supports BROKER=paper only")

    from swing_trader.rehydrate import rehydrate_from_ledger

    ledger = Ledger(url=f"sqlite:///{args.db or settings.db_path}")
    broker = PaperBroker(starting_cash=args.starting_cash)
    rehydration = rehydrate_from_ledger(broker, ledger, settings.mode)
    print(rehydration.summary(), flush=True)
    feed = YFinanceFeed()
    runtime = FinanceRuntime(ledger=ledger, broker=broker, mode=settings.mode)

    telegram = None
    notify = None
    import os

    # A DEDICATED finance bot token enables interactive approvals; the
    # shared gateway bot stays OUTBOUND-ONLY (a second getUpdates consumer
    # 409-kicks the Hermes gateway offline). Loop.md Phase 0.5 backlog 6.
    dedicated = os.environ.get("FINANCE_TELEGRAM_BOT_TOKEN", "").strip()
    token = dedicated or (
        settings.telegram_bot_token.get_secret_value()
        if settings.telegram_bot_token else ""
    )
    if token and settings.telegram_chat_id:
        from pydantic import SecretStr

        transport = HttpTransport(SecretStr(token))
        interactive = bool(dedicated) or (
            os.environ.get("FINANCE_TELEGRAM_POLL", "").lower() in ("1", "true")
        )
        allowed = {
            u for u in os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")
            if u.strip()
        }
        telegram = TelegramSurfaceAdapter(
            transport, settings.telegram_chat_id,
            interactive=interactive, allowed_users=allowed,
        )
        notify = lambda text: transport.send_message(settings.telegram_chat_id, text)
        logger.info("telegram surface attached",
                    extra={"interactive": interactive,
                           "dedicated_bot": bool(dedicated),
                           "n_allowed_users": len(allowed)})
    else:
        logger.warning("telegram not configured; portal-only confirmations")

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
    loop = DailyLoop(
        feed, broker, ledger, mode=settings.mode,
        runtime=runtime, telegram=telegram, notify=notify,
        llm_analyst=llm_analyst,
        knowledge=knowledge, knowledge_index=knowledge_index,
    )
    if rehydration.performed:
        loop.execution.seed_synced_fills(rehydration.fill_ids)
        loop.execution.seed_protective_stops(broker.get_orders(active_only=True))
    runner = DailyLoopRunner(loop.callbacks(), clock=runtime.clock)

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
        print("check done — report sent; Finance tab now has live data.", flush=True)

    def _poll_extra() -> None:
        # inside the confirmation window, poll Telegram callbacks frequently
        loop.on_confirm_poll()

    import time as _time
    try:
        while True:
            runner.run_pending()
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
