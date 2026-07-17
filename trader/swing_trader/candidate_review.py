"""Fresh, evidence-bound LLM review after a human first approves an order.

The model is advisory: it may keep the candidate or request a revised card,
but it never approves or submits an order. Every revision must pass the normal
deterministic RiskEngine and receive a second human confirmation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

from swing_trader.interfaces import Bar, NewsItem, Quote
from swing_trader.llm import LLMSettings, http_complete
from swing_trader.log import get_logger
from swing_trader.schemas import CandidateOrder

logger = get_logger(__name__)

_SYSTEM = """You are the final pre-trade reviewer for a human-confirmed swing order.
Use only the supplied fresh quote, bars, headlines and original candidate.
Return one JSON object, no prose:
{"action":"keep|revise|caution","reason_zh":"concise Chinese reason",
 "qty":number|null,"limit":number|null,"stop":number|null,"tp":number|null}
keep means every numeric field must be null. revise is only for a material,
evidence-supported parameter change. caution means the thesis may have changed
and requests a second human decision without inventing new parameters. Never
change symbol, side, order type or time-in-force. Never claim to approve or
place an order."""


@dataclass(frozen=True)
class CandidateReview:
    action: Literal["keep", "revise", "caution"]
    reason_zh: str
    edits: dict[str, float] = field(default_factory=dict)

    @property
    def needs_reconfirmation(self) -> bool:
        return self.action != "keep"


class LLMCandidateReviewer:
    """Stateless primary-model reviewer; any API/JSON failure returns ``None``."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
    ) -> None:
        self.settings = settings
        # Primary reasoning models can spend most of an 800-token completion
        # budget before emitting the small JSON verdict.  Give this narrow,
        # fail-closed review enough headroom without changing cheap subagent
        # calls that share ``http_complete``.
        self._complete = complete or (
            lambda cfg, system, prompt: http_complete(
                cfg, system, prompt, max_tokens=2_000
            )
        )

    def review(
        self,
        candidate: CandidateOrder,
        quote: Quote,
        bars: list[Bar],
        news: list[NewsItem],
        regime: str,
    ) -> CandidateReview | None:
        payload = {
            "candidate": candidate.model_dump(mode="json"),
            "fresh_quote": {
                "last": quote.last,
                "bid": quote.bid,
                "ask": quote.ask,
                "timestamp": quote.ts.isoformat(),
            },
            "recent_daily_bars": [
                {"date": b.ts.isoformat(), "open": b.open, "high": b.high,
                 "low": b.low, "close": b.close, "volume": b.volume}
                for b in bars[-10:]
            ],
            "recent_headlines": [
                {"timestamp": n.ts.isoformat(), "headline": n.headline,
                 "source": n.source}
                for n in news[:8]
            ],
            "market_regime": regime,
        }
        try:
            raw = self._complete(
                self.settings, _SYSTEM, json.dumps(payload, ensure_ascii=False)
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match is None:
                raise ValueError("no JSON object")
            data = json.loads(match.group(0))
            action = str(data.get("action", "")).lower()
            if action not in {"keep", "revise", "caution"}:
                raise ValueError("invalid review action")
            reason = str(data.get("reason_zh") or "复核未提供原因").strip()[:500]
            edits: dict[str, float] = {}
            for key in ("qty", "limit", "stop", "tp"):
                value = data.get(key)
                if value is not None:
                    number = float(value)
                    if number <= 0:
                        raise ValueError(f"{key} must be positive")
                    edits[key] = number
            if action == "keep" and edits:
                raise ValueError("keep cannot include edits")
            if action == "revise" and not edits:
                action = "caution"
            return CandidateReview(action=action, reason_zh=reason, edits=edits)
        except Exception as exc:  # fail closed in the caller
            logger.warning(
                "post-approval LLM review failed",
                extra={"symbol": candidate.symbol, "model": self.settings.model,
                       "error": str(exc)[:200]},
            )
            return None
