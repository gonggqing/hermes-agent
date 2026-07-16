"""Twice-daily, all-market investment brief coordination.

The Finance service owns this schedule.  It is intentionally independent of
the web/desktop tab (a page view must never trigger research) and independent
of Hermes agent cron (model drift or an unavailable chat session must never
silently skip a promised brief).

At 09:00 and 21:00 Asia/Shanghai the coordinator runs the same pipeline for
US/HK/CN/KR: refresh deterministic evidence, ask the configured PRIMARY model
for the final synthesis, persist the result, then notify.  The whole cycle
runs on one background thread so data/model latency cannot block Telegram
approval polling or the US order state machine.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from swing_trader.brief import ResearchBrief
from swing_trader.brief_telegram import render_research_brief
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["BriefCycleCoordinator", "latest_due_brief_slot"]

BEIJING = ZoneInfo("Asia/Shanghai")
_MARKETS = ("us", "hk", "cn", "kr")
_LABELS = {
    "us": "美国",
    "hk": "香港",
    "cn": "中国 A 股",
    "kr": "韩国半导体",
}
_EDITION_LABELS = {
    "morning": "🌅 早间简报 · 北京时间 09:00",
    "evening": "🌙 晚间简报 · 北京时间 21:00",
}


def latest_due_brief_slot(now: datetime) -> tuple[str, datetime]:
    """Return the latest promised Beijing brief edition and its instant."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(BEIJING)
    morning = local.replace(hour=9, minute=0, second=0, microsecond=0)
    evening = local.replace(hour=21, minute=0, second=0, microsecond=0)
    if local >= evening:
        return "evening", evening
    if local >= morning:
        return "morning", morning
    return "evening", evening - timedelta(days=1)


def _parse_ts(raw: object) -> Optional[datetime]:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is not None else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


class BriefCycleCoordinator:
    """Sequential all-market brief worker with a non-blocking trigger."""

    def __init__(
        self,
        runtime,
        writer,
        *,
        notify: Optional[Callable[[str], None]] = None,
        markets: tuple[str, ...] = _MARKETS,
    ) -> None:
        self.runtime = runtime
        self.writer = writer
        self.notify = notify
        self.markets = markets
        self._state_lock = threading.Lock()
        self._running = False

    def trigger(self, edition: str) -> bool:
        """Start one daemon cycle and return immediately; coalesce overlap."""

        with self._state_lock:
            if self._running:
                logger.info("brief cycle already running", extra={"edition": edition})
                return False
            self._running = True
        threading.Thread(
            target=self._run_guarded,
            args=(edition,),
            daemon=True,
            name=f"finance-{edition}-brief",
        ).start()
        return True

    def catch_up_if_due(self) -> bool:
        """After restart, run the latest missed 09:00/21:00 edition once."""

        edition, due = latest_due_brief_slot(self.runtime.clock())
        for market in self.markets:
            payload = self._payload(market)
            narrative = payload.get("narrative") if isinstance(payload, dict) else None
            generated = _parse_ts(narrative.get("generated_at")) if isinstance(narrative, dict) else None
            if generated is None or generated < due.astimezone(generated.tzinfo):
                logger.info(
                    "missed brief cycle detected",
                    extra={"edition": edition, "due": due.isoformat(), "market": market},
                )
                return self.trigger(edition)
        return False

    def _run_guarded(self, edition: str) -> None:
        try:
            self.run_cycle(edition)
        finally:
            with self._state_lock:
                self._running = False

    def run_cycle(self, edition: str) -> dict:
        """Run synchronously (used by the worker and deterministic tests)."""

        if edition not in _EDITION_LABELS:
            raise ValueError(f"unknown brief edition {edition!r}")
        result = {"edition": edition, "completed": [], "failed": [], "skipped": []}
        for market in self.markets:
            refresh = self.runtime.run_research.get(market)
            if refresh is None:
                result["skipped"].append(market)
                continue
            try:
                refresh()
                payload = self._payload(market)
                if not payload:
                    raise RuntimeError("research refresh produced no brief")
                brief = ResearchBrief.model_validate(payload)
                # Never carry an older edition's prose onto a new evidence
                # packet if the primary model fails.
                brief.narrative = None
                narrative = self.writer.write(
                    brief,
                    market_id=market.upper(),
                    market_label=_LABELS[market],
                    language="zh-CN",
                )
                if narrative is None:
                    brief.uncertainty.append(
                        f"{_EDITION_LABELS[edition]}：主模型简报生成失败，等待安全重试"
                    )
                    self._store(market, brief.model_dump(mode="json"))
                    self._notify(
                        f"⚠️ {_LABELS[market]} {_EDITION_LABELS[edition]}生成失败；"
                        "结构化证据已保存，没有使用模板或弱模型替代。"
                    )
                    result["failed"].append(market)
                    continue
                narrative = narrative.model_copy(
                    update={"edition": edition, "generated_at": self.runtime.clock()}
                )
                brief.narrative = narrative
                self._store(market, brief.model_dump(mode="json"))
                text = render_research_brief(
                    brief,
                    market_label=_LABELS[market],
                    lang="zh",
                )
                self._notify(f"{_EDITION_LABELS[edition]}\n{text}")
                result["completed"].append(market)
            except Exception:
                logger.exception(
                    "brief cycle market failed",
                    extra={"edition": edition, "market": market},
                )
                result["failed"].append(market)
        logger.info("brief cycle complete", extra=result)
        return result

    def _payload(self, market: str) -> Optional[dict]:
        if market == "us":
            return self.runtime.latest_brief
        return self.runtime.latest_briefs.get(market)

    def _store(self, market: str, payload: dict) -> None:
        if market == "us":
            self.runtime.latest_brief = payload
        else:
            self.runtime.latest_briefs[market] = payload
            if market == "cn":
                self.runtime.latest_brief_cn = payload
        from swing_trader.prediction_ledger import persist_brief_artifacts

        persist_brief_artifacts(self.runtime, market, payload)

    def _notify(self, text: str) -> None:
        if self.notify is None:
            return
        try:
            self.notify(text)
        except Exception:
            logger.warning("brief notification failed")
