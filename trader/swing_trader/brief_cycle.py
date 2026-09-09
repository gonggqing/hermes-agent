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

from swing_trader.brief import ResearchBrief, ResearchPublication
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


def _scheduled_slot(edition: str, now: datetime) -> datetime:
    """Most recent occurrence of ``edition`` on the Beijing schedule."""

    local = now.astimezone(BEIJING)
    hour = 9 if edition == "morning" else 21
    due = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return due if due <= local else due - timedelta(days=1)


def _edition_id(market: str, edition: str, due: datetime) -> str:
    return f"{due.astimezone(BEIJING).date().isoformat()}:{edition}:{market.lower()}"


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

    def trigger(
        self,
        edition: str,
        *,
        force: bool = False,
        only: Optional[tuple[str, ...]] = None,
    ) -> bool:
        """Start one daemon cycle and return immediately; coalesce overlap.

        ``force`` bypasses the freshness guard (the manual "regenerate now"
        path); ``only`` limits the run to specific markets.
        """

        with self._state_lock:
            if self._running:
                logger.info("brief cycle already running", extra={"edition": edition})
                return False
            self._running = True
        threading.Thread(
            target=self._run_guarded,
            args=(edition,),
            kwargs={"force": force, "only": only},
            daemon=True,
            name=f"finance-{edition}-brief",
        ).start()
        return True

    def regenerate_market(self, market: str) -> bool:
        """Force one market's brief to regenerate now (manual refresh)."""
        edition, _ = latest_due_brief_slot(self.runtime.clock())
        return self.trigger(edition, force=True, only=(market.lower(),))

    def catch_up_if_due(self) -> bool:
        """After restart, run the latest missed 09:00/21:00 edition once."""

        edition, due = latest_due_brief_slot(self.runtime.clock())
        for market in self.markets:
            payload = self._payload(market)
            publication = payload.get("publication") if isinstance(payload, dict) else None
            if (
                isinstance(publication, dict)
                and publication.get("status") == "complete"
                and publication.get("edition_id") == _edition_id(market, edition, due)
            ):
                continue
            narrative = payload.get("narrative") if isinstance(payload, dict) else None
            generated = (
                _parse_ts(narrative.get("generated_at")) if isinstance(narrative, dict) else None
            )
            if generated is None or generated < due.astimezone(generated.tzinfo):
                logger.info(
                    "missed brief cycle detected",
                    extra={"edition": edition, "due": due.isoformat(), "market": market},
                )
                return self.trigger(edition)
        return False

    def _run_guarded(
        self,
        edition: str,
        *,
        force: bool = False,
        only: Optional[tuple[str, ...]] = None,
    ) -> None:
        try:
            self.run_cycle(edition, force=force, only=only)
        finally:
            with self._state_lock:
                self._running = False

    _FRESH_WINDOW = timedelta(hours=4)

    def _is_fresh(
        self,
        payload: Optional[dict],
        market: str,
        edition: str,
        now: datetime,
    ) -> bool:
        """True if this market already holds a narrative for THIS edition,
        generated within the last 4 hours — so a restart that catches up a
        different market never needlessly regenerates (and re-notifies /
        re-archives) the ones that are already current."""
        if not isinstance(payload, dict):
            return False
        publication = payload.get("publication")
        if isinstance(publication, dict):
            due = _scheduled_slot(edition, now)
            if publication.get("status") != "complete" or publication.get(
                "edition_id"
            ) != _edition_id(market, edition, due):
                return False
        narrative = payload.get("narrative")
        if not isinstance(narrative, dict) or narrative.get("edition") != edition:
            return False
        generated = _parse_ts(narrative.get("generated_at"))
        if generated is None:
            return False
        return now - generated <= self._FRESH_WINDOW

    def run_cycle(
        self,
        edition: str,
        *,
        force: bool = False,
        only: Optional[tuple[str, ...]] = None,
    ) -> dict:
        """Run synchronously (used by the worker and deterministic tests).

        ``force`` skips the freshness guard; ``only`` restricts to a subset of
        markets (single-market manual regeneration)."""

        if edition not in _EDITION_LABELS:
            raise ValueError(f"unknown brief edition {edition!r}")
        now = self.runtime.clock()
        due = _scheduled_slot(edition, now)
        markets = only if only is not None else self.markets
        result = {"edition": edition, "completed": [], "failed": [], "skipped": []}
        refresh_marks = getattr(self.runtime, "refresh_portfolio_marks", None)
        if refresh_marks is not None:
            try:
                mark_report = refresh_marks("live")
                logger.info(
                    "live portfolio marks refreshed before brief cycle",
                    extra=mark_report.model_dump(),
                )
            except Exception:
                logger.exception("live portfolio mark refresh failed before brief cycle")
        for market in markets:
            refresh = self.runtime.run_research.get(market)
            if refresh is None:
                result["skipped"].append(market)
                continue
            if not force and self._is_fresh(self._payload(market), market, edition, now):
                logger.info(
                    "brief already fresh; skipping regeneration",
                    extra={"market": market, "edition": edition},
                )
                result["skipped"].append(market)
                continue
            brief = None
            try:
                refresh()
                payload = self._payload(market)
                if not payload:
                    raise RuntimeError("research refresh produced no brief")
                brief = ResearchBrief.model_validate(payload)
                # Never carry an older edition's prose onto a new evidence
                # packet if the primary model fails.
                brief.narrative = None
                publication = ResearchPublication(
                    edition_id=_edition_id(market, edition, due),
                    edition=edition,
                    scheduled_for=due,
                    evidence_as_of=brief.as_of,
                    status="pending",
                )
                brief.publication = publication
                narrative = self.writer.write(
                    brief,
                    market_id=market.upper(),
                    market_label=_LABELS[market],
                    language="zh-CN",
                )
                if narrative is None:
                    failure = "主模型简报生成失败：未返回完整、可验证的研报 JSON"
                    brief.publication = publication.model_copy(
                        update={"status": "narrative_failed", "failure": failure}
                    )
                    brief.uncertainty.append(
                        f"{_EDITION_LABELS[edition]}：{failure}；本版仅展示结构化证据"
                    )
                    self._store(market, brief.model_dump(mode="json"))
                    self._notify(
                        f"⚠️ {_LABELS[market]} {_EDITION_LABELS[edition]}生成失败；"
                        "本版新证据已保存，旧版文字不会与本版数据混合。"
                    )
                    result["failed"].append(market)
                    continue
                narrative = narrative.model_copy(
                    update={"edition": edition, "generated_at": self.runtime.clock()}
                )
                brief.narrative = narrative
                brief.publication = publication.model_copy(update={"status": "complete"})
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
                # If evidence was built successfully, preserve THIS edition's
                # structured packet and expose the failure. Never restore old
                # prose over a newer market snapshot.
                if brief is not None:
                    failure = "研报发布流程异常；本版叙事不可用"
                    brief.narrative = None
                    brief.publication = ResearchPublication(
                        edition_id=_edition_id(market, edition, due),
                        edition=edition,
                        scheduled_for=due,
                        evidence_as_of=brief.as_of,
                        status="narrative_failed",
                        failure=failure,
                    )
                    brief.uncertainty.append(failure)
                    self._store(market, brief.model_dump(mode="json"))
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
