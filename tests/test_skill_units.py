"""技能层单元测试：提示词组装 / 自修复重试 / 流式 / 错误记忆 / grounding / StateView。"""
import json

import pytest
from pydantic import BaseModel, ValidationError

from vaf.contract.base import (
    Confidence,
    DomainInput,
    Evidence,
    FalsificationCondition,
    Finding,
    SkillOutput,
    SourceGrade,
    SourceRef,
)
from vaf.errors import BudgetExceededError, MaxToolRoundsExceededError, PermissionDeniedError, SchemaValidationError
from vaf.llm.mock import ScriptedMockLLM, tool_call
from vaf.llm.router import ModelRouter
from vaf.skill.base import AnalysisSkill, BaseSkill
from vaf.skill.context import DEFAULT_STATE_ACL, SkillContext, StateView
from vaf.skill.executor import SkillExecutor
from vaf.skill.grounding import GroundingChecker
from vaf.skill.memory import ErrorMemory, ErrorRule
from vaf.skill.prompt import PromptManager, PromptTemplate
from vaf.tools.adapter import ToolAdapter
from vaf.tools.registry import ToolRegistry, ToolSpec


# ---------------------------------------------------------------- 测试装置
class MyInput(DomainInput):
    payload: str = ""


class MyOutput(SkillOutput):
    conclusion: str


class MySkill(AnalysisSkill):
    name = "my_skill"
    description = "test"
    version = "1.0.0"
    output_contract = MyOutput

    def build_prompt(self, ctx):
        return f"分析：{ctx.domain_input.payload}"


def _output_json(conclusion="ok", with_chain=True):
    data = {"conclusion": conclusion, "confidence": "high"}
    if with_chain:
        data.update(
            findings=[{"finding_id": "f1", "claim": "c", "evidence_refs": ["ev1"]}],
            evidence=[{"ev_id": "ev1", "content": "原始输入", "source_ref": "s1"}],
            sources=[{"ref_id": "s1", "grade": "L1"}],
            falsification=[
                {"signal": "s", "threshold": "t", "consequence": "c"}
            ],
        )
    return json.dumps(data, ensure_ascii=False)


def _executor(mock, **kw):
    router = ModelRouter()
    router.register("mock", mock, tier=1)
    registry = ToolRegistry()
    registry.register(lambda a: {"echo": a.get("x")}, ToolSpec(name="echo"))
    adapter = ToolAdapter(registry)
    prompts = PromptManager()
    return SkillExecutor(router=router, prompt_manager=prompts, tool_adapter=adapter, **kw), registry, adapter


def _ctx(payload="hello", **kw):
    return SkillContext(
        request_id="r1",
        domain="test",
        domain_input=MyInput(raw_id="1", source="t", timestamp="0", payload=payload),
        **kw,
    )


# ---------------------------------------------------------------- PromptManager
def test_prompt_injects_schema_rules_and_skill_name():
    (prompt, version) = PromptManager().build(MySkill(), _ctx())
    assert "「my_skill」" in prompt
    assert '"output_schema"' in prompt or "output_schema" in prompt
    assert "insufficient_data" in prompt
    assert "分析：hello" in prompt
    assert version == 1


def test_prompt_redacts_sensitive_classification():
    ctx = _ctx(payload="电话 13800138000")
    ctx.data_classification = "sensitive"
    prompt, _ = PromptManager().build(MySkill(), ctx)
    assert "13800138000" not in prompt


def test_prompt_template_versioning_and_activation():
    pm = PromptManager()
    pm.register(PromptTemplate(template_id="t.custom", version=1, body="v1 {domain_prompt}"))
    pm.register(PromptTemplate(template_id="t.custom", version=2, body="v2 {domain_prompt}"))
    assert pm.active_version("t.custom") == 2
    pm.activate_version("t.custom", 1)
    assert pm.active_version("t.custom") == 1
    with pytest.raises(ValueError, match="duplicate"):
        pm.register(PromptTemplate(template_id="t.custom", version=2, body="dup"))


def test_prompt_injects_error_rules_and_anomalies():
    from vaf.contract.base import Anomaly
    from vaf.preprocessor.nodes import PreprocessedItem, RawItem

    mem = ErrorMemory()
    mem.add_schema_error("my_skill", "MyOutput", "必须输出 findings")
    item = PreprocessedItem(
        item=RawItem(id="i", payload={"payload": "hello"}),
        anomalies=[Anomaly(source="v", detail="体温缺失")],
    )
    ctx = _ctx(preprocessed=[item], error_rules=mem.rules_for("my_skill"))
    prompt, _ = PromptManager().build(MySkill(), ctx)
    assert "必须输出 findings" in prompt
    assert "体温缺失" in prompt


