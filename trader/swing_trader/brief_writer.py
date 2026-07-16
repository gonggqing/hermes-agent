"""LLM-written, evidence-bound market brief synthesis.

The deterministic :mod:`swing_trader.brief` builder owns measurements and
provenance.  This module reads that complete snapshot and writes the actual
human-facing brief: a market-specific interpretation of how regime, themes,
movers, discovery, news, signals, portfolio risk and unknowns relate.

The writer has no broker, confirmation, risk-engine or ledger mutation path.
Any model/parse/validation failure returns ``None`` so the UI can say that the
brief was not generated; a metric template must never masquerade as research.
"""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Callable, Optional

from swing_trader.brief import (
    ForecastClaim,
    NarrativeSection,
    ResearchBrief,
    ResearchNarrative,
)
from swing_trader.llm import LLMSettings, http_complete
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["ResearchBriefWriter"]

_PROMPT_VERSION = "finance-daily-brief-v2"

_SYSTEM = """You are the senior investment strategist writing the daily market brief for a private investor.

You receive a structured evidence packet produced by deterministic monitors and research agents. Write a real analytical brief, not a template and not a list of metric readings.

Requirements:
- Use only supplied evidence. Never invent macro events, prices, earnings, flows, valuation facts, or sources.
- Do not add remembered market conventions, future dates, reporting dates, lead/lag estimates, or causal claims unless they appear in the packet.
- Adapt the analysis to the named market. US, mainland China, Hong Kong, and Korea have different structures; do not reuse generic market prose.
- Explain relationships: what is driving the tape, whether breadth confirms the index, whether themes and movers agree, what news/signals change the thesis, where evidence conflicts, and what deserves follow-up.
- Extract the useful conclusion from every populated module: regime, portfolio risk when present, movers, themes, discovery, news, signals, events, and uncertainty. Do not mechanically repeat every number or count.
- Distinguish observed facts from your inference. Mention concrete symbols, themes and figures when they support a conclusion.
- A discovery score is only a research-priority input. Do not describe operational checks, approval status, or screening gates in the prose.
- If evidence is missing or contradictory, say exactly how that limits the conclusion. Do not pad the report with generic disclaimers.
- Produce 4-7 substantive sections. Each section should contain analysis specific to today's evidence, not a description of what the UI module does.
- 'watch_next' must contain 2-6 concrete questions, events, levels, symbols or evidence changes to monitor next.
- Also emit 0-12 measurable forecast claims. A claim is a research view, not an order. Use instrument horizons 1/3/5/10/20 sessions, market regime 1/3/5, discovery/theme 5/20/60, and event 1/5. Do not emit a claim when the evidence cannot support a direction and confidence.
- Claims about an instrument must use its exact ticker as entity_key. Claims must name an invalidation condition and cite supplied evidence references (ticker, source URL, or signal source_agent).

Return ONLY one JSON object with this exact shape:
{"headline":"concise market-specific conclusion","summary":"2-4 paragraph executive synthesis","sections":[{"title":"market-specific section title","analysis":"2-5 analytical sentences"}],"watch_next":["concrete follow-up"],"claims":[{"entity_type":"market|instrument|theme|event","entity_key":"exact identifier","claim_type":"regime|swing_direction|discovery|theme|event","direction":"long|short|neutral|risk_on|risk_off|positive|negative","confidence":0.0,"horizons":[1,3,5],"thesis":"evidence-bound claim","invalidation":"observable invalidation","benchmark":"optional ticker","expected_condition":"measurable expected state","evidence_refs":["supplied reference"]}]}
"""

_MARKET_LENSES = {
    "US": (
        "Interpret VIX, index trend and breadth together; then connect sector/theme rotation, "
        "company or ETF signals, catalysts and the portfolio's risk capacity."
    ),
    "CN": (
        "Treat this as an A-share brief: distinguish broad-index participation from domestic "
        "industry and supply-chain rotation, and identify whether leadership is broadening or narrow."
    ),
    "HK": (
        "Treat this as an offshore Hong Kong brief: distinguish Hang Seng/HSTECH market structure "
        "from the behavior of China-linked platform, technology and supply-chain themes."
    ),
    "KR": (
        "Treat this as a Korea semiconductor-chain brief: assess whether memory/HBM and related "
        "equipment or packaging signals reinforce one another, without extrapolating beyond the packet."
    ),
}


def _default_complete(settings: LLMSettings, system: str, prompt: str) -> str:
    # Reasoning providers may spend a substantial part of the output budget
    # before the JSON object. Leave enough room for 4-7 useful sections.
    return http_complete(settings, system, prompt, max_tokens=4800)


