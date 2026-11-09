"""VAF 2.0 — Vertical Agent Framework.

垂直领域 Agent 通用可靠性框架：契约层(L1) / 确定性预处理(L2) / 可测试技能(L3) /
编排运行时(L4)，横切安全、配置、容错、可观测与评估闭环。
详见 DESIGN.md。
"""
from .errors import VafError  # noqa: F401

__version__ = "2.2.0"

__all__ = ["VafError", "__version__"]
