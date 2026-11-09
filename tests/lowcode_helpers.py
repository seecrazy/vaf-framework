"""低代码测试辅助：动态契约的合法输出构造。"""
from vaf.contract.base import Confidence, SkillOutput


class DynamicOutputFactory:
    """为动态生成的输出契约构造合法实例（findings/证据链/证伪齐备）。"""

    @staticmethod
    def build(contract, conclusion, confidence: Confidence):
        return contract(
            conclusion=conclusion,
            confidence=confidence,
            findings=[
                {"finding_id": "f1", "claim": "依据", "evidence_refs": ["ev1"]},
            ],
            evidence=[{"ev_id": "ev1", "content": "证据原文"}],
            falsification=[
                {"signal": "s", "threshold": "t", "consequence": "c"},
            ],
        )


# 兼容测试中的旧名字
TicketOutput = DynamicOutputFactory
