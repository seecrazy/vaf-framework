"""L1 契约层（DESIGN §4）：领域输入/输出契约、类型化证据链、哨兵机制。

V2.0 关键修订：
- 全 Pydantic v2（ConfigDict）；
- findings/evidence/sources 三级引用链，引用闭合性由校验器强制（C10）；
- insufficient_data 哨兵：差异化豁免 findings/falsification，要求 clarifying_questions（C4/C5）；
- mandatory_fields() 由 model_fields 元数据自动推导，消除双重事实来源（C3）。
"""
from __future__ import annotations

import inspect
from enum import Enum
from typing import Any, List, Literal, Optional, Type

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..errors import ContractError

# ---------------------------------------------------------------------------
# 哨兵：框架级保留结论值（C4）。领域结论枚举必须包含该成员（注册期校验）。
# ---------------------------------------------------------------------------
INSUFFICIENT_DATA = "insufficient_data"


def is_insufficient(conclusion: object) -> bool:
    if isinstance(conclusion, str):
        return conclusion == INSUFFICIENT_DATA
    return getattr(conclusion, "value", None) == INSUFFICIENT_DATA


def conclusion_enum(output_cls: Type["SkillOutput"]) -> Optional[Type[Enum]]:
    """提取输出契约 conclusion 字段的枚举类型（若有）。"""
    field_info = output_cls.model_fields.get("conclusion")
    if field_info is None:
        return None
    ann = field_info.annotation
    if inspect.isclass(ann) and issubclass(ann, Enum):
        return ann
    # Optional[Enum] / Union 情形
    for arg in getattr(ann, "__args__", ()):
        if inspect.isclass(arg) and issubclass(arg, Enum):
            return arg
    return None


def ensure_insufficient_member(output_cls: Type["SkillOutput"]) -> None:
    """领域注册期 fail-fast：结论枚举必须包含哨兵值，否则注册失败。"""
    enum_cls = conclusion_enum(output_cls)
    if enum_cls is None:
        return  # conclusion 收窄为 str 的领域，运行期仍受 is_insufficient 判定保护
    for member in enum_cls:
        if getattr(member, "value", None) == INSUFFICIENT_DATA:
            return
    raise ContractError(
        f"output contract '{output_cls.__name__}' conclusion enum '{enum_cls.__name__}' "
        f"must contain the framework sentinel member '{INSUFFICIENT_DATA}'"
    )


# ---------------------------------------------------------------------------
# 基础枚举
# ---------------------------------------------------------------------------
class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SourceGrade(str, Enum):
    """L1 原始权威 / L2 二手整理 / L3 线索。"""

    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class AnomalyLevel(str, Enum):
    FATAL = "fatal"
    WARNING = "warning"
    INFO = "info"


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
class DataGap(BaseModel):
    """数据缺口：缺失必须显式声明，禁止沉默跳过。"""

    model_config = ConfigDict(extra="forbid")

    item: str
    reason: Literal["unavailable", "error", "not_disclosed", "out_of_scope"]
    impact: Literal["none", "minor", "significant"]


class Anomaly(BaseModel):
    """全链路统一的异常标记结构（预处理节点失败、校验疑点、grounding 未达标等）。"""

    model_config = ConfigDict(extra="forbid")

    source: str
    level: AnomalyLevel = AnomalyLevel.WARNING
    detail: str = ""


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref_id: str
    grade: SourceGrade
    label: str = ""


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ev_id: str
    content: str
    source_ref: Optional[str] = None


class Finding(BaseModel):
    """类型化 finding：claim 必须挂接证据引用（C10），支撑自动审计与 grounding。"""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    claim: str
    evidence_refs: List[str] = Field(default_factory=list)
    confidence: Optional[Confidence] = None


class FalsificationCondition(BaseModel):
    """证伪条件：可观测、可量化，可绑定可执行校验器。"""

    model_config = ConfigDict(extra="forbid")

    signal: str = Field(description="可观测的具体信号")
    threshold: str = Field(description="触发阈值")
    consequence: str = Field(description="推翻的具体判断")
    check_frequency: Literal["realtime", "daily", "weekly", "event_driven"] = "realtime"
    checker_id: Optional[str] = Field(None, description="绑定的可执行校验器ID")


