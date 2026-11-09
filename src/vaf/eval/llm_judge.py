"""LLM Judge 语义评分（V2.10）：配 VAF_LLM_* 即用，治理规则强制生效。

评分维度（0-10）：依据合理性（findings 是否支撑结论）、证伪有效性
（falsification 是否可观测可触发）。JudgeGovernance 门槛仍然前置：
未登记/校准不足/换裁判未告警 → 拒绝评分（fail-closed）。
"""
from __future__ import annotations

import json
from typing import Optional

from ..errors import VafError
from ..llm.client import LLMClient
from .judge import JudgeGovernance

JUDGE_PROMPT = """你是评估裁判。对以下 Agent 输出按两个维度打分（0-10 整数）：
1. reasoning_quality：findings 是否足以支撑 conclusion（证据链闭合、无跳跃）
2. falsification_quality：falsification 条件是否可观测、可量化、真能推翻结论

输出仅 JSON：{"reasoning_quality": n, "falsification_quality": n, "rationale": "一句话"}

【输出契约】{schema}
【Agent 输出】{output}
【预期（如有）】{expected}
"""


class LLMJudge:
    def __init__(self, client: LLMClient, governance: Optional[JudgeGovernance] = None):
        self._client = client
        self._governance = governance
        if self._governance is not None:
            self._governance.require_registered()  # fail-closed：治理门槛前置

    def score(self, output: dict, expected_conclusion: Optional[str] = None) -> dict:
        prompt = JUDGE_PROMPT.format(
            schema=json.dumps({"conclusion": "str", "findings": "list", "falsification": "list"}),
            output=json.dumps(output, ensure_ascii=False, default=str)[:4000],
            expected=expected_conclusion or "（无）",
        )
        response = self._client.call([{"role": "user", "content": prompt}])
        try:
            data = json.loads((response.content or "").strip().removeprefix("```json").removesuffix("```"))
            return {
                "reasoning_quality": int(data["reasoning_quality"]),
                "falsification_quality": int(data["falsification_quality"]),
                "rationale": str(data.get("rationale", ""))[:200],
            }
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            raise VafError(f"裁判输出无法解析：{exc}；raw={str(response.content)[:120]}") from exc