# ---------------------------------------------------------------- 执行器：解析与自修复
def test_execute_parses_valid_output():
    mock = ScriptedMockLLM(responder=lambda m, t, s, **o: _output_json())
    executor, _, _ = _executor(mock)
    out = executor.execute(MySkill(), _ctx())
    assert isinstance(out, MyOutput) and out.conclusion == "ok"


def test_markdown_fences_stripped():
    wrapped = "```json\n" + _output_json() + "\n```"
    mock = ScriptedMockLLM(script=[wrapped])
    executor, _, _ = _executor(mock)
    out = executor.execute(MySkill(), _ctx())
    assert out.conclusion == "ok"


def test_self_repair_retry_recovers():
    mock = ScriptedMockLLM(
        script=[
            json.dumps({"conclusion": "ok"}),  # 缺 findings/falsification → 校验失败
            _output_json(),  # 修复后通过
        ]
    )
    executor, _, _ = _executor(mock)
    out = executor.execute(MySkill(), _ctx())
    assert out.conclusion == "ok"
    # 修复消息确实回喂了校验错误
    repair_msgs = [m for m in mock.last_messages if m.get("role") == "user"]
    assert repair_msgs and "违反" in repair_msgs[-1]["content"]


def test_self_repair_exhausted_raises_and_records_error_rule():
    mock = ScriptedMockLLM(responder=lambda m, t, s, **o: json.dumps({"conclusion": "ok"}))
    executor, _, _ = _executor(mock)
    with pytest.raises(SchemaValidationError, match="repair attempts"):
        executor.execute(MySkill(), _ctx())
    rules = executor.error_memory.rules_for("my_skill")
    assert any("MyOutput" in r.rule_text for r in rules)


def test_tool_loop_executes_via_adapter():
    registry = ToolRegistry()
    registry.register(lambda a: {"echo": a.get("x")}, ToolSpec(name="echo"))
    router = ModelRouter()
    router.register("mock", ScriptedMockLLM(name="m"), tier=1)
    adapter = ToolAdapter(registry)
    executor = SkillExecutor(router=router, prompt_manager=PromptManager(), tool_adapter=adapter)

    class ToolSkill(MySkill):
        name = "tool_skill"

        def tools(self):
            return ["echo"]

    mock = ScriptedMockLLM(
        script=[
            [tool_call("echo", {"x": "hi"})],  # 先请求工具
            _output_json(),  # 再给结论
        ]
    )
    router.register("mock2", mock, tier=2)
    # 强制选择 mock2：把 mock 端点熔断
    for _ in range(10):
        router.record_failure("mock")
    out = executor.execute(ToolSkill(), _ctx())
    assert out.conclusion == "ok"


def test_max_tool_rounds_exceeded():
    class EchoSkill(MySkill):
        name = "echo_skill"

        def tools(self):
            return ["echo"]

    mock = ScriptedMockLLM(responder=lambda m, t, s, **o: [tool_call("echo", {})])
    executor, _, _ = _executor(mock, max_tool_rounds=2)
    with pytest.raises(MaxToolRoundsExceededError):
        executor.execute(EchoSkill(), _ctx())


def test_hallucinated_tool_rejected_by_whitelist():
    """技能未声明工具但 LLM 幻觉调用 → fail-closed 拒绝。"""
    from vaf.errors import PermissionDeniedError

    mock = ScriptedMockLLM(script=[[tool_call("echo", {})]])
    executor, _, _ = _executor(mock)
    with pytest.raises(PermissionDeniedError, match="not declared"):
        executor.execute(MySkill(), _ctx())


def test_token_budget_exceeded():
    mock = ScriptedMockLLM(responder=lambda m, t, s, **o: _output_json())
    executor, _, _ = _executor(mock, token_budget=1)
    with pytest.raises(BudgetExceededError):
        executor.execute(MySkill(), _ctx())


# ---------------------------------------------------------------- 流式（A4）
from vaf.skill.base import GenerationSkill, TextOutput


class SafeGenSkill(GenerationSkill):
    name = "safe_gen"
    version = "1.0.0"
    output_contract = TextOutput

    def build_prompt(self, ctx):
        return "生成内容"

    def validate_chunk(self, buffer):
        return ["违规"] if "违禁词" in buffer else []

    def validate_generation(self, content, ctx):
        return []


def test_stream_rolling_release_with_holdback():
    text = "这是一段很长的安全内容，" * 10 + "结尾"
    mock = ScriptedMockLLM(name="s", responder=lambda m, t, s, **o: text, chunk_size=8)
    router = ModelRouter()
    router.register("s", mock, tier=1)
    executor = SkillExecutor(router=router, prompt_manager=PromptManager(), tool_adapter=ToolAdapter(ToolRegistry()))
    received = []
    out = executor.stream_execute(SafeGenSkill(), _ctx(), lambda t: received.append(t), checkpoint_size=16, holdback_size=8)
    assert "".join(received) == text
    assert out.degraded is False