def _compact_evidence(brief: ResearchBrief, market_id: str, market_label: str) -> dict:
    """Bound prompt size while retaining every analytical module."""

    discovery = brief.discovery.model_dump(mode="json") if brief.discovery else None
    if discovery is not None:
        discovery["candidates"] = discovery.get("candidates", [])[:8]
        discovery["rejected"] = discovery.get("rejected", [])[:5]

    signals = [row.model_dump(mode="json") for row in brief.signals_today]
    # Debate/LLM synthesis carries more cross-factor information than the
    # underlying single-factor voices; retain it first, then cap prompt size.
    signals.sort(
        key=lambda row: (
            0 if row["source_agent"] == "debate" else 1,
            0 if str(row["source_agent"]).startswith("llm:") else 1,
            -float(row["confidence"]),
            row["symbol"],
        )
    )

    return {
        "market": {"id": market_id.upper(), "label": market_label},
        "trading_date": brief.trading_date,
        "as_of": brief.as_of.isoformat(),
        "mode": brief.mode.value,
        "freshness": brief.freshness.model_dump(mode="json"),
        "regime": brief.regime.model_dump(mode="json") if brief.regime else None,
        "portfolio_risk": brief.risk.model_dump(mode="json") if brief.risk else None,
        "movers": {
            "top": [row.model_dump(mode="json") for row in brief.movers.top],
            "bottom": [row.model_dump(mode="json") for row in brief.movers.bottom],
        },
        "themes": [row.model_dump(mode="json") for row in brief.themes[:10]],
        "discovery": discovery,
        "news": {
            "items": [row.model_dump(mode="json") for row in brief.news.items[:10]],
            "per_symbol_sentiment": brief.news.per_symbol_sentiment,
        },
        "signals": signals[:24],
        "events": brief.events.model_dump(mode="json"),
        "candidate_flow": brief.candidates_today.model_dump(mode="json"),
        "uncertainty": brief.uncertainty[:16],
        "sources": [row.model_dump(mode="json") for row in brief.provenance[:12]],
    }


def _extract_object(raw: str) -> dict:
    """Extract one JSON object from plain or reasoning-wrapped output."""

    decoder = json.JSONDecoder()
    for index, character in enumerate(raw):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and {"headline", "summary", "sections"} <= value.keys():
            return value
    raise ValueError("no complete research-brief JSON object in response")


class ResearchBriefWriter:
    """Stateless-model brief writer with an evidence-hash process cache."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
    ) -> None:
        self.settings = settings
        self._complete = complete or _default_complete
        self._cache: dict[str, ResearchNarrative] = {}
        self._lock = threading.Lock()

    def write(
        self,
        brief: ResearchBrief,
        *,
        market_id: str,
        market_label: str,
        language: str = "zh-CN",
    ) -> Optional[ResearchNarrative]:
        evidence = _compact_evidence(brief, market_id, market_label)
        encoded = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        # The loop republishes the same evidence at monitor/research/send
        # boundaries with a fresh clock value. Cache by substantive evidence,
        # not by those moving timestamps, so one unchanged packet costs one
        # completion while changed signals/news still trigger a new analysis.
        stable_evidence = json.loads(encoded)
        stable_evidence.pop("as_of", None)
        for key in (
            "market_as_of",
            "news_as_of",
            "portfolio_as_of",
            "market_age_minutes",
            "news_age_minutes",
            "portfolio_age_minutes",
        ):
            stable_evidence.get("freshness", {}).pop(key, None)
        stable_encoded = json.dumps(stable_evidence, ensure_ascii=False, sort_keys=True)
        cache_key = hashlib.sha256(f"{language}\n{stable_encoded}".encode()).hexdigest()
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(deep=True)

        prompt = (
            f"Write in {language}. Keep financial instrument names and tickers exact.\n"
            f"MARKET_LENS={_MARKET_LENSES.get(market_id.upper(), 'Use the named market structure.')}\n"
            f"EVIDENCE_PACKET={encoded}"
        )
        try:
            raw = self._complete(self.settings, _SYSTEM, prompt)
        except Exception as exc:  # transport/provider failures are not repaired
            logger.warning(
                "research narrative skipped",
                extra={"market": market_id.upper(), "error": str(exc)[:200]},
            )
            return None

        try:
            data = _extract_object(raw)
            sections = [NarrativeSection.model_validate(row) for row in data["sections"]]
            narrative = ResearchNarrative(
                generated_at=brief.as_of,
                market=market_id.upper(),
                language=language,
                model=self.settings.model,
                prompt_version=_PROMPT_VERSION,
                evidence_hash=cache_key,
                headline=str(data["headline"]).strip(),
                summary=str(data["summary"]).strip(),
                sections=sections,
                watch_next=[
                    str(row).strip() for row in data.get("watch_next", []) if str(row).strip()
                ],
                claims=[
                    ForecastClaim.model_validate(row)
                    for row in data.get("claims", [])
                ],
            )
        except Exception as first_exc:
            # Reasoning models occasionally use their output budget before
            # closing the JSON object. One strict repair attempt is preferable
            # to a blank brief; persistent bad output still fails honestly.
            retry_prompt = (
                "STRICT JSON RETRY. Return no reasoning, markdown, code fence, or text outside "
                "the JSON object. Keep the summary concise and every section under 120 words.\n"
                + prompt
            )
            try:
                raw = self._complete(self.settings, _SYSTEM, retry_prompt)
                data = _extract_object(raw)
                narrative = ResearchNarrative(
                    generated_at=brief.as_of,
                    market=market_id.upper(),
                    language=language,
                    model=self.settings.model,
                    prompt_version=_PROMPT_VERSION,
                    evidence_hash=cache_key,
                    headline=str(data["headline"]).strip(),
                    summary=str(data["summary"]).strip(),
                    sections=[NarrativeSection.model_validate(row) for row in data["sections"]],
                    watch_next=[
                        str(row).strip() for row in data.get("watch_next", []) if str(row).strip()
                    ],
                    claims=[
                        ForecastClaim.model_validate(row)
                        for row in data.get("claims", [])
                    ],
                )
            except Exception as retry_exc:  # never break the market loop
                logger.warning(
                    "research narrative skipped",
                    extra={
                        "market": market_id.upper(),
                        "error": str(retry_exc)[:160],
                        "first_error": str(first_exc)[:160],
                    },
                )
                return None

        with self._lock:
            self._cache[cache_key] = narrative.model_copy(deep=True)
        return narrative
