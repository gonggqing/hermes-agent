"""Automatic market-close evaluation for persisted Finance forecasts.

The worker is read-only with respect to trading authority: it fetches daily
price paths and appends outcomes/evaluations to the independent prediction
ledger. It never creates candidates, approvals, orders, fills, or FX actions.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from swing_trader.datafeed import DataFeedError
from swing_trader.interfaces import Bar, DataFeed
from swing_trader.log import get_logger
from swing_trader.prediction_ledger import PredictionLedger
from swing_trader.scheduler import (
    CN_SCHEDULE,
    HK_SCHEDULE,
    KR_SCHEDULE,
    US_SCHEDULE,
    SessionSchedule,
    is_trading_day,
)

logger = get_logger(__name__)

EVALUATOR_VERSION = "adjusted-daily-path-v1"
MARKET_ORDER = ("US", "HK", "CN", "KR")
MARKET_SCHEDULES: dict[str, SessionSchedule] = {
    "US": US_SCHEDULE,
    "HK": HK_SCHEDULE,
    "CN": CN_SCHEDULE,
    "KR": KR_SCHEDULE,
}
MARKET_PROXIES = {
    "US": "SPY",
    "HK": "^HSI",
    "CN": "000001.SS",
    "KR": "^KS11",
}
# Wait for official closes to settle into the daily feed. This is evaluation,
# not execution, so a conservative delay is preferable to an incomplete bar.
MARKET_CLOSE_READY = {
    "US": time(16, 30),
    "HK": time(16, 30),
    "CN": time(15, 30),
    "KR": time(16, 0),
}

_INDEX_ALIASES = {
    ("US", "SOX"): "^SOX",
    ("HK", "HSI"): "^HSI",
    ("HK", "HSTECH"): "^HSTECH",
    ("KR", "KOSPI"): "^KS11",
}
_EVENT_WORDS = {
    "ANNOUNCEMENT",
    "CALL",
    "EARNINGS",
    "EVENT",
    "IPO",
    "RESULT",
    "RESULTS",
    "TO",
    "WEEK",
}
_DATE_FRAGMENT_RE = re.compile(r"(?:19|20)\d{2}(?:[-_/ ]\d{1,2}){0,2}")
_SYMBOL_TOKEN_RE = re.compile(
    r"(?<![A-Z0-9])(?:\^[A-Z0-9]{1,10}|[A-Z0-9][A-Z0-9]{0,9}"
    r"(?:-[A-Z])?(?:\.(?:HK|KS|KQ|SS|SZ|BJ))?)(?![A-Z0-9])"
)


class PermanentUnscorable(ValueError):
    """The claim has no deterministic price representation."""


def _is_price_symbol(symbol: str, market: str) -> bool:
    """Return whether ``symbol`` is an exact ticker for the claim market.

    Forecast prose sometimes supplied event labels such as
    ``GOOGL-EARNINGS-2026-07-22`` as if they were tickers.  Those strings can
    never produce a price series and must be retired as permanently
    unscorable instead of retrying Yahoo on every restart.
    """
    if not symbol or _DATE_FRAGMENT_RE.search(symbol):
        return False
    if any(char in symbol for char in (" ", ":", "/", "_")):
        return False
    if not re.fullmatch(
        r"(?:\^[A-Z0-9]{1,10}|[A-Z0-9]{1,10}(?:-[A-Z])?"
        r"(?:\.(?:HK|KS|KQ|SS|SZ|BJ))?)",
        symbol,
    ):
        return False
    suffix = symbol.rsplit(".", 1)[-1] if "." in symbol else ""
    if market == "CN":
        return suffix in {"SS", "SZ", "BJ"}
    if market == "HK":
        return suffix == "HK" or symbol.startswith("^")
    if market == "KR":
        return suffix in {"KS", "KQ"} or symbol.startswith("^")
    if market == "US":
        return suffix not in {"HK", "KS", "KQ", "SS", "SZ", "BJ"}
    return False


def _event_symbols(entity_key: str, market: str) -> tuple[str, ...]:
    """Extract listed underlyings from a historical event identifier.

    New prompts require one exact ticker per event claim, but old append-only
    records used forms such as ``EARNINGS:GEV/GOOGL/NOW`` and
    ``META-2026-07-29-EARNINGS``.  Resolve only unambiguous listed tickers;
    generic calendar labels and IPO names without a market ticker remain
    auditable ``unscorable`` observations.
    """
    cleaned = _DATE_FRAGMENT_RE.sub(" ", entity_key.upper())
    tokens = [
        token
        for token in _SYMBOL_TOKEN_RE.findall(cleaned)
        if token not in _EVENT_WORDS
    ]
    if "IPO" in entity_key.upper():
        # A company name around an IPO is not evidence that a listed ticker
        # exists.  Require the exchange suffix outside the US market; US IPO
        # labels are likewise not accepted until the claim names a plain exact
        # instrument instead of an event alias.
        return ()
    symbols: list[str] = []
    for token in tokens:
        symbol = _INDEX_ALIASES.get((market, token), token)
        if _is_price_symbol(symbol, market) and symbol not in symbols:
            symbols.append(symbol)
    return tuple(symbols)


@dataclass(frozen=True)
class EvaluationBatchReport:
    market: str
    through_trading_date: str
    due: int
    evaluated: int
    unscorable: int
    deferred: int
    replayed: int


@dataclass(frozen=True)
class _PathResult:
    value: float | None
    return_pct: float
    mfe_pct: float
    mae_pct: float
    observed_at: datetime
    components: tuple[str, ...]


def latest_closed_trading_date(market: str, now: datetime) -> date:
    """Latest session whose delayed daily close should be available."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    key = market.upper()
    schedule = MARKET_SCHEDULES[key]
    local = now.astimezone(schedule.tz)
    candidate = local.date()
    if not (
        is_trading_day(candidate, schedule)
        and local.time() >= MARKET_CLOSE_READY[key]
    ):
        candidate -= timedelta(days=1)
    while not is_trading_day(candidate, schedule):
        candidate -= timedelta(days=1)
    return candidate


