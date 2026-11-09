"""预置领域 credit_review 的代码风格孪生实现（V2.3 预置库）。

与 presets/credit_review/credit_review.domain.yaml 是同一领域的两种接入方式：
- 零代码 YAML：声明式，5 分钟接入（推荐起点）；
- 本 Python 包：完整控制，演示"声明式不够用时的逃生舱"——
  自定义预处理节点（DTI 推算）、类型化技能、终审、证伪、人工审核。

教学要点：
1. 契约用 pydantic 显式建模（等价于 YAML 的字段表）；
2. DTI 推算是确定性计算 → 放在预处理（EnrichNode），不交给 LLM；
3. 高金额预批 → 人工审核（on_timeout 显式 reject，金融场景保守策略）。
风控口径与 YAML 版一致（DTI≥0.50 拒、0.40~0.50 人工、征信<450 拒 / <600 人工）。
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
from vaf.preprocessor.nodes import EnrichNode, RawItem
from vaf.preprocessor.pipeline import Preprocessor
from vaf.runtime.steps import HumanReviewStep, Pipeline, SkillStep
from vaf.skill.base import AnalysisSkill, AuditResult, AuditSkill, BaseSkill
from vaf.skill.context import SkillContext

# ==========================================================================
# L1 契约（等价于 YAML input.fields / outputs.decision）
# ==========================================================================


class Employment(str, Enum):
    STABLE_EMPLOYED = "stable_employed"
    SELF_EMPLOYED = "self_employed"
    CONTRACT = "contract"
    RETIRED = "retired"
    UNEMPLOYED = "unemployed"


class CreditApplication(DomainInput):
    loan_amount: float = Field(gt=1000, le=5_000_000)
    term_months: int = Field(ge=3, le=120)
    monthly_income: float = Field(ge=0)
    monthly_debt_payment: float = Field(ge=0)
    credit_score: int = Field(ge=0, le=1000)
    employment: Employment = Employment.STABLE_EMPLOYED
    loan_purpose: str


class CreditConclusion(str, Enum):
    PRE_APPROVED = "pre_approved"
    MANUAL_REVIEW = "manual_review"
    REJECTED = "rejected"
    INSUFFICIENT = INSUFFICIENT_DATA


class CreditDecision(SkillOutput):
    conclusion: CreditConclusion
    dti_computed: Optional[float] = None
    risk_factors: List[str] = Field(default_factory=list)
    suggested_adjustments: List[str] = Field(default_factory=list)


# ==========================================================================
# L2 预处理：DTI 推算是确定性计算——放这里，绝不交给 LLM
# ==========================================================================
_ANNUAL_RATE = 0.08  # 审慎估算利率（生产应为机构定价）


class DtiComputeNode(EnrichNode):
    """等额本息推算新增月供与 DTI，写入 enriched_data（技能模板可直接引用）。"""

    name = "dti_compute"
    fail_strategy = "fallback"
    fallback_value = {}

    def enrich(self, item: RawItem) -> dict:
        import math

        p = float(item.payload.get("loan_amount", 0))
        n = int(item.payload.get("term_months", 1))
        income = float(item.payload.get("monthly_income", 0)) or 1e-9
        existing = float(item.payload.get("monthly_debt_payment", 0))
        r = _ANNUAL_RATE / 12
        payment = p * r * (1 + r) ** n / ((1 + r) ** n - 1) if p and n else 0.0
        dti = (existing + payment) / income
        return {"estimated_monthly_payment": round(payment, 2), "dti": round(dti, 4)}


class CreditPreprocessor(Preprocessor):
    def __init__(self) -> None:
        super().__init__()
        self.enrichers = [DtiComputeNode()]


# ==========================================================================
# L3 技能
# ==========================================================================


class AssessSkill(AnalysisSkill):
    name = "assess"
    description = "信贷预审（硬规则 + LLM 综合）"
    version = "1.0.0"
    output_contract = CreditDecision

    def build_prompt(self, ctx: SkillContext) -> str:
        app_form = ctx.domain_input.model_dump()
        precomputed = ""
        for item in ctx.preprocessed:
            dti = item.enriched_data.get("dti")
            pay = item.enriched_data.get("estimated_monthly_payment")
            if dti is not None:
                precomputed = f"（预处理已推算：新增月供≈{pay} 元，DTI≈{dti}）"
        return (
            f"申请信息：{app_form} {precomputed}\n"
            "预审规则：用途违规（购房/股市/理财）→ rejected；DTI≥0.50 → rejected；"
            "0.40~0.50 → manual_review（给降额/延展期建议）；征信<450 → rejected，"
            "450~600 → manual_review；unemployed → manual_review；"
            "否则 pre_approved。缺关键信息 → insufficient_data 并追问。"
            "dti_computed 取预处理推算值；风险因子与证据链必填。"
        )


class ApprovalGuardSkill(AuditSkill):
    name = "approval_guard"
    description = "低置信预批降级人工"
    version = "1.0.0"
    output_contract = CreditDecision

    def build_prompt(self, ctx):  # pragma: no cover - 审核不走 LLM
        raise NotImplementedError

    def audit(self, target: SkillOutput, ctx: SkillContext) -> AuditResult:
        if target.conclusion == CreditConclusion.PRE_APPROVED and target.confidence != Confidence.HIGH:
            revision = target.model_copy(
                update={
                    "conclusion": CreditConclusion.MANUAL_REVIEW,
                    "confidence": Confidence.MEDIUM,
                    "compliance_tags": target.compliance_tags + ["audit_downgraded"],
                }
            )
            return AuditResult(passed=False, issues=["低置信预批"], revision=revision)
        return AuditResult(passed=True)


class CreditScoreRecheckChecker(FalsificationChecker):
    checker_id = "credit_score_recheck"

    def check(self, context: dict) -> bool:
        score = context.get("input", {}).get("credit_score")
        return score is not None and float(score) < 450


# ==========================================================================
# L4 编排：比 YAML 版多一个高金额人工审核（代码路径的典型增值点）
# ==========================================================================
SKILLS: List[BaseSkill] = [AssessSkill(), ApprovalGuardSkill()]

PIPELINE = Pipeline(
    name="credit_review_python",
    domain="credit_review_python",
    version="1.0.0",
    preprocessor="CreditPreprocessor",  # 触发 DTI 推算预处理
    steps=[
        SkillStep(name="assess", skill="assess", on_schema_fail="retry", max_retries=2),
        HumanReviewStep(
            name="high_amount_review",
            depends_on=["assess"],
            trigger_condition=(
                "input.loan_amount > 1000000 and "
                "steps.assess.output.conclusion == 'pre_approved'"
            ),
            timeout_seconds=86400,
            on_timeout="reject",  # 金融场景：审核超时保守拒绝
            review_fields=["conclusion", "dti_computed", "risk_factors"],
            required_role="reviewer",
        ),
    ],
    output_synthesizer=None,
    audit_skill="approval_guard",
)


def synthesize(state, outputs):
    decision = outputs.get("assess")
    if isinstance(decision, CreditDecision):
        return decision
    return CreditDecision(
        conclusion=CreditConclusion.INSUFFICIENT,
        confidence=Confidence.LOW,
        clarifying_questions=["预审链路未产出结论，请人工介入"],
    )


def register(app) -> None:
    app.register_checker(CreditScoreRecheckChecker())
    app.register_domain(
        name="credit_review_python",
        input_contract=CreditApplication,
        pipeline=PIPELINE,
        skills=SKILLS,
        preprocessor=CreditPreprocessor,
        synthesizer=synthesize,
        audit_skill="approval_guard",
        data_classification="sensitive",
    )
