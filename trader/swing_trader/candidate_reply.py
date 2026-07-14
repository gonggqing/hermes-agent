"""Fast-LLM Chinese rationale summaries for Telegram candidate outcomes.

The model can translate/summarize evidence only. Status, quantity, prices,
account and execution wording are rendered deterministically elsewhere.
"""

from __future__ import annotations

import json
import re
from typing import Callable, Optional

from swing_trader.llm import LLMSettings, http_complete
from swing_trader.log import get_logger
from swing_trader.schemas import CandidateOrder

logger = get_logger(__name__)

_SYSTEM = """你是投资研究编辑。只返回 JSON：{"reason_zh":"一句简洁中文"}。
将给定候选单的英文研究依据压缩为不超过 100 个汉字的一句话，说明为什么形成该候选单。
只能使用输入里的证据，不得新增事实，不得声称已经下单或成交。"""


class CandidateReasonSummarizer:
    """Stateless, fail-safe rationale translator using the configured fast model."""

    def __init__(
        self,
        settings: LLMSettings,
        complete: Optional[Callable[[LLMSettings, str, str], str]] = None,
    ) -> None:
        self.settings = settings
        self._complete = complete or http_complete

    def summarize(self, candidate: CandidateOrder) -> Optional[str]:
        payload = {
            "symbol": candidate.symbol,
            "side": candidate.side.value,
            "rationale": candidate.rationale,
            "risk_note": candidate.risk_note,
        }
        try:
            raw = self._complete(
                self.settings,
                _SYSTEM,
                json.dumps(payload, ensure_ascii=False),
            )
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            if match is None:
                raise ValueError("no JSON object")
            reason = str(json.loads(match.group(0)).get("reason_zh") or "").strip()
            return reason[:160] or None
        except Exception as exc:
            logger.warning(
                "candidate rationale summary skipped",
                extra={"symbol": candidate.symbol, "error": str(exc)[:160]},
            )
            return None
