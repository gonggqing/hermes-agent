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
    ThesisAction,
)
from swing_trader.llm import LLMSettings, http_complete
from swing_trader.log import get_logger

logger = get_logger(__name__)

__all__ = ["ResearchBriefWriter"]

_PROMPT_VERSION = "finance-daily-brief-v3"

_SYSTEM = """You are the senior investment strategist writing the daily market brief for a private investor.

You receive a structured evidence packet produced by deterministic monitors and research agents. Write a real analytical brief, not a template and not a list of metric readings.

Requirements:
- Use only supplied evidence. Never invent macro events, prices, earnings, flows, valuation facts, or sources.
- Do not add remembered market conventions, future dates, reporting dates, lead/lag estimates, or causal claims unless they appear in the packet.
- Adapt the analysis to the named market. US, mainland China, Hong Kong, and Korea have different structures; do not reuse generic market prose.
- Start from INFORMATION DELTA: explain what changed since the prior published brief, which thesis strengthened/weakened/invalidated, and what remains genuinely unchanged.
- News items carry publication time, age, source quality and prior-brief recurrence. A repeated headline is not a new catalyst. Never describe a recurring article as "news accumulating"; say it adds no new information unless a distinct supplied update changes the thesis.
- Explain relationships: what is driving the tape, whether breadth confirms the index, whether themes and movers agree, what fresh news/signals change the thesis, where evidence conflicts, and what deserves follow-up.
- Extract the useful conclusion from every populated module: regime, portfolio risk when present, movers, themes, discovery, news, signals, events, and uncertainty. Do not mechanically repeat every number or count.
- Distinguish observed facts from your inference. Mention concrete symbols, themes and figures when they support a conclusion.
- A discovery score is only a research-priority input. Do not describe operational checks, approval status, or screening gates in the prose.
- If evidence is missing or contradictory, say exactly how that limits the conclusion. Do not pad the report with generic disclaimers.
- Produce 4-7 substantive sections. Each section should contain analysis specific to today's evidence, not a description of what the UI module does.
- Include a decision-oriented action map only where evidence supports it. Stances are research guidance, never executable orders: buy_on_confirmation, hold, reduce_on_weakness, exit_if_invalidated, watch, avoid. Technical alignment alone is insufficient for buy_on_confirmation: require at least one non-technical support (fresh catalyst, fundamentals, breadth/relative strength, or a clearly supplied thesis update). If it is absent, use watch/hold/avoid and say what evidence is missing.
- For every action view, state what changed, the intended trading-session horizon, an observable invalidation, and supplied evidence references. Prioritize current holdings, material thesis changes and the highest-conviction opportunities; do not fill a quota.
- 'watch_next' must contain 2-6 concrete questions, events, levels, symbols or evidence changes to monitor next.
- Also emit 0-12 measurable forecast claims. A claim is a research view, not an order. Use instrument horizons 1/3/5/10/20 sessions, market regime 1/3/5, discovery/theme 5/20/60, and event 1/5. Do not emit a claim when the evidence cannot support a direction and confidence.
- Claims about an instrument must use its exact listed ticker as entity_key. Event claims are only measurable when tied to one listed instrument: emit one claim per affected ticker and use that exact ticker as entity_key; never use event names, dates, joined ticker lists, private-company names, or labels such as "EARNINGS:..." as entity_key. Claims must name an invalidation condition and cite supplied evidence references (ticker, source URL, or signal source_agent).

Return ONLY one JSON object with this exact shape:
{"headline":"concise market-specific conclusion","summary":"2-4 paragraph executive synthesis","change_summary":["material change since prior brief"],"action_views":[{"symbol":"exact ticker","display_name":"optional supplied name","stance":"buy_on_confirmation|hold|reduce_on_weakness|exit_if_invalidated|watch|avoid","thesis_state":"new|strengthened|unchanged|weakened|invalidated","confidence":0.0,"horizon_sessions":5,"what_changed":"specific delta","rationale":"multi-factor reasoning","invalidation":"observable condition","evidence_refs":["supplied reference"]}],"sections":[{"title":"market-specific section title","analysis":"2-5 analytical sentences"}],"watch_next":["concrete follow-up"],"claims":[{"entity_type":"market|instrument|theme|event","entity_key":"exact identifier","claim_type":"regime|swing_direction|discovery|theme|event","direction":"long|short|neutral|risk_on|risk_off|positive|negative","confidence":0.0,"horizons":[1,3,5],"thesis":"evidence-bound claim","invalidation":"observable invalidation","benchmark":"optional ticker","expected_condition":"measurable expected state","evidence_refs":["supplied reference"]}]}
"""

