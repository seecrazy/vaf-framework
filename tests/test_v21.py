"""V2.1 新特性测试：开闭式步骤处理器 / 流式步骤 / 快速构造器 / 模板路由 /
配置读取 / inspect / 通知推送 / 领域发现 / Redis 存储。"""
import json
import threading
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import BaseModel

from e2e_helpers import EMERGENCY_PATIENT, make_app
from vaf.app import VafApp
from vaf.contract.base import (
    INSUFFICIENT_DATA,
    Confidence,
    DomainInput,
    SkillOutput,
    SourceGrade,
)
from vaf.contract.builders import (
    quick_findings,
    quick_falsification,
    quick_insufficient,
    quick_output,
)
from vaf.demo_llm import DemoResponder
from vaf.errors import ContractError, VafError
from vaf.llm.mock import ScriptedMockLLM
from vaf.runtime.engine import PipelineEngine
from vaf.runtime.handlers import StepHandler, StepRunContext
from vaf.runtime.notification import NotificationHub, webhook_sink
from vaf.runtime.persistence import RedisStateStore
from vaf.runtime.steps import GenerationStep, Pipeline, SkillStep, pipeline_from_yaml_text
from vaf.security.permissions import User
from vaf.skill.base import AnalysisSkill, GenerationSkill, TextOutput
from vaf.skill.context import SkillContext
from vaf.skill.prompt import PromptManager, PromptTemplate

REVIEWER = User("r", {"reviewer"})


# ==========================================================================
# P0-1：开闭式步骤处理器
# ==========================================================================
def test_custom_step_type_via_handler_registry():
    """注册一种全新步骤类型（wait_signal），不修改引擎任何代码。"""
    from vaf.runtime.state import StepStatusEnum
    from pydantic import ConfigDict

    class WaitStep(BaseModel):
        model_config = ConfigDict(extra="forbid")

        type: str = "wait_signal"
        name: str
        depends_on: list = []
        signal_key: str = "ready"

    class WaitStepHandler(StepHandler):
        step_type = WaitStep

        def run(self, ctx: StepRunContext) -> bool:
            # 从 state 变量读取信号，未置位则视为完成（演示用）
            ready = ctx.state.variables.get("signals", {}).get(ctx.step.signal_key, True)
            ctx.ss.status = StepStatusEnum.SUCCEEDED
            ctx.ss.output = {"signalled": ready}
            return False

    app = make_app()
    app.engine.register_step_handler(WaitStepHandler())
    from medical_triage import PatientInput, SymptomIntakeSkill

    pipeline = Pipeline(
        name="custom_step_demo",
        domain="medical_triage",
        steps=[
            WaitStep(name="wait"),
            SkillStep(name="intake", skill="symptom_intake", depends_on=["wait"]),
        ],
    )
    app.engine.register_domain(
        type(app.engine.domain_of("medical_triage"))(
            name="medical_triage",
            input_contract=PatientInput,
            pipeline=pipeline,
            skills={"symptom_intake": SymptomIntakeSkill()},
        )
    )
    h = app.run(
        "custom_step_demo",
        {**EMERGENCY_PATIENT, "raw_id": "cs-1"},
        "custom-step-1",
    )
    assert h.status.value == "succeeded", h.error
    state = app.state_store.get("custom-step-1")
    assert state.step_states["wait"].output == {"signalled": True}
    assert state.step_states["intake"].status.value == "succeeded"


def test_unknown_step_type_gets_clear_error():
    app = make_app()
    from pydantic import ConfigDict

    class MysteryStep(BaseModel):
        model_config = ConfigDict(extra="forbid")
        type: str = "mystery"
        name: str

    from medical_triage import PatientInput, SymptomIntakeSkill

    pipeline = Pipeline(
        name="mystery_demo",
        domain="medical_triage",
        steps=[MysteryStep(name="m")],
    )
    with pytest.raises(VafError, match="no StepHandler registered"):
        app.engine.register_domain(
            type(app.engine.domain_of("medical_triage"))(
                name="medical_triage",
                input_contract=PatientInput,
                pipeline=pipeline,
                skills={"symptom_intake": SymptomIntakeSkill()},
            )
        )


