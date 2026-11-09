"""medical_triage 示例领域（DESIGN §16.1）。

演示能力：交互澄清回路（A1）、emergency 人工审核（on_timeout 显式 reject）、
条件路由、可执行证伪（体温回检）、审计技能否决权。
依赖 ScriptedMockLLM 提供确定性 LLM 响应（生产替换为真实 Provider 即可）。
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from vaf.contract.base import (
    INSUFFICIENT_DATA,
    Confidence,
    DomainInput,
    SkillOutput,
)
from vaf.contract.falsification import FalsificationChecker
from vaf.preprocessor.builtin import FieldPresenceValidator, PIIDetectionNode
from vaf.preprocessor.nodes import RawItem, ValidatorNode
from vaf.preprocessor.pipeline import Preprocessor
from vaf.runtime.steps import HumanReviewStep, InteractionStep, Pipeline, SkillStep
from vaf.skill.base import (
    AnalysisSkill,
    AuditResult,
    AuditSkill,
    BaseSkill,
    ExtractionSkill,
)
from vaf.skill.context import SkillContext

# ==========================================================================
# L1 契约
# ==========================================================================


class TriageConclusion(str, Enum):
    EMERGENCY = "emergency"
    URGENT = "urgent"
    ROUTINE = "routine"
    SELF_CARE = "self_care"
    INSUFFICIENT = INSUFFICIENT_DATA  # 哨兵成员（注册期校验必需）


class PatientInput(DomainInput):
    chief_complaint: str
    vitals: dict = Field(default_factory=dict)
    medical_history: List[str] = Field(default_factory=list)
    age: int = 0
    gender: str = ""


class TriageOutput(SkillOutput):
    conclusion: TriageConclusion
    red_flags: List[str] = Field(default_factory=list)  # C5 修订：默认空，不逼模型造假
    recommended_department: Optional[str] = None
    suggested_actions: List[str] = Field(default_factory=list)


class SymptomEntities(BaseModel):
    symptoms: List[str] = Field(default_factory=list)
    duration_hours: Optional[float] = None
    severity_1to10: Optional[int] = None
    mentioned_medications: List[str] = Field(default_factory=list)


class TriageResult(SkillOutput):
    """最终合成结果（SkillOutput 子类：可被终审技能否决/修订，C7）。"""

    conclusion: TriageConclusion
    red_flags: List[str] = Field(default_factory=list)
    department: Optional[str] = None
    actions: List[str] = Field(default_factory=list)
    clarification_rounds: int = 0


# ==========================================================================
# L2 预处理
# ==========================================================================
_VITAL_RANGES = {
    "heart_rate": (30, 220, "fatal"),
    "temperature_c": (35.0, 42.0, "fatal"),
    "systolic_bp": (60, 260, "fatal"),
    "resp_rate": (6, 50, "warning"),
    "spo2_pct": (70, 100, "warning"),
}


class VitalsRangeValidator(ValidatorNode):
    """领域节点：嵌套 vitals 字典的区间校验（越界 → 异常标记）。"""

    name = "vitals_range"
    fail_strategy = "skip"

    def validate(self, item: RawItem) -> list:
        from vaf.contract.base import Anomaly, AnomalyLevel

        vitals = item.payload.get("vitals") or {}
        out = []
        for key, (low, high, level) in _VITAL_RANGES.items():
            value = vitals.get(key)
            if value is None:
                continue
            try:
                num = float(value)
            except (TypeError, ValueError):
                continue
            if num < low or num > high:
                out.append(
                    Anomaly(
                        source="validator:vitals_range",
                        level=AnomalyLevel(level),
                        detail=f"{key}={num} outside [{low}, {high}]",
                    )
                )
        return out


class TriagePreprocessor(Preprocessor):
    """必填字段 + 生命体征区间 + 主诉 PII 检出。"""

    def __init__(self) -> None:
        super().__init__()
        self.validators = [
            FieldPresenceValidator(fields=["chief_complaint", "age"]),
            VitalsRangeValidator(),
            PIIDetectionNode(fields=["chief_complaint"]),
        ]


# ==========================================================================
# L3 技能
# ==========================================================================


class SymptomIntakeSkill(ExtractionSkill):
    name = "symptom_intake"
    description = "从主诉中抽取症状实体、病程与严重度"
    version = "1.1.0"
    output_contract = SymptomEntities

    def build_prompt(self, ctx: SkillContext) -> str:
        complaint = getattr(ctx.domain_input, "chief_complaint", "")
        return (
            f"从患者主诉中抽取结构化症状信息。主诉：{complaint}\n"
            "输出 symptoms / duration_hours / severity_1to10 / mentioned_medications。"
        )


class VitalsRiskCheckSkill(AnalysisSkill):
    name = "vitals_risk_check"
    description = "基于生命体征判断生理风险等级（急诊心率阈值运行期可配）"
    version = "1.1.0"
    output_contract = TriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        vitals = getattr(ctx.domain_input, "vitals", {})
        anomalies = "; ".join(ctx.anomalies_text()) or "无"
        # 运行期配置读取（V2.1 P1-3）：阈值来自配置中心，版本随执行计划快照固化
        hr_threshold = 130
        if ctx.config is not None:
            hr_threshold = ctx.config.get("medical.emergency_hr_threshold", 130)
        return (
            f"已知生命体征：{vitals}。预处理疑点：{anomalies}。\n"
            f"判断生理风险：心率<40或>{hr_threshold}、体温>=39.5、收缩压<85或>200 "
            "任一命中即 emergency，否则依据组合给 urgent/routine/self_care；体征全缺则 "
            f"'{INSUFFICIENT_DATA}'。"
        )


class TriageClarifySkill(AnalysisSkill):
    name = "triage_clarify"
    description = "信息不足时生成澄清问题（交互回路，A1）"
    version = "1.1.0"
    output_contract = TriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        answered = ctx.clarifications or {}
        intake = ctx.upstream_outputs.get("symptom_intake")
        vitals = ctx.upstream_outputs.get("vitals_risk_check")
        return (
            f"主诉：{getattr(ctx.domain_input, 'chief_complaint', '')}\n"
            f"生命体征：{getattr(ctx.domain_input, 'vitals', {})}\n"
            f"症状抽取：{intake.model_dump(mode='json') if intake else '无'}\n"
            f"体征风险：{vitals.model_dump(mode='json') if vitals else '无'}\n"
            f"已收集澄清回答：{answered}。\n"
            "若关键信息（起病时间、疼痛性质、伴随症状、意识状态）仍缺失，输出结论 "
            f"'{INSUFFICIENT_DATA}' 并在 clarifying_questions 给出追问；否则输出明确分诊结论。"
        )


class DifferentialDiagnosisSkill(AnalysisSkill):
    name = "differential_diagnosis"
    description = "综合分诊判断"
    version = "1.2.0"
    output_contract = TriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        intake = ctx.upstream_outputs.get("symptom_intake")
        vitals = ctx.upstream_outputs.get("vitals_risk_check")
        return (
            f"症状抽取：{intake.model_dump(mode='json') if intake else '无'}\n"
            f"体征风险：{vitals.model_dump(mode='json') if vitals else '无'}\n"
            "综合给出分诊结论（emergency/urgent/routine/self_care）、red_flags、"
            "建议科室与处置动作；必须给出证伪条件与证据链（findings 引用 evidence）。"
        )


class RiskStratificationSkill(AnalysisSkill):
    name = "risk_stratification"
    description = "中高风险患者的分层复核"
    version = "1.0.0"
    output_contract = TriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        return (
            "对诊断结论做风险分层复核：emergency/urgent 给出转运与监护建议；"
            "routine/self_care 给出观察指标与复诊条件。"
        )


class TriageAuditorSkill(AuditSkill):
    name = "triage_final_auditor"
    description = "终审：低置信度 emergency 降级为 urgent 并留痕（否决权演示）"
    version = "1.0.0"
    output_contract = TriageOutput

    def audit(self, target: SkillOutput, ctx: SkillContext) -> AuditResult:
        if target.conclusion == "emergency" and target.confidence == Confidence.LOW:
            revision = target.model_copy(
                update={
                    "conclusion": TriageConclusion.URGENT,
                    "confidence": Confidence.MEDIUM,
                    "compliance_tags": target.compliance_tags + ["audit_downgraded"],
                }
            )
            return AuditResult(
                passed=False,
                issues=["low-confidence emergency downgraded to urgent by auditor"],
                revision=revision,
            )
        return AuditResult(passed=True)


# ==========================================================================
# 可执行证伪校验器
# ==========================================================================


class FeverRecheckChecker(FalsificationChecker):
    """体温回检：复测体温 < 38.5°C 则推翻发热相关判断。"""

    checker_id = "triage_fever_recheck"

    def check(self, context: dict) -> bool:
        recheck = context.get("recheck_vitals", {})
        temp = recheck.get("temperature_c")
        if temp is None:
            vitals = context.get("input", {}).get("vitals", {})
            temp = vitals.get("temperature_c")
        if temp is None:
            return False
        try:
            return float(temp) < 38.5
        except (TypeError, ValueError):
            return False


# ==========================================================================
# Pipeline
# ==========================================================================
PIPELINE = Pipeline(
    name="medical_triage",
    domain="medical_triage",
    version="1.3.0",
    preprocessor="TriagePreprocessor",
    steps=[
        SkillStep(name="symptom_intake", skill="symptom_intake", parallel_group="intake_group"),
        SkillStep(name="vitals_risk_check", skill="vitals_risk_check", parallel_group="intake_group"),
        InteractionStep(
            name="triage_clarify",
            skill="triage_clarify",
            depends_on=["symptom_intake", "vitals_risk_check"],
            max_rounds=2,
            on_exhausted="abort",
        ),
        SkillStep(
            name="differential_diagnosis",
            skill="differential_diagnosis",
            depends_on=["symptom_intake", "vitals_risk_check", "triage_clarify"],
            on_schema_fail="retry",
            max_retries=2,
        ),
        HumanReviewStep(
            name="emergency_audit",
            depends_on=["differential_diagnosis"],
            trigger_condition="steps.differential_diagnosis.output.conclusion == 'emergency'",
            timeout_seconds=1800,
            on_timeout="reject",  # B6：急诊审核超时策略必须显式配置
            review_fields=["conclusion", "red_flags", "recommended_department"],
            required_role="reviewer",
        ),
        SkillStep(
            name="risk_stratification",
            skill="risk_stratification",
            depends_on=["differential_diagnosis"],
            condition=(
                "steps.differential_diagnosis.output.conclusion in "
                "['emergency', 'urgent']"
            ),
        ),
    ],
    output_synthesizer="triage_result_synthesizer",
    audit_skill="triage_final_auditor",
)

SKILLS: List[BaseSkill] = [
    SymptomIntakeSkill(),
    VitalsRiskCheckSkill(),
    TriageClarifySkill(),
    DifferentialDiagnosisSkill(),
    RiskStratificationSkill(),
    TriageAuditorSkill(),
]


def synthesize(state, outputs) -> TriageResult:
    diagnosis = outputs.get("differential_diagnosis")
    strat = outputs.get("risk_stratification")
    clarify_rounds = len(state.clarifications)
    base = diagnosis if isinstance(diagnosis, TriageOutput) else None
    strat_out = strat if isinstance(strat, TriageOutput) else None
    if base is not None:
        conclusion = base.conclusion  # 主判断来自差分诊断（终审可修订）
        return TriageResult(
            conclusion=conclusion,
            confidence=base.confidence,
            findings=base.findings,
            evidence=base.evidence,
            sources=base.sources,
            assumptions=base.assumptions,
            falsification=base.falsification,
            data_gaps=base.data_gaps,
            red_flags=base.red_flags,
            department=(base.recommended_department or (strat_out.recommended_department if strat_out else None)),
            actions=(strat_out.suggested_actions if strat_out else base.suggested_actions),
            clarification_rounds=clarify_rounds,
            data_classification="sensitive",
        )
    return TriageResult(
        conclusion=TriageConclusion.INSUFFICIENT,
        confidence=Confidence.LOW,
        clarifying_questions=["诊断链路未产出结论，请人工介入"],
        clarification_rounds=clarify_rounds,
    )


PIPELINE_YAML = """
name: medical_triage
domain: medical_triage
version: "1.3.0"
preprocessor: TriagePreprocessor
steps:
  - name: symptom_intake
    type: skill
    skill: symptom_intake
    parallel_group: intake_group
  - name: vitals_risk_check
    type: skill
    skill: vitals_risk_check
    parallel_group: intake_group
  - name: triage_clarify
    type: interaction
    skill: triage_clarify
    depends_on: [symptom_intake, vitals_risk_check]
    max_rounds: 2
    on_exhausted: abort
  - name: differential_diagnosis
    type: skill
    skill: differential_diagnosis
    depends_on: [symptom_intake, vitals_risk_check, triage_clarify]
    on_schema_fail: retry
    max_retries: 2
  - name: emergency_audit
    type: human_review
    depends_on: [differential_diagnosis]
    trigger_condition: "steps.differential_diagnosis.output.conclusion == 'emergency'"
    timeout_seconds: 1800
    on_timeout: reject
    review_fields: [conclusion, red_flags, recommended_department]
    required_role: reviewer
  - name: risk_stratification
    type: skill
    skill: risk_stratification
    depends_on: [differential_diagnosis]
    condition: "steps.differential_diagnosis.output.conclusion in ['emergency', 'urgent']"
output_synthesizer: triage_result_synthesizer
audit_skill: triage_final_auditor
"""


def register(app) -> None:
    """向 VafApp 注册本领域。"""
    app.register_checker(FeverRecheckChecker())
    app.register_domain(
        name="medical_triage",
        input_contract=PatientInput,
        pipeline=PIPELINE,
        skills=SKILLS,
        preprocessor=TriagePreprocessor,
        synthesizer=synthesize,
        audit_skill="triage_final_auditor",
        data_classification="sensitive",
    )
