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
from pathlib import Path
from typing import Callable, Optional

from swing_trader.log import get_logger
from swing_trader.schemas import Direction, Signal

logger = get_logger(__name__)

__all__ = [
    "LLMAnalyst",
    "LLMSettings",
    "hermes_primary_model",
    "http_complete",
    "llm_settings_from_env",
]

_PROVIDER_DEFAULTS = {
    # provider: (base_url, model, api-key env var)
    "deepseek": ("https://api.deepseek.com/v1", "deepseek-v4-flash", "DEEPSEEK_API_KEY"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4", "glm5-turbo", "GLM_API_KEY"),
    # MiniMax China (OpenAI-compatible endpoint). M-series are REASONING models
    # (emit <think>…</think>), so the analyst uses a larger token budget and
    # extracts the trailing JSON object.
    "minimax": ("https://api.minimaxi.com/v1", "MiniMax-M2.7-highspeed", "MINIMAX_CN_API_KEY"),
}

_SYSTEM = (
    "You are a cautious swing-trading analyst for a tiny retail CASH account. "
    "Respond with ONLY a JSON object: "
    '{"direction": "long"|"short"|"neutral", "confidence": 0.0-1.0, '
    '"thesis": "<one sentence, cite the evidence given>"}. '
    "short means avoid/trim (the account cannot short). Be conservative: "
    "prefer neutral when evidence is mixed. When research_context is provided, "
    "GROUND your thesis in it and reference the dated source; never invent facts "
    "beyond the evidence given. Treat older research as background, not as a new "
    "catalyst. Only recent_headlines may support a claim that news is current."
)


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    model: str
    api_key: str
    timeout: float = 20.0


def hermes_primary_model(env: Optional[dict] = None) -> Optional[str]:
    """Read ``model.default`` from the active Hermes ``config.yaml``.

    Finance runs in a small standalone virtualenv and deliberately does not
    import the Hermes CLI/config stack.  Model names are behavioural config,
    not secrets, so the canonical source remains ``config.yaml`` rather than a
    new environment variable.  This tiny reader supports the two shapes Hermes
    accepts (``model: name`` and ``model: {default: name}``) without adding a
    YAML dependency to the trading core.
    """

    e = env if env is not None else os.environ
    home = Path(e.get("HERMES_HOME") or (Path.home() / ".hermes"))
    path = home / "config.yaml"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    in_model = False
    model_indent = 0
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_model = False
            if not stripped.startswith("model:"):
                continue
            value = stripped.partition(":")[2].strip().strip("'\"")
            if value:
                return value
            in_model = True
            model_indent = indent
            continue
        if in_model and indent > model_indent:
            key, sep, value = stripped.partition(":")
            if sep and key.strip() in {"default", "model"}:
                value = value.split("#", 1)[0].strip().strip("'\"")
                return value or None
        elif in_model and indent <= model_indent:
            break
    return None


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
                # Final investment briefs use the same primary model as Hermes.
                # FINANCE_LLM_MODEL remains a backwards-compatible explicit
                # override, but normal configuration lives in config.yaml.
                model = (
                    e.get("FINANCE_LLM_MODEL")
                    or hermes_primary_model(e)
                    or default_model
                )
            return LLMSettings(
                base_url=e.get("FINANCE_LLM_BASE_URL", base).rstrip("/"),
                model=model,
                api_key=key,
            )
    return None


def http_complete(
    settings: LLMSettings,
    system: str,
    prompt: str,
    *,
    max_tokens: int = 800,
) -> str:
    """One stateless OpenAI-compatible completion.

    Shared by the analysis voice and narrow structured extractors. Keeping it
    stateless avoids coupling Finance utility calls to Hermes conversation
    history or prompt caching.
    """
    import requests

    payload = {
        "model": settings.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        # Headroom for reasoning models (MiniMax M-series / deepseek-v4)
        # that spend tokens on <think> before the JSON verdict. Longer
        # structured tasks (the daily brief writer) opt into a larger cap.
        "max_tokens": max_tokens,
    }
    # MiniMax's OpenAI-compatible API otherwise places the complete reasoning
    # trace in `content` before the answer. Long HK/CN evidence packets can
    # consume the completion budget before the final JSON closes. The official
    # provider extension separates reasoning into `reasoning_details`, leaving
    # `content` as the parseable answer. This is stateless, so no reasoning
    # history needs to be replayed. Do not send the extension to other vendors.
    if (
        "minimaxi.com" in settings.base_url.lower()
        or settings.model.lower().startswith("minimax-")
    ):
        payload["reasoning_split"] = True

    resp = requests.post(
        f"{settings.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.api_key}"},
        json=payload,
        timeout=settings.timeout,
    )
    resp.raise_for_status()
    message = resp.json()["choices"][0]["message"]
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content

    # Reasoning providers are not perfectly consistent about the final answer
    # field.  MiniMax has returned either ``reasoning_content`` or structured
    # ``reasoning_details`` when ``reasoning_split`` is enabled.  Recover text
    # from those documented-compatible shapes, while still failing closed when
    # the response contains no usable text at all.
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    details = message.get("reasoning_details")
    if isinstance(details, list):
        parts: list[str] = []
        for item in details:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "reasoning"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
                        break
        joined = "\n".join(parts).strip()
        if joined:
            return joined
    fields = ",".join(sorted(str(key) for key in message))
    raise ValueError(f"completion message has no usable content (fields={fields})")


class LLMAnalyst:
    """Optional per-symbol LLM opinion; ``analyze`` never raises."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
    ) -> None:
        self.settings = settings
        self._complete = complete or http_complete

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