# ==========================================================================
# P2-4：GenerationStep 流式步骤
# ==========================================================================
class SafeGenSkill(GenerationSkill):
    name = "discharge_note_gen"
    description = "生成出院指导"
    version = "1.0.0"
    output_contract = TextOutput

    def build_prompt(self, ctx):
        return "生成出院指导文本"

    def validate_chunk(self, buffer):
        return ["违规内容"] if "违禁词" in buffer else []

    def validate_generation(self, content, ctx):
        return []


def test_generation_step_streams_within_pipeline():
    app = make_app()
    # 回复足够长以触发多次 checkpoint
    long_text = "出院指导：多休息，多饮水，按时服药，如不适及时复诊。" * 6
    app.model_router.register(
        "mock-gen",
        ScriptedMockLLM(name="g", responder=lambda m, t, s, **o: long_text, chunk_size=16),
        tier=1,
    )
    # 熔断主端点，强制路由到 mock-gen
    for _ in range(10):
        app.model_router.record_failure("mock-main")

    from medical_triage import PatientInput

    pipeline = Pipeline(
        name="gen_demo",
        domain="medical_triage",
        steps=[
            GenerationStep(name="gen_note", skill="discharge_note_gen", checkpoint_size=40, holdback_size=12),
        ],
    )
    reg = app.engine.domain_of("medical_triage")
    skills = dict(reg.skills)
    skills["discharge_note_gen"] = SafeGenSkill()
    app.engine.register_domain(
        type(reg)(
            name="medical_triage",
            input_contract=PatientInput,
            pipeline=pipeline,
            skills=skills,
        )
    )
    h = app.run("gen_demo", {**EMERGENCY_PATIENT, "raw_id": "gen-1"}, "gen-step-1")
    assert h.status.value == "succeeded", h.error
    state = app.state_store.get("gen-step-1")
    ss = state.step_states["gen_note"]
    assert ss.status.value == "succeeded"
    assert ss.output["text"] == long_text
    # 流式事件留痕
    events = [e for e in app.events.tail(50) if e["type"] == "generation_streamed"]
    assert events and events[-1]["payload"]["chars"] == len(long_text)


def test_generation_step_violation_aborts_run():
    app = make_app()
    bad_text = "正常开头。" + "这里出现违禁词导致熔断" + "，后续内容不应流出。"
    app.model_router.register(
        "mock-bad",
        ScriptedMockLLM(name="gb", responder=lambda m, t, s, **o: bad_text, chunk_size=8),
        tier=1,
    )
    for _ in range(10):
        app.model_router.record_failure("mock-main")

    from medical_triage import PatientInput

    pipeline = Pipeline(
        name="gen_bad",
        domain="medical_triage",
        steps=[GenerationStep(name="gen_note", skill="discharge_note_gen", checkpoint_size=16, holdback_size=6)],
    )
    reg = app.engine.domain_of("medical_triage")
    skills = dict(reg.skills)
    skills["discharge_note_gen"] = SafeGenSkill()
    app.engine.register_domain(
        type(reg)(name="medical_triage", input_contract=PatientInput, pipeline=pipeline, skills=skills)
    )
    h = app.run("gen_bad", {**EMERGENCY_PATIENT, "raw_id": "gen-2"}, "gen-step-2")
    assert h.status.value == "failed"
    assert "violated" in h.error or "StreamViolation" in h.error


def test_generation_step_requires_generation_skill():
    app = make_app()
    from medical_triage import PatientInput

    pipeline = Pipeline(
        name="gen_misuse",
        domain="medical_triage",
        steps=[GenerationStep(name="g", skill="symptom_intake")],  # ExtractionSkill → 拒绝
    )
    reg = app.engine.domain_of("medical_triage")
    with pytest.raises(Exception, match="GenerationSkill"):
        app.engine.register_domain(
            type(reg)(name="medical_triage", input_contract=PatientInput, pipeline=pipeline, skills=reg.skills)
        )


# ==========================================================================
# P1-1：输出快速构造器
# ==========================================================================
class QuickOutput(SkillOutput):
    conclusion: str


