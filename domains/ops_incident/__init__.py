"""ops_incident 示例领域（DESIGN §16.2）。

演示能力：RouterStep 动态选路（A2）、探测循环（loop_condition）、
工具调用（log_probe）、子流程（sub_pipeline）、告警去重打分预处理。
"""
from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field

from vaf.contract.base import (
    INSUFFICIENT_DATA,
    Anomaly,
    AnomalyLevel,
    Confidence,
    DomainInput,
    SkillOutput,
)
from vaf.preprocessor.nodes import RawItem, ValidatorNode
from vaf.preprocessor.pipeline import Preprocessor
from vaf.runtime.steps import (
    Pipeline,
    RouteRule,
    RouterStep,
    SkillStep,
    SubPipelineStep,
)
from vaf.skill.base import AnalysisSkill, BaseSkill, ExtractionSkill
from vaf.skill.context import SkillContext
from vaf.tools.registry import ToolSpec

# ==========================================================================
# L1 契约
# ==========================================================================


class IncidentConclusion(str, Enum):
    CRITICAL = "critical"
    DEGRADED = "degraded"
    MINOR = "minor"
    BENIGN = "benign"
    INSUFFICIENT = INSUFFICIENT_DATA


class IncidentInput(DomainInput):
    alerts: List[dict] = Field(default_factory=list)
    service: str = ""
    oncall: str = ""


class IncidentTriageOutput(SkillOutput):
    conclusion: IncidentConclusion
    suspected_components: List[str] = Field(default_factory=list)
    blast_radius: str = "unknown"
    suggested_actions: List[str] = Field(default_factory=list)


class HypothesisOutput(SkillOutput):
    conclusion: IncidentConclusion
    hypotheses: List[dict] = Field(default_factory=list)  # {component, guess, confidence}
    next_probe_target: Optional[str] = None


class ProbeOutput(SkillOutput):
    conclusion: IncidentConclusion
    probe_target: str = ""
    evidence_found: bool = False
    probe_summary: str = ""


class ReportInputSpec(BaseModel):
    """喂给子流程的输入规格（与 ReportInput 契约字段对齐）。"""

    raw_id: str
    source: str
    timestamp: str
    root_cause: str
    severity: str


class ReportInput(DomainInput):
    """子流程（incident_report_gen）的输入契约。"""

    root_cause: str
    severity: str


class ReportOutput(SkillOutput):
    conclusion: IncidentConclusion
    report_markdown: str = ""
    notify_channels: List[str] = Field(default_factory=list)


class IncidentReport(BaseModel):
    """主流程最终合成结果。"""

    conclusion: str
    confidence: str
    root_cause: str
    severity: str
    actions: List[str] = Field(default_factory=list)
    probe_rounds: int = 0
    child_report_request_id: str = ""
    falsification_flags: List[str] = Field(default_factory=list)


# ==========================================================================
# L2 预处理
# ==========================================================================


class AlertSanityValidator(ValidatorNode):
    """领域节点：重复告警 / 已关闭告警 / 缺 severity 标记。"""

    name = "alert_sanity"
    fail_strategy = "skip"

    def validate(self, item: RawItem) -> list:
        alerts = item.payload.get("alerts", [])
        seen_keys = set()
        out = []
        closed = 0
        for alert in alerts:
            key = alert.get("alert_key") or alert.get("id", "")
            if alert.get("status") == "closed":
                closed += 1
            if key in seen_keys:
                out.append(
                    Anomaly(
                        source="validator:alert_sanity",
                        level=AnomalyLevel.INFO,
                        detail=f"duplicate alert_key {key}",
                    )
                )
            seen_keys.add(key)
            if not alert.get("severity"):
                out.append(
                    Anomaly(
                        source="validator:alert_sanity",
                        level=AnomalyLevel.WARNING,
                        detail=f"alert {key} missing severity",
                    )
                )
        if closed:
            out.append(
                Anomaly(
                    source="validator:alert_sanity",
                    level=AnomalyLevel.INFO,
                    detail=f"{closed} closed alerts included",
                )
            )
        return out


class IncidentPreprocessor(Preprocessor):
    def __init__(self) -> None:
        super().__init__()
        self.validators = [AlertSanityValidator()]


# ==========================================================================
# 工具
# ==========================================================================

