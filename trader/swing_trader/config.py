"""Runtime configuration (Loop.md §2, §3, §9).

Env-var driven (with optional `.env` file). Safety posture:

- ``DRY_RUN`` defaults to True — nothing is ever sent anywhere by default.
- Live orders require ALL of: ``HUMAN_CONFIRM=true``, ``BROKER != paper``,
  ``DRY_RUN=false`` (Loop.md §3/§9).
- Risk parameters are validated against the hard caps in
  :mod:`swing_trader.constants`; loosening them raises at load time.
- Secrets are ``SecretStr`` — they never appear in ``repr``/``str`` and must
  never be written to logs or the ledger.
"""

from __future__ import annotations

from datetime import time
from enum import Enum
from pathlib import Path
from typing import Literal, Optional

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from swing_trader.constants import (
    DAILY_DRAWDOWN_BREAKER_PCT,
    HARD_MAX_PER_TRADE_RISK_PCT,
)


class BrokerBackend(str, Enum):
    PAPER = "paper"
    ALPACA = "alpaca"
    IBKR = "ibkr"


class Mode(str, Enum):
    """Tag every ledger row with where fills come from (Loop.md §6)."""

    PAPER = "paper"
    LIVE = "live"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Safety / mode (env names match Loop.md §3 verbatim) ---
    dry_run: bool = True
    broker: BrokerBackend = BrokerBackend.PAPER
    human_confirm: bool = False

    # --- Interactive Brokers connection (Loop.md §5.1 / Phase 1) ---
    # Only used when broker=ibkr. TWS: 7497=paper, 7496=live. IB Gateway:
    # 4002=paper, 4001=live. The broker factory derives the paper/live account
    # flag from the triple gate (``live_orders_allowed``), NOT from the port, so
    # a live port under an un-gated config is refused at construction.
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1

    # --- Risk parameters (config may tighten hard caps, never loosen) ---
    per_trade_risk_pct: float = Field(default=1.0, gt=0)
    daily_drawdown_breaker_pct: float = DAILY_DRAWDOWN_BREAKER_PCT

    # --- Daily window, times in ET (Loop.md §4) ---
    push_time_et: time = time(11, 30)
    confirm_cutoff_et: time = time(12, 30)
    market_tz: str = "America/New_York"
    user_tz: str = "Asia/Shanghai"

    # --- China morning RESEARCH session (Loop.md two-session extension) ---
    # A second daily session that runs a lighter, tech-focused research brief on
    # the China/HK market in the CN morning. Report-only for now (NO orders),
    # but wired so it can gain order authority later. ``cn_symbols`` overrides
    # the default CN/HK universe (comma-separated; empty = built-in default).
    cn_session_enabled: bool = True
    cn_market_tz: str = "Asia/Shanghai"
    cn_symbols: str = ""

    # --- KR (Korea) semiconductor RESEARCH session (Loop.md two-session ext) ---
    # A narrow, semiconductor-only KR read (memory giants + HBM chain): KR semi
    # sentiment leads/transfers to the CN tape. Report-only. ``kr_symbols``
    # overrides the built-in universe (comma-separated; empty = default).
    kr_session_enabled: bool = True
    kr_market_tz: str = "Asia/Seoul"
    kr_symbols: str = ""

    # --- Telegram bots: distinct roles in the shared group (Loop.md P0.5) ---
    # The shared gateway bot (``telegram_bot_token``) is the OUTBOUND-ONLY
    # REPORTER (daily summaries / research briefs). A dedicated finance bot
    # (``finance_telegram_bot_token``) is the interactive GATEKEEPER that only
    # asks for approval; it long-polls its OWN token so it never 409-conflicts
    # with the gateway. ``telegram_allowed_users`` is the approval allowlist.
    finance_telegram_bot_token: Optional[SecretStr] = None
    telegram_allowed_users: str = ""

    # --- Storage / logging ---
    db_path: Path = Path("trader.db")
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- Secrets (never logged; see swing_trader.log redaction filter) ---
    telegram_bot_token: Optional[SecretStr] = None
    telegram_chat_id: Optional[str] = None

    @field_validator("per_trade_risk_pct")
    @classmethod
    def _cap_per_trade_risk(cls, v: float) -> float:
        if v > HARD_MAX_PER_TRADE_RISK_PCT:
            raise ValueError(
                f"per_trade_risk_pct={v} exceeds hard cap "
                f"{HARD_MAX_PER_TRADE_RISK_PCT} (Loop.md §3)"
            )
        return v

    @field_validator("daily_drawdown_breaker_pct")
    @classmethod
    def _cap_breaker(cls, v: float) -> float:
        if v < DAILY_DRAWDOWN_BREAKER_PCT:
            raise ValueError(
                f"daily_drawdown_breaker_pct={v} is looser than hard cap "
                f"{DAILY_DRAWDOWN_BREAKER_PCT} (Loop.md §3)"
            )
        if v >= 0:
            raise ValueError("daily_drawdown_breaker_pct must be negative")
        return v

    @model_validator(mode="after")
    def _window_ordering(self) -> "Settings":
        if self.confirm_cutoff_et <= self.push_time_et:
            raise ValueError(
                "confirm_cutoff_et must be after push_time_et (Loop.md §4: 11:30 -> 12:30 ET)"
            )
        return self

    @property
    def live_orders_allowed(self) -> bool:
        """Guardrail (Loop.md §3/§9): HUMAN_CONFIRM && BROKER != paper && !DRY_RUN."""
        return (
            self.human_confirm
            and self.broker is not BrokerBackend.PAPER
            and not self.dry_run
        )

    @property
    def mode(self) -> Mode:
        return Mode.LIVE if self.live_orders_allowed else Mode.PAPER


def load_settings(env_file: str | Path | None = ".env") -> Settings:
    """Load settings from environment (and optional .env file)."""
    return Settings(_env_file=env_file)