def test_quick_output_assembles_closed_chain():
    out = quick_output(
        QuickOutput,
        conclusion="ok",
        confidence=Confidence.HIGH,
        claims=[
            ("连接池耗尽是根因", "db-proxy ERROR: pool exhausted", SourceGrade.L1, "logs"),
            ("支付链路受影响", "payment-api upstream timeout", SourceGrade.L2, "alerts"),
        ],
        falsifications=[quick_falsification("池占用回落", "<80%", "推翻根因", checker_id=None)],
    )
    # 引用闭合由契约校验器保证（构造即校验，未抛即通过）
    assert len(out.findings) == 2
    assert out.findings[0].evidence_refs == ["ev1"]
    # 同 label 来源去重共享 ref
    labels = [s.label for s in out.sources]
    assert labels == ["logs", "alerts"]
    assert out.sources[0].grade == SourceGrade.L1


def test_quick_output_rejects_empty_claims():
    with pytest.raises(ContractError, match="at least one claim"):
        quick_output(QuickOutput, conclusion="ok", confidence=Confidence.HIGH, claims=[])


def test_quick_output_default_falsification_placeholder():
    out = quick_output(
        QuickOutput,
        conclusion="ok",
        confidence=Confidence.MEDIUM,
        claims=[("唯一依据", "证据原文")],
    )
    assert len(out.falsification) == 1  # 占位证伪（满足非哨兵 ≥1）


def test_quick_insufficient_builds_sentinel():
    from enum import Enum

    class C(str, Enum):
        OK = "ok"
        INSUFFICIENT = INSUFFICIENT_DATA

    class Out(SkillOutput):
        conclusion: C

    out = quick_insufficient(Out, questions=["血压多少？"])
    assert out.conclusion == INSUFFICIENT_DATA
    assert out.clarifying_questions == ["血压多少？"]
    assert out.data_gaps[0].reason == "not_disclosed"


def test_quick_findings_id_prefix():
    findings, evidence, sources = quick_findings([("a", "b")], prefix="r2_")
    assert findings[0].finding_id == "r2_f1"
    assert evidence[0].ev_id == "r2_ev1"


# ==========================================================================
# P1-2：提示词模板按技能路由
# ==========================================================================
class TemplatedSkill(AnalysisSkill):
    name = "templated_skill"
    version = "1.0.0"
    output_contract = QuickOutput
    prompt_template_id = "custom.med"

    def build_prompt(self, ctx):
        return "领域指令"


def test_prompt_template_per_skill_routing():
    pm = PromptManager()
    pm.register(
        PromptTemplate(
            template_id="custom.med",
            version=1,
            body="[定制模板] 技能={skill} 领域={domain}\n规则:\n{anomalies}\n{error_rules}\n{tools}\n澄清:{clarifications}\n<schema>{output_schema}</schema>\n{domain_prompt}",
        )
    )
    ctx = SkillContext(
        request_id="r", domain="med", domain_input=_input(), config=None
    )
    prompt, version = pm.build(TemplatedSkill(), ctx)
    assert prompt.startswith("[定制模板] 技能=templated_skill")
    assert version == 1
    # 版本升级即激活新模板
    pm.register(
        PromptTemplate(
            template_id="custom.med", version=2, body="[v2] {skill} {domain_prompt}"
        )
    )
    prompt2, version2 = pm.build(TemplatedSkill(), ctx)
    assert prompt2.startswith("[v2]") and version2 == 2


def test_unknown_template_id_falls_back_to_core():
    class MissingTemplateSkill(TemplatedSkill):
        name = "missing_template_skill"
        prompt_template_id = "not.registered"

    # 未注册的模板 ID 回退核心模板（而不是崩溃）
    prompt, version = PromptManager().build(MissingTemplateSkill(), SkillContext(
        request_id="r", domain="med", domain_input=_input()
    ))
    assert "「missing_template_skill」" in prompt and version == 1


def _input():
    class Inp(DomainInput):
        pass

    return Inp(raw_id="1", source="s", timestamp="0")