# 模拟日志库（生产中替换为真实日志/指标查询后端）
FAKE_LOGS: dict = {
    "db-proxy": [
        "2026-11-08 10:00:01 ERROR connection pool exhausted (active=64 max=64)",
        "2026-11-08 10:00:05 WARN query latency p99=8.2s",
    ],
    "payment-api": [
        "2026-11-08 10:00:02 ERROR upstream timeout calling db-proxy",
    ],
    "cache-node-1": [
        "2026-11-08 09:59:58 INFO routine healthcheck ok",
    ],
}


def log_probe(arguments: dict) -> dict:
    target = arguments.get("target", "")
    pattern = (arguments.get("pattern") or "").lower()
    lines = FAKE_LOGS.get(target, [])
    matched = [ln for ln in lines if (not pattern) or (pattern in ln.lower())]
    return {
        "target": target,
        "total_lines": len(lines),
        "matched_lines": matched,
        "found": bool(matched),
    }


LOG_PROBE_SPEC = ToolSpec(
    name="log_probe",
    description="探测指定组件的最近日志，返回匹配行",
    params_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "description": "组件名，如 db-proxy"},
            "pattern": {"type": "string", "description": "匹配子串，可选"},
        },
        "required": ["target"],
    },
    idempotent=True,
    has_side_effect=False,
    timeout_s=5.0,
)


# ==========================================================================
# L3 技能
# ==========================================================================


class AlertTriageSkill(AnalysisSkill):
    name = "alert_triage"
    description = "告警批量定性（级别/疑似组件/影响面）"
    version = "1.0.0"
    output_contract = IncidentTriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        alerts = getattr(ctx.domain_input, "alerts", [])
        return (
            f"服务 {getattr(ctx.domain_input, 'service', '')} 收到 {len(alerts)} 条告警："
            f"{alerts}。预处理疑点：{ctx.anomalies_text() or '无'}。\n"
            "给出整体定性（critical/degraded/minor/benign/insufficient_data）、疑似组件与影响面。"
        )


class HypothesisGenSkill(AnalysisSkill):
    name = "hypothesis_gen"
    description = "生成根因假设（附置信度与下一探测目标）"
    version = "1.0.0"
    output_contract = HypothesisOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        triage = ctx.upstream_outputs.get("alert_triage")
        return (
            f"告警定性：{triage.model_dump(mode='json') if triage else '无'}\n"
            "生成 1-3 条根因假设（component/guess/confidence），整体置信度写入 confidence "
            "（high/medium/low），并给出 next_probe_target。"
        )


class ProbeExecutorSkill(AnalysisSkill):
    name = "probe_executor"
    description = "调用 log_probe 验证假设（演示工具循环）"
    version = "1.0.0"
    output_contract = ProbeOutput

    def tools(self) -> List[str]:
        return ["log_probe"]

    def build_prompt(self, ctx: SkillContext) -> str:
        hypothesis = ctx.upstream_outputs.get("hypothesis_gen")
        previous = ctx.upstream_outputs.get("deep_probe") or ctx.upstream_outputs.get("quick_probe")
        return (
            f"假设：{hypothesis.model_dump(mode='json') if hypothesis else '无'}\n"
            f"上一轮探测：{previous.model_dump(mode='json') if previous else '无'}（第 "
            f"{ctx.loop_count + 1} 轮）\n"
            "调用 log_probe 工具验证 next_probe_target；依据返回的 matched_lines 判断 "
            "evidence_found，并说明是否仍有数据缺口。"
        )


class PrepareReportInputSkill(ExtractionSkill):
    name = "prepare_report_input"
    description = "整理子流程所需的结构化输入"
    version = "1.0.0"
    output_contract = ReportInputSpec

    def build_prompt(self, ctx: SkillContext) -> str:
        return (
            "整理事故报告子流程输入：raw_id 取输入 raw_id，source/timestamp 取输入值，"
            "root_cause 依据探测结论一句话概括，severity 取 critical/degraded/minor/benign。"
        )


class RcaSynthesizerSkill(AnalysisSkill):
    name = "rca_synthesizer"
    description = "综合根因结论"
    version = "1.0.0"
    output_contract = IncidentTriageOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        return (
            "综合假设与全部探测结果，给出最终根因结论（conclusion/suspected_components/"
            "blast_radius）与处置建议写入 suggested_actions。"
        )


class ReportSkill(AnalysisSkill):
    name = "generate_report"
    description = "子流程：生成事故报告（Markdown + 通知渠道）"
    version = "1.0.0"
    output_contract = ReportOutput

    def build_prompt(self, ctx: SkillContext) -> str:
        return (
            f"根因：{getattr(ctx.domain_input, 'root_cause', '')}；"
            f"级别：{getattr(ctx.domain_input, 'severity', '')}。"
            "输出报告正文 report_markdown 与通知渠道 notify_channels。"
        )


