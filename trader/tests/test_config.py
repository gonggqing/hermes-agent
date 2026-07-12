"""Tests for swing_trader.config — safety posture and hard-cap validation."""

from datetime import time

import pytest
from pydantic import ValidationError

from swing_trader.config import BrokerBackend, Mode, Settings


def make(**kw) -> Settings:
    """Settings isolated from any real .env file."""
    return Settings(_env_file=None, **kw)


class TestSafeDefaults:
    def test_dry_run_default_true(self):
        assert make().dry_run is True

    def test_paper_broker_default(self):
        assert make().broker is BrokerBackend.PAPER

    def test_human_confirm_default_false(self):
        assert make().human_confirm is False

    def test_live_orders_blocked_by_default(self):
        s = make()
        assert s.live_orders_allowed is False
        assert s.mode is Mode.PAPER


class TestLiveOrderGuardrail:
    """Loop.md §3: no live order unless HUMAN_CONFIRM && BROKER != paper (&& !DRY_RUN)."""

    def test_all_three_flags_required(self):
        s = make(dry_run=False, broker="ibkr", human_confirm=True)
        assert s.live_orders_allowed is True
        assert s.mode is Mode.LIVE

    @pytest.mark.parametrize(
        "kw",
        [
            dict(broker="ibkr", human_confirm=True),  # dry_run still true
            dict(dry_run=False, human_confirm=True),  # broker still paper
            dict(dry_run=False, broker="ibkr"),  # no human confirm
            dict(),
        ],
    )
    def test_any_missing_flag_blocks_live(self, kw):
        s = make(**kw)
        assert s.live_orders_allowed is False
        assert s.mode is Mode.PAPER


class TestEnvOverrides:
    def test_env_vars_match_loop_md_names(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("BROKER", "ibkr")
        monkeypatch.setenv("HUMAN_CONFIRM", "true")
        s = Settings(_env_file=None)
        assert s.dry_run is False
        assert s.broker is BrokerBackend.IBKR
        assert s.human_confirm is True

    def test_secret_from_env(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
        s = Settings(_env_file=None)
        assert s.telegram_bot_token.get_secret_value() == "123:abc"


class TestHardCaps:
    def test_per_trade_risk_above_cap_rejected(self):
        with pytest.raises(ValidationError, match="hard cap"):
            make(per_trade_risk_pct=1.61)

    def test_per_trade_risk_at_cap_ok(self):
        assert make(per_trade_risk_pct=1.6).per_trade_risk_pct == 1.6

    def test_per_trade_risk_must_be_positive(self):
        with pytest.raises(ValidationError):
            make(per_trade_risk_pct=0)

    def test_breaker_looser_than_cap_rejected(self):
        with pytest.raises(ValidationError, match="looser"):
            make(daily_drawdown_breaker_pct=-4.5)

    def test_breaker_must_be_negative(self):
        with pytest.raises(ValidationError, match="negative"):
            make(daily_drawdown_breaker_pct=0.0)

    def test_breaker_tighter_ok(self):
        assert make(daily_drawdown_breaker_pct=-3.0).daily_drawdown_breaker_pct == -3.0


class TestWindow:
    def test_cutoff_must_follow_push(self):
        with pytest.raises(ValidationError, match="after push_time_et"):
            make(push_time_et=time(12, 30), confirm_cutoff_et=time(11, 30))

    def test_defaults_match_loop_md(self):
        s = make()
        assert s.push_time_et == time(11, 30)
        assert s.confirm_cutoff_et == time(12, 30)
        assert s.market_tz == "America/New_York"
        assert s.user_tz == "Asia/Shanghai"


class TestSecretsNeverLeak:
    def test_token_not_in_repr_or_str(self):
        s = make(telegram_bot_token="123:supersecret")
        assert "supersecret" not in repr(s)
        assert "supersecret" not in str(s)