# ---------------------------------------------------------------------------
# 领域输入基类
# ---------------------------------------------------------------------------
class DomainInput(BaseModel):
    """所有领域输入的基类。extra=forbid 强制结构化。"""

    model_config = ConfigDict(extra="forbid")

    raw_id: str
    source: str
    timestamp: str


class MultimodalInput(DomainInput):
    """多模态输入基类。"""

    text_content: Optional[str] = None
    image_urls: List[str] = Field(default_factory=list)
    document_url: Optional[str] = None
    audio_url: Optional[str] = None


# ---------------------------------------------------------------------------
# 技能输出基类：类型化证据链 + 哨兵差异化校验
# ---------------------------------------------------------------------------
class SkillOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conclusion: str = Field(description="核心结论；领域子类应收窄为枚举（须含哨兵值）")
    confidence: Confidence

    # 类型化证据链（C10）
    findings: List[Finding] = Field(default_factory=list, description="核心依据")
    evidence: List[Evidence] = Field(default_factory=list, description="证据池")
    sources: List[SourceRef] = Field(default_factory=list, description="来源分级")
    assumptions: List[str] = Field(default_factory=list, description="前置假设")
    falsification: List[FalsificationCondition] = Field(default_factory=list, description="证伪条件")
    data_gaps: List[DataGap] = Field(default_factory=list, description="数据缺口")

    # 交互回路（A1）：仅哨兵场景允许非空
    clarifying_questions: List[str] = Field(default_factory=list)

    # 合规元数据
    data_classification: Literal["public", "internal", "sensitive", "confidential"] = "internal"
    compliance_tags: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_contract(self) -> "SkillOutput":
        insufficient = is_insufficient(self.conclusion)

        # 1) 差异化强制（C5）：哨兵场景豁免 findings/falsification，但必须提出澄清问题
        if insufficient:
            if not self.clarifying_questions:
                raise ValueError(
                    f"sentinel conclusion '{INSUFFICIENT_DATA}' requires at least one clarifying_question"
                )
        else:
            if not self.findings:
                raise ValueError("non-sentinel conclusion requires at least one finding")
            if not self.falsification:
                raise ValueError("non-sentinel conclusion requires at least one falsification condition")
            if self.clarifying_questions:
                raise ValueError("clarifying_questions are only allowed with the insufficient_data sentinel")

        # 2) 引用闭合（C10）
        ev_ids = {e.ev_id for e in self.evidence}
        for finding in self.findings:
            for ref in finding.evidence_refs:
                if ref not in ev_ids:
                    raise ValueError(
                        f"finding '{finding.finding_id}' references unknown evidence '{ref}'"
                    )
        src_ids = {s.ref_id for s in self.sources}
        for ev in self.evidence:
            if ev.source_ref is not None and ev.source_ref not in src_ids:
                raise ValueError(f"evidence '{ev.ev_id}' references unknown source '{ev.source_ref}'")

        # 3) ev_id / ref_id / finding_id 唯一性
        if len(ev_ids) != len(self.evidence):
            raise ValueError("duplicate ev_id in evidence")
        if len(src_ids) != len(self.sources):
            raise ValueError("duplicate ref_id in sources")
        f_ids = [f.finding_id for f in self.findings]
        if len(set(f_ids)) != len(f_ids):
            raise ValueError("duplicate finding_id in findings")
        return self

    @classmethod
    def mandatory_fields(cls) -> List[str]:
        """由 model_fields 元数据自动推导（C3）：
        1. pydantic required 字段（无默认值）；
        2. 声明了 min_length>=1 的集合字段。
        """
        mandatory: List[str] = []
        for name, field_info in cls.model_fields.items():
            if field_info.is_required():
                mandatory.append(name)
                continue
            for meta in field_info.metadata:
                min_len = getattr(meta, "min_length", None)
                if min_len is not None and min_len >= 1:
                    mandatory.append(name)
                    break
        return mandatory


def evidence_pool_texts(output: SkillOutput) -> List[str]:
    """输出自身证据池的文本（grounding / 审计辅助）。"""
    return [e.content for e in output.evidence]