_MARKET_LENSES = {
    "US": (
        "Interpret VIX, index trend and breadth together; then connect sector/theme rotation, "
        "company or ETF signals, catalysts and the portfolio's risk capacity."
    ),
    "CN": (
        "Treat this as an A-share brief: distinguish broad-index participation from domestic "
        "industry and supply-chain rotation. Compare the static market anchors with the dynamic "
        "cross-industry discovery pool; do not center semiconductors or any legacy watchlist theme "
        "unless today's evidence makes it material. Identify actionable trend changes across "
        "industries, current holdings and newly surfaced instruments."
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
    # Reasoning providers spend a substantial part of the output budget on
    # reasoning BEFORE emitting the JSON, and that reasoning counts against
    # max_tokens. At 4800 the larger markets (US/HK/CN — many sections, dense
    # zh-CN) truncated mid-object, so _extract_object found no complete JSON and
    # the whole narrative was dropped. The isolated brief worker has a
    # five-minute ceiling, so budget generously for reasoning + a full 4-7
    # section report.
    return http_complete(settings, system, prompt, max_tokens=12000)


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
        "current_holdings": [row.model_dump(mode="json") for row in brief.holdings],
        "movers": {
            "top": [row.model_dump(mode="json") for row in brief.movers.top],
            "bottom": [row.model_dump(mode="json") for row in brief.movers.bottom],
        },
        "themes": [row.model_dump(mode="json") for row in brief.themes[:10]],
        "discovery": discovery,
        "news": {
            "items": [row.model_dump(mode="json") for row in brief.news.items[:10]],
            "per_symbol_sentiment": brief.news.per_symbol_sentiment,
            "stale_items_excluded": brief.news.stale_items_excluded,
            "future_items_excluded": brief.news.future_items_excluded,
            "duplicate_items_excluded": brief.news.duplicate_items_excluded,
        },
        "signals": signals[:24],
        "events": brief.events.model_dump(mode="json"),
        "candidate_flow": brief.candidates_today.model_dump(mode="json"),
        "uncertainty": brief.uncertainty[:16],
        "sources": [row.model_dump(mode="json") for row in brief.provenance[:12]],
    }


def _news_key(row: dict) -> str:
    return str(row.get("url") or row.get("headline") or "").strip().casefold()


def _signal_map(payload: dict) -> dict[str, dict]:
    rows = payload.get("signals_today") or []
    return {
        f"{row.get('symbol')}|{row.get('source_agent')}": row
        for row in rows
        if isinstance(row, dict) and row.get("symbol")
    }


def _build_history_context(
    evidence: dict,
    history: list[dict],
) -> dict:
    """Compact deterministic deltas; the model does not rediscover diffs."""

    prior = history[0] if history else None
    prior_news: dict[str, list[str]] = {}
    for payload in history:
        trading_date = str(payload.get("trading_date") or "")
        for row in (payload.get("news") or {}).get("items", []):
            if not isinstance(row, dict):
                continue
            key = _news_key(row)
            if key:
                prior_news.setdefault(key, []).append(trading_date)

    for row in evidence.get("news", {}).get("items", []):
        dates = sorted(set(prior_news.get(_news_key(row), [])))
        row["novel_vs_prior_briefs"] = not dates
        row["seen_in_prior_briefs"] = len(dates)
        row["first_seen_trading_date"] = dates[0] if dates else None

    signal_changes: list[dict] = []
    current_signals = {
        f"{row.get('symbol')}|{row.get('source_agent')}": row
        for row in evidence.get("signals", [])
        if isinstance(row, dict) and row.get("symbol")
    }
    prior_signals = _signal_map(prior or {})
    for key, current in current_signals.items():
        previous = prior_signals.get(key)
        if previous is None:
            state = "new"
        elif (
            previous.get("direction") == current.get("direction")
            and abs(float(previous.get("confidence") or 0) - float(current.get("confidence") or 0)) < 0.05
            and previous.get("thesis") == current.get("thesis")
        ):
            state = "unchanged"
        else:
            state = "changed"
        signal_changes.append(
            {
                "symbol": current.get("symbol"),
                "source_agent": current.get("source_agent"),
                "state": state,
                "previous_direction": previous.get("direction") if previous else None,
                "current_direction": current.get("direction"),
                "previous_confidence": previous.get("confidence") if previous else None,
                "current_confidence": current.get("confidence"),
            }
        )

    prior_themes = {
        str(row.get("theme")): {"rank": index + 1, **row}
        for index, row in enumerate((prior or {}).get("themes") or [])
        if isinstance(row, dict)
    }
    theme_changes = []
    for index, row in enumerate(evidence.get("themes") or []):
        previous = prior_themes.get(str(row.get("theme")))
        theme_changes.append(
            {
                "theme": row.get("theme"),
                "current_rank": index + 1,
                "prior_rank": previous.get("rank") if previous else None,
                "current_dist_sma50_pct": row.get("avg_dist_sma50_pct"),
                "prior_dist_sma50_pct": previous.get("avg_dist_sma50_pct") if previous else None,
            }
        )

    prior_publications = []
    for payload in history[:4]:
        narrative = payload.get("narrative")
        if not isinstance(narrative, dict):
            continue
        prior_publications.append(
            {
                "trading_date": payload.get("trading_date"),
                "edition": narrative.get("edition"),
                "headline": narrative.get("headline"),
                "summary": str(narrative.get("summary") or "")[:700],
                "action_views": (narrative.get("action_views") or [])[:8],
                "claims": (narrative.get("claims") or [])[:10],
            }
        )

    current_regime = evidence.get("regime") or {}
    previous_regime = (prior or {}).get("regime") or {}
    return {
        "has_prior_publication": bool(prior_publications),
        "regime_change": {
            "previous": previous_regime.get("risk_on_off"),
            "current": current_regime.get("risk_on_off"),
            "breadth_previous": previous_regime.get("breadth_pct_above_50dma"),
            "breadth_current": current_regime.get("breadth_pct_above_50dma"),
        },
        "signal_changes": signal_changes[:30],
        "theme_changes": theme_changes[:10],
        "prior_publications": prior_publications,
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


def _validate_action_views(rows: list, brief: ResearchBrief) -> list[ThesisAction]:
    """Keep model guidance inside the supplied research universe.

    This is not trade authority, but invented tickers or a technical-only
    "buy" would still make the human brief unsafe. Enforce those two claims in
    code instead of trusting prompt compliance.
    """

    discovery_symbols = {
        row.symbol for row in (brief.discovery.candidates if brief.discovery else [])
    }
    allowed_symbols = {
        *(row.symbol for row in brief.holdings),
        *(row.symbol for row in brief.signals_today),
        *(row.symbol for row in brief.movers.top),
        *(row.symbol for row in brief.movers.bottom),
        *discovery_symbols,
    }
    display_names = {
        row.symbol: row.display_name
        for row in [*brief.holdings, *brief.movers.top, *brief.movers.bottom]
        if row.display_name
    }
    signal_support: dict[str, set[str]] = {}
    for signal in brief.signals_today:
        supported_source = signal.source_agent in {"fundamental", "sentiment"} or (
            signal.source_agent.startswith("llm:")
            and (
                float(signal.features.get("n_headlines") or 0) > 0
                or float(signal.features.get("n_research") or 0) > 0
            )
        )
        if signal.direction == "long" and supported_source:
            signal_support.setdefault(signal.symbol, set()).add(signal.source_agent)
    fresh_positive_news = {
        item.symbol
        for item in brief.news.items
        if item.symbol and item.sentiment is not None and item.sentiment > 0.2
    }

    actions: list[ThesisAction] = []
    for raw in rows:
        action = ThesisAction.model_validate(raw)
        if action.symbol not in allowed_symbols:
            raise ValueError(f"action symbol not present in evidence: {action.symbol}")
        if action.stance == "buy_on_confirmation" and not (
            action.symbol in discovery_symbols
            or action.symbol in fresh_positive_news
            or signal_support.get(action.symbol)
        ):
            raise ValueError(
                f"technical-only buy_on_confirmation is not allowed: {action.symbol}"
            )
        canonical_name = display_names.get(action.symbol)
        if canonical_name:
            action = action.model_copy(update={"display_name": canonical_name})
        actions.append(action)
    return actions


class ResearchBriefWriter:
    """Stateless-model brief writer with an evidence-hash process cache."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
        history_loader: Optional[Callable[[str, str, int], list[dict]]] = None,
    ) -> None:
        self.settings = settings
        self._complete = complete or _default_complete
        self._history_loader = history_loader
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
        history: list[dict] = []
        if self._history_loader is not None:
            try:
                history = self._history_loader(
                    market_id.lower(), brief.as_of.isoformat(), 6
                )
            except Exception as exc:  # history improves judgment, never availability
                logger.warning(
                    "research history unavailable",
                    extra={"market": market_id.upper(), "error": str(exc)[:160]},
                )
        evidence["history_and_delta"] = _build_history_context(evidence, history)
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
            sections = [
                NarrativeSection.model_validate(row) for row in data["sections"][:7]
            ]
            narrative = ResearchNarrative(
                generated_at=brief.as_of,
                market=market_id.upper(),
                language=language,
                model=self.settings.model,
                prompt_version=_PROMPT_VERSION,
                evidence_hash=cache_key,
                headline=str(data["headline"]).strip(),
                summary=str(data["summary"]).strip(),
                change_summary=[
                    str(row).strip()
                    for row in data.get("change_summary", [])
                    if str(row).strip()
                ][:6],
                action_views=_validate_action_views(
                    data.get("action_views", [])[:12], brief
                )[:12],
                sections=sections,
                watch_next=[
                    str(row).strip() for row in data.get("watch_next", []) if str(row).strip()
                ][:6],
                claims=[
                    ForecastClaim.model_validate(row)
                    for row in data.get("claims", [])[:20]
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
                    change_summary=[
                        str(row).strip()
                        for row in data.get("change_summary", [])
                        if str(row).strip()
                    ][:6],
                    action_views=_validate_action_views(
                        data.get("action_views", [])[:12], brief
                    )[:12],
                    sections=[
                        NarrativeSection.model_validate(row)
                        for row in data["sections"][:7]
                    ],
                    watch_next=[
                        str(row).strip() for row in data.get("watch_next", []) if str(row).strip()
                    ][:6],
                    claims=[
                        ForecastClaim.model_validate(row)
                        for row in data.get("claims", [])[:20]
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
