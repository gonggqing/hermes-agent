"""Tests for the manual kill-switch (Loop.md §3 / Phase 0.95).

Filesystem HALT flag: engage/release/state, fail-closed on read errors, and the
integration into assess_health (engaged → entries_allowed False, UNHEALTHY).
"""

from __future__ import annotations

from datetime import datetime, timezone

from swing_trader.health import HealthLevel, assess_health
from swing_trader.killswitch import KillSwitch, kill_switch_path


class _Snap:
    def __init__(self, ts):
        self.ts = ts


def _fresh(now):
    return _Snap(now)


def _clock():
    return datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class TestKillSwitchFile:
    def test_starts_released(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL", clock=_clock)
        assert ks.engaged() is False
        assert ks.state().engaged is False

    def test_engage_then_release(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL", clock=_clock)
        st = ks.engage(reason="drawdown spike", actor="gongqing")
        assert st.engaged and st.reason == "drawdown spike" and st.actor == "gongqing"
        assert st.since == _clock().isoformat()
        assert ks.engaged() is True
        # persisted: a fresh handle sees it
        assert KillSwitch(tmp_path / "KILL").state().reason == "drawdown spike"
        ks.release(actor="gongqing")
        assert ks.engaged() is False

    def test_engage_idempotent(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL", clock=_clock)
        ks.engage(reason="a")
        ks.engage(reason="b")  # overwrites
        assert ks.state().reason == "b" and ks.engaged()

    def test_release_when_not_engaged_is_noop(self, tmp_path):
        ks = KillSwitch(tmp_path / "KILL", clock=_clock)
        ks.release()  # must not raise
        assert ks.engaged() is False

    def test_bare_touch_is_engaged(self, tmp_path):
        p = tmp_path / "KILL"
        p.write_text("")  # engaged by an operator `touch`, no JSON body
        ks = KillSwitch(p, clock=_clock)
        assert ks.engaged() is True
        st = ks.state()
        assert st.engaged and st.reason == ""  # tolerant of missing body

    def test_fail_closed_on_read_error(self, tmp_path, monkeypatch):
        ks = KillSwitch(tmp_path / "KILL", clock=_clock)

        def boom(self):
            raise OSError("fs down")

        monkeypatch.setattr("pathlib.Path.exists", boom)
        assert ks.engaged() is True  # unknown state → assume ENGAGED

    def test_path_helper_is_sibling_of_db(self, tmp_path):
        db = tmp_path / "sub" / "trader.db"
        db.parent.mkdir(parents=True)
        assert kill_switch_path(db) == (tmp_path / "sub" / "KILL_SWITCH").resolve()


class TestHealthIntegration:
    def test_engaged_forces_entries_halted(self):
        now = _clock()
        h = assess_health(market=_fresh(now), portfolio=_fresh(now),
                          kill_switch_engaged=True, kill_switch_reason="manual",
                          now=now)
        assert h.entries_allowed is False
        assert h.level is HealthLevel.UNHEALTHY
        assert any(c.name == "kill_switch" for c in h.checks)
        assert any("kill-switch" in w for w in h.warnings)

    def test_released_with_fresh_data_allows_entries(self):
        now = _clock()
        h = assess_health(market=_fresh(now), portfolio=_fresh(now),
                          kill_switch_engaged=False, now=now)
        assert h.entries_allowed is True

    def test_kill_switch_overrides_even_when_data_fresh(self):
        # Everything else healthy, but the operator halt still wins.
        now = _clock()
        h = assess_health(market=_fresh(now), portfolio=_fresh(now),
                          kill_switch_engaged=True, now=now)
        assert h.entries_allowed is False
