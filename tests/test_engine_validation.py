"""引擎校验与快照保护测试：Pipeline 加载期校验矩阵 / 计划漂移 fail-fast。"""
import pytest

from vaf.contract.base import DomainInput
from vaf.errors import PipelineValidationError, PlanMismatchError
from vaf.runtime.engine import DomainRegistration, PipelineEngine
from vaf.runtime.steps import (
    HumanReviewStep,
    InteractionStep,
    Pipeline,
    RouteRule,
    RouterStep,
    SkillStep,
    SubPipelineStep,
)


# 复用示例领域的真实技能集
from medical_triage import SKILLS as MED_SKILLS, PatientInput, PIPELINE as MED_PIPELINE


def _engine(app):
    return app.engine


def test_unknown_skill_rejected(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    reg = DomainRegistration(
        name="t",
        input_contract=PatientInput,
        pipeline=Pipeline(
            name="t",
            domain="t",
            steps=[SkillStep(name="s1", skill="ghost_skill")],
        ),
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="unregistered skill"):
        engine.register_domain(reg)


def test_dependency_cycle_rejected(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="cyc",
        domain="t",
        steps=[
            SkillStep(name="a", skill="symptom_intake", depends_on=["b"]),
            SkillStep(name="b", skill="vitals_risk_check", depends_on=["a"]),
        ],
    )
    reg = DomainRegistration(
        name="t",
        input_contract=PatientInput,
        pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="cycle"):
        engine.register_domain(reg)


def test_unknown_dependency_rejected(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="dep",
        domain="t",
        steps=[SkillStep(name="a", skill="symptom_intake", depends_on=["ghost"])],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="unknown step"):
        engine.register_domain(reg)


def test_router_ref_requires_dependency(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="router_ref",
        domain="t",
        steps=[
            RouterStep(name="r", routes=[RouteRule(condition="1 == 1", go_to="x")], default_next="x"),
            SkillStep(
                name="x",
                skill="symptom_intake",
                condition="routers.r.selected == 'x'",  # 未声明 depends_on r
            ),
            SkillStep(name="y", skill="symptom_intake", depends_on=["r"]),
        ],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="does not depend on it"):
        engine.register_domain(reg)


def test_router_cannot_reference_other_router(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="rr",
        domain="t",
        steps=[
            RouterStep(name="r1", routes=[RouteRule(condition="1 == 1", go_to="a")], default_next="a"),
            RouterStep(
                name="r2",
                depends_on=["r1"],
                routes=[RouteRule(condition="routers.r1.selected == 'a'", go_to="a")],
                default_next="a",
            ),
            SkillStep(name="a", skill="symptom_intake", depends_on=["r1", "r2"]),
        ],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="may not reference"):
        engine.register_domain(reg)


def test_router_target_must_exist(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="rt",
        domain="t",
        steps=[
            RouterStep(name="r", routes=[RouteRule(condition="1 == 1", go_to="ghost")], default_next=None),
            SkillStep(name="a", skill="symptom_intake"),
        ],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="unknown step"):
        engine.register_domain(reg)


def test_interaction_requires_analysis_skill(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="ix",
        domain="t",
        steps=[
            InteractionStep(name="ask", skill="symptom_intake"),  # ExtractionSkill，不允许
            SkillStep(name="a", skill="vitals_risk_check", depends_on=["ask"]),
        ],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="AnalysisSkill"):
        engine.register_domain(reg)


def test_sub_pipeline_rejects_pause_steps(build_medical_app):  # noqa: F811
    app = build_medical_app
    engine = _engine(app)
    child = Pipeline(
        name="child_with_pause",
        domain="t",
        steps=[
            HumanReviewStep(name="rev", trigger_condition="1 == 1", on_timeout="reject"),
            SkillStep(name="a", skill="symptom_intake", depends_on=["rev"]),
        ],
    )
    engine.register_domain(
        DomainRegistration(
            name="t",
            input_contract=PatientInput,
            pipeline=child,
            skills={s.name: s for s in MED_SKILLS},
        )
    )
    parent = Pipeline(
        name="parent",
        domain="t",
        steps=[
            SubPipelineStep(name="sub", pipeline="child_with_pause"),
            SkillStep(name="a", skill="symptom_intake", depends_on=["sub"]),
        ],
    )
    with pytest.raises(PipelineValidationError, match="pause-type"):
        engine.register_domain(
            DomainRegistration(
                name="t2",
                input_contract=PatientInput,
                pipeline=parent,
                skills={s.name: s for s in MED_SKILLS},
            )
        )


def test_invalid_condition_expression_rejected_at_load(build_medical_app):  # noqa: F811
    engine = _engine(build_medical_app)
    pipeline = Pipeline(
        name="badexpr",
        domain="t",
        steps=[
            SkillStep(name="a", skill="symptom_intake", condition="eval('boom')"),
            SkillStep(name="b", skill="vitals_risk_check", depends_on=["a"]),
        ],
    )
    reg = DomainRegistration(
        name="t", input_contract=PatientInput, pipeline=pipeline,
        skills={s.name: s for s in MED_SKILLS},
    )
    with pytest.raises(PipelineValidationError, match="invalid condition"):
        engine.register_domain(reg)


# ---------------------------------------------------------------- 快照保护（A3）
def test_plan_mismatch_blocks_resume(build_medical_app):  # noqa: F811
    app = build_medical_app
    patient = {
        "raw_id": "pm-1", "source": "t", "timestamp": "0",
        "chief_complaint": "胸痛", "vitals": {"heart_rate": 130, "temperature_c": 39.6},
        "age": 60, "gender": "m",
    }
    h1 = app.run("medical_triage", patient, "plan-1")
    assert h1.status.value == "waiting_review" or h1.status.value == "waiting_input"

    # 模拟领域升级：注册一个新版本的技能 → plan_hash 变化
    reg = app.engine.domain_of("medical_triage")
    upgraded = reg.skills["symptom_intake"].__class__()
    upgraded.version = "99.0.0"
    reg.skills["symptom_intake"] = upgraded

    with pytest.raises(PlanMismatchError, match="plan changed"):
        app.resume("plan-1")
    # 显式 migrate 放行
    handle = app.resume("plan-1", migrate=True)
    assert handle.status.value in ("waiting_review", "waiting_input", "succeeded")
