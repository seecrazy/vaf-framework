"""技能基类（DESIGN §6.1 / C2 / C6 / C7）。

V2.0 修订：
- 统一 build_prompt() 钩子，执行器零 isinstance 分发（C6）；
- AnalysisSkill 的 output_contract 在类定义期校验必须是 SkillOutput 子类（C2）；
- AuditSkill 返回类型化 AuditResult，revision 必须过同一套契约校验（C7）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Type

from pydantic import BaseModel, ConfigDict, Field

from ..contract.base import SkillOutput
from ..errors import ContractError
from .context import SkillContext


class BaseSkill(ABC):
    name: str = ""
    description: str = ""
    version: str = "1.0.0"
    output_contract: Type[BaseModel]
    grounding_required: bool = False
    grounding_min: float = 0.6
    stream_enabled: bool = False
    # 提示词模板路由（V2.1 P1-2）：None → 框架核心模板；自定义模板须支持同套占位符
    prompt_template_id: Optional[str] = None
    # 类别基类标记：AnalysisSkill 等中间基类豁免 name/contract 校验
    _category_base: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_category_base"):
            return
        # 显式定义为空串立即报错；完全未定义留待注册期兜底（app.register_domain）
        name = cls.__dict__.get("name")
        if name is not None and name == "":
            raise ContractError(f"skill class '{cls.__name__}' must define a non-empty name")
        contract = cls.__dict__.get("output_contract")
        if contract is not None and not (isinstance(contract, type) and issubclass(contract, BaseModel)):
            raise ContractError(
                f"skill '{cls.__name__}' output_contract must be a pydantic BaseModel subclass"
            )

    # -- 声明面 ------------------------------------------------------------
    def tools(self) -> List[str]:
        """可调用工具白名单；未声明则不可调用。"""
        return []

    def knowledge_bases(self) -> List[str]:
        return []

    def few_shot_sets(self) -> List[str]:
        return []

    def degrade_strategy(self) -> str:
        """技能级降级：retry | degrade_model | rule_fallback | abort。"""
        return "retry"

    def model_tier(self) -> int:
        """模型偏好 tier（数值越小越轻量；具体语义由部署方定义）。"""
        return 1

    # -- 执行面 ------------------------------------------------------------
    @abstractmethod
    def build_prompt(self, ctx: SkillContext) -> str:
        """领域专属推理指令（统一钩子；通用规则由 PromptManager 注入）。"""


class AnalysisSkill(BaseSkill):
    """分析判断类：output_contract 必须是 SkillOutput 子类（含证伪/缺口/证据链）。"""

    _category_base = True

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls.__dict__.get("_category_base"):
            return
        contract = cls.__dict__.get("output_contract")
        if contract is not None and not (
            isinstance(contract, type) and issubclass(contract, SkillOutput)
        ):
            raise ContractError(
                f"AnalysisSkill '{cls.__name__}' output_contract must subclass SkillOutput "
                f"(got {contract!r})"
            )


class ExtractionSkill(BaseSkill):
    """信息抽取类：output_contract 为任意结构化实体模型（无需证伪三件套）。"""


class GenerationSkill(BaseSkill):
    """内容生成类：自然语言输出，滚动校验 + 终检（流式见 executor.stream_execute）。"""

    def build_prompt(self, ctx: SkillContext) -> str:  # pragma: no cover - abstract-ish
        raise NotImplementedError

    def validate_chunk(self, buffer: str) -> List[str]:
        """滚动校验：返回问题清单；非空表示缓冲区内存在违规内容。"""
        return []

    def validate_generation(self, content: str, ctx: SkillContext) -> List[str]:
        """终检：返回问题清单。"""
        return []


class TextOutput(BaseModel):
    """生成类技能的默认输出契约。"""

    model_config = ConfigDict(extra="forbid")

    text: str
    compliance_flags: List[str] = Field(default_factory=list)
    degraded: bool = False


class AuditResult(BaseModel):
    """审核结果（C7）：revision 是 SkillOutput 实例，走同一套契约校验。"""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    issues: List[str] = Field(default_factory=list)
    revision: Optional[SkillOutput] = None
    reviewer_note: str = ""


class AuditSkill(BaseSkill):
    """独立审核类：拥有否决权与修正权（不直接调用 LLM，无需提示词）。"""

    def build_prompt(self, ctx: SkillContext) -> str:  # pragma: no cover - 审核不走 LLM
        raise NotImplementedError("AuditSkill does not use LLM prompting")

    @abstractmethod
    def audit(self, target: SkillOutput, ctx: SkillContext) -> AuditResult:  # pragma: no cover
        raise NotImplementedError