# ==========================================================================
# P1-3：运行期配置读取
# ==========================================================================
def test_config_change_blocks_paused_run_resume():
    """运行期读取的配置版本随快照固化：run 暂停期间配置变更 → 续跑被拒（A3 联动）。"""
    app = make_app()
    app.config.set("medical.emergency_hr_threshold", 110, actor="test")
    h = app.run("medical_triage", {**EMERGENCY_PATIENT, "raw_id": "cfg-1"}, "cfg-run-1")
    assert h.status.value == "waiting_input"
    h = app.submit_clarification("cfg-run-1", {"默认": "是"})
    assert h.status.value == "waiting_review"  # 停在审核：计划指纹仍可校验

    # 配置热更新（新版本 → plan_hash 变化）→ 旧 run 续跑 fail-fast
    app.config.set("medical.emergency_hr_threshold", 95, actor="test")
    with pytest.raises(Exception, match="plan changed"):
        app.resume("cfg-run-1")
    # 新 run 使用新配置版本正常完成
    h2 = app.run("medical_triage", {**EMERGENCY_PATIENT, "raw_id": "cfg-2"}, "cfg-run-2")
    assert h2.status.value in ("waiting_input", "waiting_review")


def test_config_gray_reaches_skill_via_request_bucket():
    app = make_app()
    app.config.set("medical.emergency_hr_threshold", 130)  # v1 基线
    app.config.set("medical.emergency_hr_threshold", 100)  # v2
    app.config.set_gray("medical.emergency_hr_threshold", 2, tenant_in=["vip-tenant"], percentage=0)
    reg = app.engine.domain_of("medical_triage")
    app.engine.tenant = "vip-tenant"
    step = reg.pipeline.step_map()["vitals_risk_check"]
    from vaf.runtime.state import StepState

    ctx = app.engine._build_ctx(
        app.state_store.get("cfg-run-1") or _blank_state(app, "cfg-gray"),
        reg,
        step,
        {},
        StepState(step="vitals_risk_check"),
    )
    assert ctx.config.get("medical.emergency_hr_threshold", 130) == 100  # vip 租户命中灰度


def _blank_state(app, request_id):
    from vaf.runtime.state import PipelineState

    state = PipelineState(request_id=request_id, pipeline="medical_triage", domain="medical_triage")
    state.input = {**EMERGENCY_PATIENT, "raw_id": request_id}
    return state


# ==========================================================================
# P0-2：inspect_run
# ==========================================================================
def test_inspect_run_report():
    app = make_app()
    h = app.run("medical_triage", EMERGENCY_PATIENT, "insp-1")
    assert h.status.value == "waiting_input"
    report = app.inspect_run("insp-1")
    assert report["status"] == "waiting_input"
    names = [s["step"] for s in report["steps"]]
    assert "symptom_intake" in names and "emergency_audit" in names
    assert report["pending_clarification"]["questions"]
    assert report["events_tail"]
    assert app.inspect_run("no-such-run") is None


# ==========================================================================
# P2-2：通知推送
# ==========================================================================
def test_notification_hub_dispatches_review_events():
    received = []
    hub = NotificationHub(sinks=[lambda t, p: received.append((t, p))])
    app = VafApp(notification_hub=hub)
    app.register_model_endpoint("m", ScriptedMockLLM(responder=DemoResponder()), tier=1)
    import medical_triage

    medical_triage.register(app)
    app.run("medical_triage", EMERGENCY_PATIENT, "notif-1")
    app.submit_clarification("notif-1", {"默认": "是"})
    types = [t for t, _ in received]
    assert "clarification_requested" in types
    assert "review_requested" in types
    # 非订阅事件不打扰
    assert "run_started" not in types


def test_webhook_sink_posts_with_signature():
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            captured["body"] = json.loads(body)
            captured["signature"] = self.headers.get("X-VAF-Signature")
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):  # 静默
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sink = webhook_sink(f"http://127.0.0.1:{server.server_port}/hook", secret="s3cret")
        hub = NotificationHub(sinks=[sink])
        hub.notify("review_requested", {"task_id": "t1"})
        assert captured["body"]["event"] == "review_requested"
        assert captured["body"]["payload"]["task_id"] == "t1"
        import hashlib
        import hmac

        expected = hmac.new(
            b"s3cret", json.dumps(
                {"event": "review_requested", "payload": {"task_id": "t1"}}, ensure_ascii=False
            ).encode(), hashlib.sha256
        ).hexdigest()
        assert captured["signature"] == expected
    finally:
        server.shutdown()


