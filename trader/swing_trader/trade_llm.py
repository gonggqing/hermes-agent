"""High-speed LLM extraction for Finance Bot trade-record messages.

The model only turns free text into a small proposed structure. It cannot
write the portfolio, choose an unverified instrument, or confirm a draft;
instrument search, holdings/account checks and human confirmation remain
deterministic downstream gates.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from swing_trader.llm import LLMSettings, http_complete
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["ExtractedTrade", "LLMTradeExtractor"]

_SYSTEM = """You extract a completed investment trade from one user message.
Return ONLY one JSON object with exactly these fields:
{
  "is_trade": true|false,
  "action": "buy"|"sell"|null,
  "instrument_query": "the exact code or name written by the user"|null,
  "suggested_symbol": "a likely canonical ticker/code"|null,
  "quantity": number|null,
  "price": number|null,
  "price_kind": "trade"|"nav"|"average"|null,
  "account_hint": "account/broker name stated by the user"|null
}
Understand natural Chinese word order, including '以2.1025成交价',
'以2.1025净值', and '以2.1025的价格'. Preserve the user's instrument name in
instrument_query. suggested_symbol is only a search hint; use null when not
confident. Never invent quantity, price, account, or a completed fill. An order
that is merely placed but not filled is not a completed trade."""


@dataclass(frozen=True)
class ExtractedTrade:
    action: str
    instrument_query: str
    suggested_symbol: Optional[str]
    quantity: float
    price: Optional[float]
    price_kind: Optional[str] = None
    account_hint: Optional[str] = None


class LLMTradeExtractor:
    """Stateless, fail-closed trade extractor using the configured fast model."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
        attempts: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self._complete = complete or http_complete
        self._attempts = max(1, int(attempts))
        self._sleep = sleep

    def extract(self, text: str) -> Optional[ExtractedTrade]:
        last_error: Optional[Exception] = None
        for attempt in range(self._attempts):
            try:
                return self._extract_once(text)
            except Exception as exc:  # transient model/API/JSON failure
                last_error = exc
                if attempt + 1 < self._attempts:
                    self._sleep(0.5 * (attempt + 1))
        logger.warning(
            "llm trade extraction failed",
            extra={
                "model": self.settings.model,
                "attempts": self._attempts,
                "error": str(last_error)[:200],
            },
        )
        return None

    def _extract_once(self, text: str) -> Optional[ExtractedTrade]:
        try:
            raw = self._complete(
                self.settings,
                _SYSTEM,
                json.dumps({"message": text[:2000]}, ensure_ascii=False),
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match is None:
                raise ValueError("no JSON object in response")
            data = json.loads(match.group(0))
            if data.get("is_trade") is not True:
                return None
            action = str(data.get("action") or "").strip().lower()
            if action not in {"buy", "sell"}:
                raise ValueError("invalid trade action")
            query = str(data.get("instrument_query") or "").strip()
            if not query:
                raise ValueError("missing instrument query")
            quantity = float(data["quantity"])
            if quantity <= 0:
                raise ValueError("quantity must be positive")
            price_raw = data.get("price")
            price = None if price_raw is None else float(price_raw)
            if price is not None and price <= 0:
                raise ValueError("price must be positive")
            suggested = str(data.get("suggested_symbol") or "").strip().upper() or None
            price_kind = str(data.get("price_kind") or "").strip().lower() or None
            account = str(data.get("account_hint") or "").strip() or None
            return ExtractedTrade(
                action=action,
                instrument_query=query,
                suggested_symbol=suggested,
                quantity=quantity,
                price=price,
                price_kind=price_kind,
                account_hint=account,
            )
        except Exception:
            # The outer retry loop handles transient API errors and malformed
            # provider responses. It never falls back to regex or a partial
            # draft after retries are exhausted.
            raise