# ==========================================================================
# Pipeline（主 + 子）
# ==========================================================================
MAIN_PIPELINE = Pipeline(
    name="ops_incident",
    domain="ops_incident",
    version="1.1.0",
    preprocessor="IncidentPreprocessor",
    steps=[
        SkillStep(name="alert_triage", skill="alert_triage"),
        SkillStep(name="hypothesis_gen", skill="hypothesis_gen", depends_on=["alert_triage"]),
        RouterStep(
            name="route_next",
            depends_on=["hypothesis_gen"],
            routes=[
                RouteRule(
                    condition="steps.hypothesis_gen.output.confidence == 'high'",
                    go_to="deep_probe",
                ),
                RouteRule(
                    condition="steps.hypothesis_gen.output.confidence == 'low'",
                    go_to="synthesize_rca",
                ),
            ],
            default_next="quick_probe",
        ),
        SkillStep(
            name="deep_probe",
            skill="probe_executor",
            depends_on=["route_next"],
            condition="routers.route_next.selected == 'deep_probe'",
            loop_condition="len(steps.deep_probe.output.data_gaps) > 0 and loop_count < 2",
            max_loops=2,
            on_schema_fail="retry",
        ),
        SkillStep(
            name="quick_probe",
            skill="probe_executor",
            depends_on=["route_next"],
            condition="routers.route_next.selected == 'quick_probe'",
        ),
        SkillStep(
            name="prepare_report_input",
            skill="prepare_report_input",
            depends_on=["deep_probe", "quick_probe", "hypothesis_gen"],
        ),
        SubPipelineStep(
            name="report_sub",
            pipeline="incident_report_gen",
            depends_on=["prepare_report_input"],
            input_from="prepare_report_input",
        ),
        SkillStep(
            name="synthesize_rca",
            skill="rca_synthesizer",
            depends_on=["hypothesis_gen", "report_sub"],
        ),
    ],
    output_synthesizer="incident_result_synthesizer",
)

REPORT_PIPELINE = Pipeline(
    name="incident_report_gen",
    domain="ops_incident",
    version="1.0.0",
    steps=[
        SkillStep(name="generate_report", skill="generate_report"),
    ],
    output_synthesizer="report_identity_synthesizer",
)

MAIN_SKILLS: List[BaseSkill] = [
    AlertTriageSkill(),
    HypothesisGenSkill(),
    ProbeExecutorSkill(),
    PrepareReportInputSkill(),
    RcaSynthesizerSkill(),
]

REPORT_SKILLS: List[BaseSkill] = [ReportSkill()]


def synthesize_main(state, outputs) -> IncidentReport:
    rca = outputs.get("synthesize_rca")
    probes = sum(
        1
        for _ in [outputs.get("deep_probe"), outputs.get("quick_probe")]
        if _ is not None
    )
    child = state.step_states.get("report_sub")
    child_request = (child.output or {}).get("child_request_id", "") if child and child.output else ""
    if isinstance(rca, IncidentTriageOutput):
        conclusion = getattr(rca.conclusion, "value", rca.conclusion)
        return IncidentReport(
            conclusion=conclusion,
            confidence=rca.confidence.value,
            root_cause=", ".join(rca.suspected_components) or "unknown",
            severity=conclusion,
            actions=rca.suggested_actions,
            probe_rounds=probes,
            child_report_request_id=child_request,
            falsification_flags=list(state.falsification_flags),
        )
    return IncidentReport(conclusion=INSUFFICIENT_DATA, confidence="low")


def synthesize_report(state, outputs):
    return outputs.get("generate_report")


def register(app) -> None:
    """向 VafApp 注册本领域（主流程 + 子流程）。"""
    app.register_tool(log_probe, LOG_PROBE_SPEC)
    app.register_domain(
        name="ops_incident",
        input_contract=IncidentInput,
        pipeline=MAIN_PIPELINE,
        skills=MAIN_SKILLS,
        preprocessor=IncidentPreprocessor,
        synthesizer=synthesize_main,
        data_classification="internal",
    )
    app.register_domain(
        name="incident_report_gen",
        input_contract=ReportInput,
        pipeline=REPORT_PIPELINE,
        skills=REPORT_SKILLS,
        synthesizer=synthesize_report,
        data_classification="internal",
    )
