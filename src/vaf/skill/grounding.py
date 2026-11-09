"""Grounding 校验（DESIGN §6.6 / B2 / C10）：finding 的证据必须能在证据池中对齐。"""
from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field

from ..contract.base import SkillOutput
from ..observe.metrics import default_metrics

_WS = re.compile(r"\s+")


def normalize(text: str) -> str:
    return _WS.sub("", text).lower()


class GroundingReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float = 1.0
    total_findings: int = 0
    supported_findings: int = 0
    unsupported_findings: List[str] = Field(default_factory=list)
    unresolved_evidence: List[str] = Field(default_factory=list)


class GroundingChecker:
    """证据对齐规则：finding.evidence_refs 解析成功，且被引用 evidence 的内容
    （归一化后，取前 120 字符滑窗）能在证据池文本（输入 payload / 工具结果 / 上游输出）中命中。"""

    PROBE_CHARS = 120

    def __init__(self):
        self._metrics = default_metrics()
        self._score_hist = self._metrics.histogram("grounding_score", "grounding 得分分布")

    def pool_text(self, texts: List[str]) -> str:
        return normalize(" ".join(t for t in texts if t))

    def verify(self, output: SkillOutput, pool_texts: List[str]) -> GroundingReport:
        pool = self.pool_text(pool_texts)
        ev_map = {e.ev_id: normalize(e.content) for e in output.evidence}
        total = len(output.findings)
        supported = 0
        unsupported: List[str] = []
        unresolved: List[str] = []

        for finding in output.findings:
            ok = bool(finding.evidence_refs)
            for ref in finding.evidence_refs:
                content = ev_map.get(ref)
                if content is None:
                    unresolved.append(ref)
                    ok = False
                    continue
                if content and pool and not self._hit(content, pool):
                    ok = False
            if ok:
                supported += 1
            else:
                unsupported.append(finding.finding_id)

        score = (supported / total) if total else 1.0
        self._score_hist.observe(round(score, 4))
        return GroundingReport(
            score=round(score, 4),
            total_findings=total,
            supported_findings=supported,
            unsupported_findings=unsupported,
            unresolved_evidence=unresolved,
        )

    def _hit(self, normalized_content: str, pool: str) -> bool:
        if not normalized_content:
            return True  # 空证据视为未提供内容，由契约层豁免
        if normalized_content in pool:
            return True
        probe = normalized_content[: self.PROBE_CHARS]
        return probe in pool