def test_webhook_failure_does_not_raise():
    sink = webhook_sink("http://127.0.0.1:1/nowhere", retries=0, timeout_s=0.2)
    hub = NotificationHub(sinks=[sink])
    hub.notify("review_requested", {"x": 1})  # 不抛异常
    assert hub._dropped.value() >= 0  # 指标路径可达


# ==========================================================================
# P2-1：领域自动发现
# ==========================================================================
def test_scan_domain_dirs_and_register(tmp_path):
    (tmp_path / "toy_domain").mkdir()
    (tmp_path / "toy_domain" / "__init__.py").write_text(
        "from enum import Enum\n"
        "from vaf.contract.base import INSUFFICIENT_DATA, DomainInput, SkillOutput\n"
        "from vaf.runtime.steps import Pipeline, SkillStep\n"
        "from vaf.skill.base import AnalysisSkill, BaseSkill\n"
        "from vaf.skill.context import SkillContext\n"
        "from typing import List\n"
        "class ToyInput(DomainInput):\n    pass\n"
        "class ToyOutput(SkillOutput):\n    conclusion: str\n"
        "class ToySkill(AnalysisSkill):\n"
        "    name = 'toy_skill'\n"
        "    version = '1.0.0'\n"
        "    output_contract = ToyOutput\n"
        "    def build_prompt(self, ctx): return 'x'\n"
        "PIPELINE = Pipeline(name='toy_domain', domain='toy_domain',\n"
        "    steps=[SkillStep(name='s', skill='toy_skill')],\n"
        "    output_synthesizer=None)\n"
        "def register(app):\n"
        "    app.register_domain(name='toy_domain', input_contract=ToyInput,\n"
        "        pipeline=PIPELINE, skills=[ToySkill()])\n",
        encoding="utf-8",
    )
    (tmp_path / "not_a_domain").mkdir()  # 无 register → 被忽略
    (tmp_path / "not_a_domain" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    from vaf.runtime.discovery import register_discovered, scan_domain_dirs

    modules = scan_domain_dirs([str(tmp_path)])
    assert [m.__name__ for m in modules] == ["toy_domain"]

    app = VafApp()
    registered = register_discovered(app, dirs=[str(tmp_path)], include_entrypoints=False)
    assert registered == ["toy_domain"]
    assert app.engine.domain_of("toy_domain") is not None


def test_entrypoint_loader(monkeypatch):
    import importlib.metadata as md

    class FakeEP:
        name = "toy"
        value = "medical_triage"

        def load(self):
            import medical_triage

            return medical_triage

    def fake_entry_points():
        class Eps:
            def select(self, group=None):
                return [FakeEP()] if group == "vaf.domains" else []

        return Eps()

    monkeypatch.setattr(md, "entry_points", fake_entry_points)
    from vaf.runtime.discovery import load_entrypoint_domains

    modules = load_entrypoint_domains()
    assert any(m.__name__ == "medical_triage" for m in modules)


# ==========================================================================
# P2-3：RedisStateStore
# ==========================================================================
class FakeRedis:
    """dict 实现的最小 redis 客户端（get/set/expire/scan_iter）。"""

    def __init__(self):
        self.data = {}
        self.ttls = {}

    def set(self, key, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def expire(self, key, ttl):
        self.ttls[key] = ttl

    def scan_iter(self, match=None):
        import fnmatch

        for key in list(self.data.keys()):
            if match is None or fnmatch.fnmatch(key, match):
                yield key


def test_redis_state_store_roundtrip():
    from vaf.runtime.state import PipelineState, RunStatus, StepState, StepStatusEnum

    fake = FakeRedis()
    store = RedisStateStore(fake, ttl_seconds=3600)
    state = PipelineState(request_id="r1", pipeline="p", domain="d", plan_hash="h")
    state.step_states["a"] = StepState(step="a", status=StepStatusEnum.SUCCEEDED, output={"x": 1})
    store.save(state)
    loaded = store.get("r1")
    assert loaded.plan_hash == "h"
    assert loaded.step_states["a"].output == {"x": 1}
    assert fake.ttls["vaf:runs:r1"] == 3600
    assert [s.request_id for s in store.list()] == ["r1"]
    assert store.list(status=RunStatus.FAILED) == []
    assert store.get("ghost") is None
