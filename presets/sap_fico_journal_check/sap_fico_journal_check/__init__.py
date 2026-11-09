"""预置领域：sap_fico_journal_check SAP 总账凭证过账前检查（Python 变体，V2.4）

为什么用 Python 而不是零代码 YAML（教学要点）：
- 借贷平衡校验需要对行项目【跨行求和】的聚合计算，内置声明式节点
  只做逐条校验（区间/存在性），聚合超出声明式预处理能力 → 转代码路径；
- 这正是 V2.2 文档所述"何时必须写代码"的典型案例。

SAP FICO 知识锚点（教学口径，实施以系统配置为准）：
- 借贷平衡：总账凭证借贷合计必须相等（FB01/FB50 强制校验）；
- 科目类别：示例采用常见教学区间——100000~399999 资产负债表科目（BS）、
  400000~499999 损益科目（PL）；真实系统以 FS00/SKA1 科目主数据为准；
- CO 分配：损益科目若为初级成本要素，过账必须携带成本对象（成本中心/
  内部订单），否则报 KI235 类错误（"Account requires an assignment to a
  CO object"）——FI/CO 集成最常见报错之一；
- 冲销：冲销凭证常用凭证类型 AB（原凭证反向过账）；
- 期间控制：过账期间须在 OB52 中开放，期间关闭则拒绝过账。
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import Field

from vaf.contract.base import (
    INSUFFICIENT_DATA,
    Confidence,
    DomainInput,
    SkillOutput,
)
from vaf.contract.falsification import FalsificationChecker
from vaf.preprocessor.nodes import RawItem, ValidatorNode
from vaf.preprocessor.pipeline import Preprocessor
from vaf.runtime.steps import Pipeline, SkillStep
from vaf.skill.base import AnalysisSkill, AuditResult, AuditSkill, BaseSkill
from vaf.skill.context import SkillContext

# ---------------------------------------------------------------------------
# L1 契约
# ---------------------------------------------------------------------------


class JournalConclusion(str, Enum):
    VALID = "valid"
    WARNING = "warning"
    REJECTED = "rejected"
    INSUFFICIENT = INSUFFICIENT_DATA


class JournalInput(DomainInput):
    company_code: str = Field(description="公司代码（如 1000）")
    doc_type: str = Field(default="SA", description="凭证类型（SA=总账，AB=冲销）")
    posting_date: str = Field(description="过账日期 YYYY-MM-DD")
    reference: str = ""
    line_items: List[dict] = Field(
        description="行项目 [{account, dc(D|C), amount, cost_center?, tax_code?}]"
    )


class JournalCheckOutput(SkillOutput):
    conclusion: JournalConclusion
    imbalance_amount: Optional[float] = None
    co_issues: List[str] = Field(default_factory=list)
    issues: List[str] = Field(default_factory=list)
    suggestions: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# L2 预处理：跨行聚合校验（声明式做不了 → 代码）
# ---------------------------------------------------------------------------
_BS_RANGE = (100000, 399999)   # 教学口径：资产负债表科目区间
_PL_RANGE = (400000, 499999)   # 教学口径：损益科目区间


def _account_kind(account) -> Optional[str]:
    try:
        acc = int(str(account))
    except (TypeError, ValueError):
        return None
    if _BS_RANGE[0] <= acc <= _BS_RANGE[1]:
        return "BS"
    if _PL_RANGE[0] <= acc <= _PL_RANGE[1]:
        return "PL"
    return None


class BalanceCheckNode(ValidatorNode):
    """跨行聚合：借贷合计差 → FATAL 异常（差值写进 detail 供技能引用）。"""

    name = "balance_check"
    fail_strategy = "skip"

    def validate(self, item: RawItem) -> list:
        from vaf.contract.base import Anomaly, AnomalyLevel

        lines = item.payload.get("line_items", []) or []
        debit = sum(float(l.get("amount", 0)) for l in lines if l.get("dc") == "D")
        credit = sum(float(l.get("amount", 0)) for l in lines if l.get("dc") == "C")
        diff = round(debit - credit, 2)
        if abs(diff) > 0.005:
            return [
                Anomaly(
                    source="validator:balance_check",
                    level=AnomalyLevel.FATAL,
                    detail=f"借贷不平衡：借方合计 {debit:.2f}，贷方合计 {credit:.2f}，差额 {diff:.2f}",
                )
            ]
        return []


class COAssignmentValidator(ValidatorNode):
    """损益科目缺成本对象 → FATAL（对应 SAP KI235 类报错）。"""

    name = "co_assignment"
    fail_strategy = "skip"

    def validate(self, item: RawItem) -> list:
        from vaf.contract.base import Anomaly, AnomalyLevel

        out = []
        for idx, line in enumerate(item.payload.get("line_items", []) or [], start=1):
            if _account_kind(line.get("account")) != "PL":
                continue
            if not line.get("cost_center"):
                out.append(
                    Anomaly(
                        source="validator:co_assignment",
                        level=AnomalyLevel.FATAL,
                        detail=(
                            f"行 {idx} 损益科目 {line.get('account')} 未携带成本中心"
                            f"（SAP 将报 KI235：Account requires an assignment to a CO object）"
                        ),
                    )
                )
        return out


class PostingDateValidator(ValidatorNode):
    """过账日期基本格式检查（期间开放与否需查 OB52，超出静态检查范围）。"""

    name = "posting_date_check"
    fail_strategy = "skip"

    def validate(self, item: RawItem) -> list:
        from datetime import datetime

        from vaf.contract.base import Anomaly, AnomalyLevel

        raw = str(item.payload.get("posting_date", ""))
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return []
        except ValueError:
            return [
                Anomaly(
                    source="validator:posting_date_check",
                    level=AnomalyLevel.FATAL,
                    detail=f"过账日期格式非法：{raw!r}（应为 YYYY-MM-DD）",
                )
            ]


class JournalPreprocessor(Preprocessor):
    def __init__(self) -> None:
        super().__init__()
        self.validators = [PostingDateValidator(), BalanceCheckNode(), COAssignmentValidator()]


# ---------------------------------------------------------------------------
# L3 技能
# ---------------------------------------------------------------------------


class PostingCheckSkill(AnalysisSkill):
    name = "posting_check"
    description = "凭证过账前综合检查（确定性疑点已由预处理标记）"
    version = "1.0.0"
    output_contract = JournalCheckOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        lines = getattr(ctx.domain_input, "line_items", [])
        anomalies = "; ".join(ctx.anomalies_text()) or "无"
        return (
            f"公司代码 {getattr(ctx.domain_input, 'company_code', '')}，"
            f"凭证类型 {getattr(ctx.domain_input, 'doc_type', '')}，"
            f"过账日期 {getattr(ctx.domain_input, 'posting_date', '')}。\n"
            f"行项目：{lines}\n预处理标记：{anomalies}\n"
            "判定规则：任一 FATAL（借贷不平衡/损益缺成本中心/日期非法）→ rejected，"
            "并把差额与问题行写进 imbalance_amount/co_issues/issues；"
            "无 FATAL 但有可优化点（如未填参考号）→ warning 并给 suggestions；"
            "全部通过 → valid。行项目为空 → insufficient_data 并追问。"
        )


class StrictnessGuardSkill(AuditSkill):
    name = "strictness_guard"
    description = "非高置信的 valid 降级为 warning（凭证检查宁严勿松）"
    version = "1.0.0"
    output_contract = JournalCheckOutput

    def build_prompt(self, ctx):  # pragma: no cover
        raise NotImplementedError

    def audit(self, target: SkillOutput, ctx: SkillContext) -> AuditResult:
        if target.conclusion == JournalConclusion.VALID and target.confidence != Confidence.HIGH:
            revision = target.model_copy(
                update={
                    "conclusion": JournalConclusion.WARNING,
                    "confidence": Confidence.MEDIUM,
                    "suggestions": target.suggestions + ["凭证校验置信度不足，建议人工复核"],
                    "compliance_tags": target.compliance_tags + ["audit_downgraded"],
                }
            )
            return AuditResult(passed=False, issues=["非高置信 valid"], revision=revision)
        return AuditResult(passed=True)


class ReversalChecker(FalsificationChecker):
    """冲销凭证（AB）过账后原凭证失效——SAP FI 常识。"""

    checker_id = "reversal_posted"

    def check(self, context: dict) -> bool:
        return str(context.get("input", {}).get("doc_type", "")).upper() == "AB"


# ---------------------------------------------------------------------------
# L4 编排
# ---------------------------------------------------------------------------
SKILLS: List[BaseSkill] = [PostingCheckSkill(), StrictnessGuardSkill()]

PIPELINE = Pipeline(
    name="sap_fico_journal_check",
    domain="sap_fico_journal_check",
    version="1.0.0",
    preprocessor="JournalPreprocessor",
    steps=[SkillStep(name="posting_check", skill="posting_check", on_schema_fail="retry", max_retries=2)],
    audit_skill="strictness_guard",
)


def synthesize(state, outputs):
    out = outputs.get("posting_check")
    if isinstance(out, JournalCheckOutput):
        return out
    return JournalCheckOutput(
        conclusion=JournalConclusion.INSUFFICIENT,
        confidence=Confidence.LOW,
        clarifying_questions=["凭证行项目缺失，请补全借贷行"],
    )


def register(app) -> None:
    app.register_checker(ReversalChecker())
    app.register_domain(
        name="sap_fico_journal_check",
        input_contract=JournalInput,
        pipeline=PIPELINE,
        skills=SKILLS,
        preprocessor=JournalPreprocessor,
        synthesizer=synthesize,
        audit_skill="strictness_guard",
        data_classification="internal",
    )