class PredictionCloseEvaluator:
    """Idempotent, restart-safe evaluator with a non-blocking poll hook."""

    def __init__(
        self,
        ledger: PredictionLedger,
        feed: DataFeed,
        *,
        clock=lambda: datetime.now(timezone.utc),
        poll_interval: timedelta = timedelta(minutes=10),
    ) -> None:
        self.ledger = ledger
        self.feed = feed
        self.clock = clock
        self.poll_interval = poll_interval
        self._lock = threading.Lock()
        self._running = False
        self._last_trigger: datetime | None = None

    def trigger_due(self) -> bool:
        """Start one background pass; coalesce overlap and rapid polling."""
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        with self._lock:
            if self._running:
                return False
            if self._last_trigger is not None and now - self._last_trigger < self.poll_interval:
                return False
            self._running = True
            self._last_trigger = now
        threading.Thread(
            target=self._run_guarded,
            daemon=True,
            name="finance-prediction-close-evaluator",
        ).start()
        return True

    def _run_guarded(self) -> None:
        try:
            reports = self.run_all_due()
            if any(report.due for report in reports):
                logger.info(
                    "prediction close evaluation completed",
                    extra={
                        "reports": [report.__dict__ for report in reports],
                    },
                )
        except Exception:
            logger.exception("prediction close evaluation pass failed")
        finally:
            with self._lock:
                self._running = False

    def run_all_due(self) -> list[EvaluationBatchReport]:
        now = self.clock()
        return [
            self.run_market(market, latest_closed_trading_date(market, now))
            for market in MARKET_ORDER
        ]

    def run_market(
        self,
        market: str,
        through_trading_date: date | str,
    ) -> EvaluationBatchReport:
        key = market.upper()
        through = (
            through_trading_date
            if isinstance(through_trading_date, date)
            else date.fromisoformat(through_trading_date)
        )
        due = self.ledger.list_due_checkpoints(
            due_on_or_before=through,
            market=key,
            limit=5000,
        )
        cache: dict[str, list[Bar]] = {}
        evaluated = unscorable = deferred = replayed = 0
        for item in due:
            checkpoint = item["checkpoint"]
            try:
                report = self._evaluate_item(item, cache)
            except PermanentUnscorable as exc:
                # Deterministically unsupported claims must not clog the retry
                # queue forever. Record an auditable no-score observation.
                observed_at = self._close_instant(
                    key, date.fromisoformat(checkpoint["due_trading_date"])
                )
                report = self.ledger.evaluate_checkpoint(
                    checkpoint["id"],
                    observed_at=observed_at,
                    source="unscorable:prediction-close-v1",
                    evaluator_version=EVALUATOR_VERSION,
                    payload={"reason": str(exc)},
                )
                unscorable += 1
            except (DataFeedError, OSError, TimeoutError) as exc:
                # Transient data failures stay pending and retry on a later
                # pass; absence of data is never scored as a bad forecast.
                deferred += 1
                logger.warning(
                    "prediction checkpoint deferred",
                    extra={
                        "checkpoint_id": checkpoint["id"],
                        "market": key,
                        "reason": str(exc)[:240],
                    },
                )
                continue
            if report.replayed:
                replayed += 1
            elif report.state != "observed":
                evaluated += 1
        return EvaluationBatchReport(
            market=key,
            through_trading_date=through.isoformat(),
            due=len(due),
            evaluated=evaluated,
            unscorable=unscorable,
            deferred=deferred,
            replayed=replayed,
        )

    def _evaluate_item(self, item: dict[str, Any], cache: dict[str, list[Bar]]):
        checkpoint = item["checkpoint"]
        revision = item["revision"]
        series = item["series"]
        run = item["run"]
        market = series["market"].upper()
        start = date.fromisoformat(run["trading_date"])
        due = date.fromisoformat(checkpoint["due_trading_date"])
        symbols = self._entity_symbols(series, revision)
        entity = self._basket_path(
            symbols,
            start=start,
            due=due,
            market=market,
            cache=cache,
            fixed_baseline=(
                revision.get("baseline_value") if len(symbols) == 1 else None
            ),
        )

        benchmark_symbol = revision.get("benchmark") or MARKET_PROXIES[market]
        benchmark_return = None
        if benchmark_symbol and benchmark_symbol not in symbols:
            benchmark_return = self._basket_path(
                (benchmark_symbol,),
                start=start,
                due=due,
                market=market,
                cache=cache,
            ).return_pct

        return self.ledger.evaluate_checkpoint(
            checkpoint["id"],
            observed_at=entity.observed_at,
            source=f"daily-bars:{type(self.feed).__name__}:adjusted-v1",
            entity_value=entity.value,
            return_pct=entity.return_pct,
            benchmark_return_pct=benchmark_return,
            mfe_pct=entity.mfe_pct,
            mae_pct=entity.mae_pct,
            evaluator_version=EVALUATOR_VERSION,
            payload={
                "components": list(entity.components),
                "start_trading_date": start.isoformat(),
                "due_trading_date": due.isoformat(),
                "benchmark": benchmark_symbol if benchmark_return is not None else None,
            },
        )

    @staticmethod
    def _entity_symbols(series: dict[str, Any], revision: dict[str, Any]) -> tuple[str, ...]:
        entity_type = str(series["entity_type"]).lower()
        entity_key = str(series["entity_key"]).strip().upper()
        market = str(series["market"]).upper()
        if entity_type == "instrument" and entity_key:
            symbol = _INDEX_ALIASES.get((market, entity_key), entity_key)
            if not _is_price_symbol(symbol, market):
                raise PermanentUnscorable(
                    f"instrument claim {entity_key!r} is not an exact {market} ticker"
                )
            return (symbol,)
        if entity_type == "event" and entity_key:
            symbols = _event_symbols(entity_key, market)
            if symbols:
                return symbols
            raise PermanentUnscorable(
                f"event claim {entity_key!r} has no exact listed {market} ticker"
            )
        if entity_type == "market":
            proxy = revision.get("benchmark") or MARKET_PROXIES.get(series["market"].upper())
            if proxy:
                return (str(proxy).upper(),)
        if entity_type == "theme":
            try:
                payload = json.loads(revision.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            leaders = payload.get("leaders") if isinstance(payload, dict) else None
            symbols = tuple(
                normalized
                for raw in (leaders or [])
                if (normalized := _INDEX_ALIASES.get(
                    (market, str(raw).strip().upper()),
                    str(raw).strip().upper(),
                ))
                and _is_price_symbol(normalized, market)
            )
            if symbols:
                return tuple(dict.fromkeys(symbols))
        raise PermanentUnscorable(
            f"{entity_type or 'unknown'} claim {entity_key!r} has no price representation"
        )

    def _basket_path(
        self,
        symbols: tuple[str, ...],
        *,
        start: date,
        due: date,
        market: str,
        cache: dict[str, list[Bar]],
        fixed_baseline: float | None = None,
    ) -> _PathResult:
        paths = [
            self._single_path(
                symbol,
                start=start,
                due=due,
                market=market,
                cache=cache,
                fixed_baseline=fixed_baseline if len(symbols) == 1 else None,
            )
            for symbol in symbols
        ]
        return _PathResult(
            value=paths[0].value if len(paths) == 1 else None,
            return_pct=sum(path.return_pct for path in paths) / len(paths),
            mfe_pct=sum(path.mfe_pct for path in paths) / len(paths),
            mae_pct=sum(path.mae_pct for path in paths) / len(paths),
            observed_at=max(path.observed_at for path in paths),
            components=symbols,
        )

    def _single_path(
        self,
        symbol: str,
        *,
        start: date,
        due: date,
        market: str,
        cache: dict[str, list[Bar]],
        fixed_baseline: float | None,
    ) -> _PathResult:
        bars = cache.get(symbol)
        if bars is None:
            bars = sorted(
                self.feed.get_bars(symbol, timeframe="1d", limit=400),
                key=lambda bar: bar.ts,
            )
            cache[symbol] = bars
        schedule = MARKET_SCHEDULES[market]
        dated = [(bar.ts.astimezone(schedule.tz).date(), bar) for bar in bars]
        end_candidates = [(day, bar) for day, bar in dated if day <= due]
        if not end_candidates or end_candidates[-1][0] != due:
            raise DataFeedError(f"{symbol} adjusted close for {due} is not available")
        baseline_candidates = [(day, bar) for day, bar in dated if day <= start]
        if fixed_baseline is not None and fixed_baseline > 0:
            baseline = float(fixed_baseline)
            baseline_day = start
        elif baseline_candidates:
            baseline_day, baseline_bar = baseline_candidates[-1]
            baseline = baseline_bar.close
        else:
            raise DataFeedError(f"{symbol} has no baseline at or before {start}")
        if baseline <= 0:
            raise PermanentUnscorable(f"{symbol} baseline is not positive")
        path_bars = [bar for day, bar in dated if baseline_day <= day <= due]
        if not path_bars:
            raise DataFeedError(f"{symbol} has no path from {start} to {due}")
        end = end_candidates[-1][1]
        return _PathResult(
            value=end.close,
            return_pct=(end.close / baseline - 1.0) * 100.0,
            mfe_pct=(max(bar.high for bar in path_bars) / baseline - 1.0) * 100.0,
            mae_pct=(min(bar.low for bar in path_bars) / baseline - 1.0) * 100.0,
            observed_at=end.ts,
            components=(symbol,),
        )

    @staticmethod
    def _close_instant(market: str, trading_date: date) -> datetime:
        schedule = MARKET_SCHEDULES[market]
        return datetime.combine(
            trading_date,
            MARKET_CLOSE_READY[market],
            tzinfo=schedule.tz,
        ).astimezone(timezone.utc)
