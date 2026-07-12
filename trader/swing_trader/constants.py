"""Hard guardrail constants (Loop.md §3).

These are HARD CAPS enforced in code. User config may tighten them, never
loosen them (validated in `swing_trader.config.Settings`). The RiskEngine
re-asserts them independently of config, so the LLM/agent layer cannot
bypass them by editing settings.
"""

HARD_MAX_PER_TRADE_RISK_PCT: float = 1.6
"""Per-trade risk (as % of total equity) may never exceed this."""

DAILY_DRAWDOWN_BREAKER_PCT: float = -4.0
"""Daily drawdown at or below this halts all new entries for the day."""
