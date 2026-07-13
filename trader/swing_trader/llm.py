"""LLM analysis layer (Loop.md §5.3 "upgrade to LLM", §8 model plan).

Adds ONE more voice to the per-symbol debate: an OpenAI-compatible chat model
(DeepSeek / GLM / anything with the same API) reads compact market context
and returns a structured opinion that becomes a normal :class:`Signal`.

Guardrails (Loop.md §3): this layer can only INFLUENCE ANALYSIS QUALITY.
It has no access to the RiskEngine, the ConfirmationService, or the broker;
its output is capped (confidence ≤ 0.8) and any failure — network, timeout,
bad JSON, refusal — degrades to *no signal*, leaving the rule-based agents
in charge. Keys are read from env and never logged.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Callable, Optional

from swing_trader.log import get_logger
from swing_trader.schemas import Direction, Signal

logger = get_logger(__name__)

__all__ = ["LLMAnalyst", "LLMSettings", "llm_settings_from_env"]

_PROVIDER_DEFAULTS = {
    # provider: (base_url, model, api-key env var)
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-v4-flash", "DEEPSEEK_API_KEY"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm5-turbo", "GLM_API_KEY"),
}

_SYSTEM = (
    "You are a cautious swing-trading analyst for a tiny retail CASH account. "
    "Respond with ONLY a JSON object: "
    '{"direction": "long"|"short"|"neutral", "confidence": 0.0-1.0, '
    '"thesis": "<one sentence, cite the evidence given>"}. '
    "short means avoid/trim (the account cannot short). Be conservative: "
    "prefer neutral when evidence is mixed. When research_context is provided, "
    "GROUND your thesis in it and reference the source; never invent facts "
    "beyond the evidence given."
)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str
    timeout: float = 20.0


def llm_settings_from_env(
    env: Optional[dict] = None, *, role: str = "search"
) -> Optional[LLMSettings]:
    """Build settings from env; provider chain deepseek → glm; None if no key.

    ``role`` selects the model tier (Loop.md two-session extension request:
    "search/summary agent uses deepseek-v4-flash to save token fee"):

    - ``"search"`` (default): the search/summary/analysis subagent. Pinned to
      the CHEAP flash model — ``FINANCE_LLM_SEARCH_MODEL`` (default the
      provider's flash: deepseek-v4-flash / glm5-turbo) — so it stays cheap
      even if a pricier decision model is configured via ``FINANCE_LLM_MODEL``.
    - anything else: the general/decision tier — ``FINANCE_LLM_MODEL`` (default
      the provider's own default model).
    """
    e = env if env is not None else os.environ
    provider = e.get("FINANCE_LLM_PROVIDER", "").strip().lower()
    order = [provider] if provider in _PROVIDER_DEFAULTS else list(_PROVIDER_DEFAULTS)
    for name in order:
        base, default_model, key_var = _PROVIDER_DEFAULTS[name]
        key = e.get(key_var, "").strip()
        if key:
            if role == "search":
                model = e.get("FINANCE_LLM_SEARCH_MODEL", default_model)
            else:
                model = e.get("FINANCE_LLM_MODEL", default_model)
            return LLMSettings(
                base_url=e.get("FINANCE_LLM_BASE_URL", base).rstrip("/"),
                model=model,
                api_key=key,
            )
    return None


def _http_complete(settings: LLMSettings, system: str, prompt: str) -> str:
    import requests

    resp = requests.post(
        f"{settings.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        json={
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 300,
        },
        timeout=settings.timeout,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


class LLMAnalyst:
    """Optional per-symbol LLM opinion; ``analyze`` never raises."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
    ) -> None:
        self.settings = settings
        self._complete = complete or _http_complete

    def analyze(
        self,
        symbol: str,
        features: dict,
        headlines: list[str],
        regime: str = "neutral",
        research: Optional[list[str]] = None,
    ) -> Optional[Signal]:
        payload = {
            "symbol": symbol,
            "market_regime": regime,
            "technical_features": features,
            "recent_headlines": headlines[:8],
        }
        if research:  # RAG grounding (Loop.md §5.10) — cite, don't invent
            payload["research_context"] = research[:6]
        prompt = json.dumps(payload, ensure_ascii=False)
        try:
            raw = self._complete(self.settings, _SYSTEM, prompt)
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match is None:
                raise ValueError("no JSON object in response")
            data = json.loads(match.group(0))
            direction = Direction(str(data["direction"]).strip().lower())
            confidence = min(0.8, max(0.0, float(data["confidence"])))
            thesis = str(data.get("thesis", ""))[:400] or "llm opinion"
        except Exception as exc:  # ANY failure -> no signal (fail-safe)
            logger.warning(
                "llm analysis skipped",
                extra={"symbol": symbol, "error": str(exc)[:200]},
            )
            return None
        return Signal(
            source_agent=f"llm:{self.settings.model}",
            symbol=symbol,
            thesis=thesis,
            direction=direction,
            confidence=confidence,
            features_json={"regime": regime, "n_headlines": len(headlines),
                           "n_research": len(research or [])},
        )