def test_stream_violation_aborts():
    text = "正常开头，" + "违禁词出现在中间" + "，之后还有更多内容"
    mock = ScriptedMockLLM(name="s2", responder=lambda m, t, s, **o: text, chunk_size=6)
    router = ModelRouter()
    router.register("s2", mock, tier=1)
    executor = SkillExecutor(router=router, prompt_manager=PromptManager(), tool_adapter=ToolAdapter(ToolRegistry()))
    from vaf.errors import StreamViolationError

    with pytest.raises(StreamViolationError):
        executor.stream_execute(SafeGenSkill(), _ctx(), lambda t: None, checkpoint_size=8, holdback_size=4)


# ---------------------------------------------------------------- 错误记忆（B8）
def test_schema_rule_auto_activates_business_needs_approval():
    mem = ErrorMemory()
    mem.add_schema_error("s1", "X", "schema 规则")
    assert mem.rules_for("s1") != []
    rule = mem.add_business_error("s1", "Y", "业务规则")
    assert mem.rules_for("s1") == [] or all(r.rule_text != "业务规则" for r in mem.rules_for("s1"))
    mem.activate(rule.rule_id, approver="alice")
    assert any(r.rule_text == "业务规则" for r in mem.rules_for("s1"))


def test_rule_conflict_latest_wins_and_eviction():
    mem = ErrorMemory(max_active_per_scope=2)
    mem.add_schema_error("s", "p", "规则1")
    mem.add_schema_error("s", "p", "规则2")  # 同 pattern 覆盖
    rules = [r for r in mem.all_rules() if r.scope == "s" and r.status == "active"]
    assert len(rules) == 1 and rules[0].rule_text == "规则2"
    mem.add_schema_error("s", "p2", "规则3")
    mem.add_schema_error("s", "p3", "规则4")  # 超上限 → 淘汰
    active = [r for r in mem.all_rules(status="active") if r.scope == "s"]
    assert len(active) == 2


def test_rule_expiry_retired():
    mem = ErrorMemory()
    rule = mem.add(ErrorRule(kind="schema", scope="s", pattern="p", rule_text="临时", expires_at=1.0))
    import time

    assert mem.rules_for("s", now=time.time() + 10) == []


def test_domain_scope_rules_injected_for_skill():
    mem = ErrorMemory()
    mem.add_schema_error("domain:med", "p", "领域级规则")
    assert any(r.rule_text == "领域级规则" for r in mem.rules_for("any_skill", "med"))


# ---------------------------------------------------------------- Grounding（C10/B2）
def _grounding_output():
    return SkillOutput(
        conclusion="ok",
        confidence=Confidence.HIGH,
        findings=[
            Finding(finding_id="f1", claim="a", evidence_refs=["ev1"]),
            Finding(finding_id="f2", claim="b", evidence_refs=["ev2"]),
        ],
        evidence=[
            Evidence(ev_id="ev1", content="体温 39.2 度"),
            Evidence(ev_id="ev2", content="完全编造的内容不在池中"),
        ],
        sources=[SourceRef(ref_id="s", grade=SourceGrade.L1)],
        falsification=[FalsificationCondition(signal="s", threshold="t", consequence="c")],
    )


def test_grounding_supported_and_unsupported():
    report = GroundingChecker().verify(
        _grounding_output(), ["患者体温 39.2 度", "其他上下文"]
    )
    assert report.total_findings == 2
    assert report.supported_findings == 1
    assert report.unsupported_findings == ["f2"]
    assert 0 < report.score < 1


def test_grounding_full_pool_scores_one():
    out = _grounding_output()
    pool = [e.content for e in out.evidence]
    assert GroundingChecker().verify(out, pool).score == 1.0


# ---------------------------------------------------------------- StateView ACL
def test_state_view_allows_default_prefixes():
    view = StateView(list(DEFAULT_STATE_ACL), {"steps": {"dx": {"output": {"conclusion": "x"}}}, "vars": {"public": {"a": 1}}})
    assert view.get("steps.dx.output.conclusion") == "x"
    assert view.get("vars.public.a") == 1


def test_state_view_denies_out_of_acl():
    view = StateView(list(DEFAULT_STATE_ACL), {"steps": {"dx": {"output": {"secret": 1}}}, "vars": {"private": "x"}})
    with pytest.raises(PermissionDeniedError, match="state_acl"):
        view.get("vars.private")
    with pytest.raises(PermissionDeniedError):
        # 越权读取即使带 default 也直接抛（fail-closed）
        view.get("vars.private", default="fallback")


def test_state_view_custom_acl_prefix():
    view = StateView(["vars.tenant_only.*"], {"vars": {"tenant_only": {"k": "v"}, "other": "x"}})
    assert view.get("vars.tenant_only.k") == "v"
    with pytest.raises(PermissionDeniedError):
        view.get("vars.other")
