"""零代码领域接入测试（V2.2）：动态契约 / 模板插值 / 配置化技能 / 声明式预处理 /
表达式证伪 / result_from / 发现与脚手架 / 端到端。"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from vaf.app import VafApp
from vaf.contract.base import INSUFFICIENT_DATA, Confidence, SkillOutput, is_insufficient
from vaf.demo_llm import DemoResponder
from vaf.errors import VafError
from vaf.llm.mock import ScriptedMockLLM
from vaf.lowcode import (
    DomainSpec,
    ExpressionChecker,
    build_input_model,
    build_output_model,
    load_domain_dict,
    render_template,
)
from vaf.lowcode import InputSpec, FieldSpec, OutputSpec
from lowcode_helpers import TicketOutput
from vaf.skill.context import SkillContext

DOMAIN_FILE = Path(__file__).resolve().parent.parent / "domains" / "customer_ticket.domain.yaml"


def _ticket_app(responder=None):
    app = VafApp()
    app.register_model_endpoint(
        "m", ScriptedMockLLM(name="m", responder=responder or DemoResponder()), tier=1
    )
    app.load_domain_file(str(DOMAIN_FILE))
    return app


TICKET = {
    "raw_id": "tk-1", "source": "app", "timestamp": "0",
    "subject": "云主机被扣款两次，要求核查",
    "body": "本月账单多扣了 320 元", "customer_level": "vip", "order_amount": 320.0,
}


# ==========================================================================
# 动态契约建模
# ==========================================================================
def test_input_model_build_and_validation():
    model = build_input_model("t", InputSpec(fields=[
        FieldSpec(name="a", type="str", required=True),
        FieldSpec(name="n", type="float", min=0, max=100),
        FieldSpec(name="tags", type="list", required=False),
        FieldSpec(name="level", enum=["low", "high"], default="low"),
        FieldSpec(name="code", type="str", pattern=r"^\d{4}$"),
    ]))
    obj = model(raw_id="r", source="s", timestamp="t", a="x", n=50, tags=[], code="1234")
    assert obj.level == "low"
    with pytest.raises(ValidationError):
        model(raw_id="r", source="s", timestamp="t", a="x", n=200)  # n 超上限
    with pytest.raises(ValidationError):
        model(raw_id="r", source="s", timestamp="t", a="x", n=1, code="abc")  # pattern
    with pytest.raises(ValidationError):
        model(raw_id="r", source="s", timestamp="t", n=1, a="x", extra_field=1)  # extra=forbid


def test_output_model_sentinel_auto_added():
    model = build_output_model("t", "out", OutputSpec(conclusion_enum=["ok", "bad"]))
    assert issubclass(model, SkillOutput)
    # 哨兵自动追加：能构造 insufficient 输出
    sentinel_out = model(
        conclusion=INSUFFICIENT_DATA, confidence=Confidence.LOW, clarifying_questions=["q?"]
    )
    assert is_insufficient(sentinel_out.conclusion)
    # 注册期哨兵校验必然通过
    from vaf.contract.base import ensure_insufficient_member

    ensure_insufficient_member(model)


def test_output_entity_kind_is_plain_model():
    model = build_output_model("t", "ent", OutputSpec(kind="entity", fields=[
        FieldSpec(name="items", type="list"),
    ]))
    assert issubclass(model, object) and not issubclass(model, SkillOutput)
    assert model(items=[1, 2]).items == [1, 2]


def test_analysis_output_requires_conclusion_enum():
    with pytest.raises(VafError, match="conclusion_enum"):
        build_output_model("t", "out", OutputSpec(kind="analysis"))


def test_unsupported_field_type_rejected():
    with pytest.raises(VafError, match="不支持"):
        build_input_model("t", InputSpec(fields=[FieldSpec(name="x", type="pickle")]))


# ==========================================================================
# 模板插值（沙箱表达式）
# ==========================================================================
def _ctx_with(payload=None, upstream=None):
    from vaf.contract.base import DomainInput

    class Inp(DomainInput):
        note: str = ""

    inp = Inp(raw_id="1", source="s", timestamp="0", note=payload or "hello")
    return SkillContext(request_id="r", domain="d", domain_input=inp, upstream_outputs=upstream or {})


def test_render_template_basic_and_upstream():
    from vaf.skill.base import TextOutput

    ctx = _ctx_with("世界", upstream={"prev": TextOutput(text="上一段")})
    out = render_template("输入：{{ input.note }}；上游：{{ upstream.prev }}", _tmpl_ns(ctx))
    assert "输入：世界" in out
    assert "上一段" in out and "text" in out  # BaseModel → JSON


def _tmpl_ns(ctx):
    from vaf.lowcode import _template_namespace

    return _template_namespace(ctx, [])


def test_render_template_rejects_dangerous_expression():
    with pytest.raises(VafError, match="插值失败"):
        render_template("{{ __import__('os') }}", {"input": {}})


def test_render_template_enum_and_missing():
    from enum import Enum

    class E(str, Enum):
        A = "a"

    ns = {"input": {"e": E.A}}
    assert render_template("{{ input.e }}", ns) == "a"
    with pytest.raises(VafError):
        render_template("{{ input.missing.field }}", {"input": {}})


# ==========================================================================
# 配置化终审（veto + 降级）
# ==========================================================================
def test_config_audit_veto_and_downgrade():
    from vaf.lowcode import ConfigAuditSkill, SkillSpec

    contract = build_output_model("t", "cls", OutputSpec(
        conclusion_enum=["billing", "manual_review"],
    ))
    skill = ConfigAuditSkill(
        SkillSpec(
            name="guard", type="audit", output="cls",
            veto_when="output.conclusion != 'manual_review' and output.confidence == 'low'",
            downgrade_to="manual_review", confidence_after="medium",
        ),
        contract,
    )
    low = TicketOutput.build(contract, "billing", Confidence.LOW)
    result = skill.audit(low, _ctx_with())
    assert result.passed is False and result.revision is not None
    assert result.revision.conclusion == "manual_review"
    assert result.revision.confidence == Confidence.MEDIUM
    assert "audit_downgraded" in result.revision.compliance_tags
    high = TicketOutput.build(contract, "billing", Confidence.HIGH)
    assert skill.audit(high, _ctx_with()).passed is True


def test_config_audit_bad_downgrade_target():
    from vaf.lowcode import ConfigAuditSkill, SkillSpec

    contract = build_output_model("t", "cls", OutputSpec(conclusion_enum=["billing"]))
    skill = ConfigAuditSkill(
        SkillSpec(name="g", type="audit", output="cls",
                  veto_when="1 == 1", downgrade_to="ghost", confidence_after="medium"),
        contract,
    )
    low = contract(
        conclusion="billing", confidence=Confidence.LOW,
        findings=[{"finding_id": "f1", "claim": "c", "evidence_refs": ["e1"]}],
        evidence=[{"ev_id": "e1", "content": "x"}],
        falsification=[{"signal": "s", "threshold": "t", "consequence": "c"}],
    )
    with pytest.raises(VafError, match="downgrade_to"):
        skill.audit(low, _ctx_with())


# ==========================================================================
# 声明式预处理 / 表达式校验器
# ==========================================================================
def test_assemble_preprocessor_nodes():
    from vaf.lowcode import PreprocessorSpec, assemble_preprocessor
    from vaf.preprocessor.nodes import RawItem

    factory = assemble_preprocessor(PreprocessorSpec(
        validators=[
            {"type": "field_presence", "fields": ["subject", "vitals.hr"]},
            {"type": "range", "field": "vitals.hr", "low": 30, "high": 220, "level": "fatal"},
        ],
        scorer={"type": "static_scorer", "field": "sev", "map": {"high": 10}, "default": 1},
    ))
    pre = factory()
    items = pre.run([RawItem(id="i", payload={
        "subject": "x", "sev": "high", "vitals": {"hr": 300},
    })])
    assert items[0].score == 10.0
    details = " ".join(a.detail for a in items[0].anomalies)
    assert "hr=300" in details  # 嵌套点路径区间校验


def test_assemble_preprocessor_unknown_node():
    from vaf.lowcode import PreprocessorSpec, assemble_preprocessor

    factory = assemble_preprocessor(PreprocessorSpec(validators=[{"type": "quantum"}]))
    with pytest.raises(VafError, match="未注册"):
        factory()


def test_expression_checker():
    checker = ExpressionChecker("ck", "'已解决' in input.subject")
    assert checker.check({"input": {"subject": "问题已解决"}}) is True
    assert checker.check({"input": {"subject": "问题仍在"}}) is False
    # 上下文缺字段不崩溃
    assert checker.check({"input": {}}) is False


# ==========================================================================
# 端到端：零代码领域
# ==========================================================================
def test_zero_code_domain_e2e_billing():
    app = _ticket_app()
    h = app.run("customer_ticket", TICKET, "lc-1")
    assert h.status.value == "succeeded", h.error
    assert h.result["conclusion"] == "billing"
    assert h.result["subcategory"]
    # 预处理疑点注入提示词（PII 检测在 subject/body 无 PII 时不标记）
    state = app.state_store.get("lc-1")
    assert state.plan_hash


def test_zero_code_domain_falsification_triggers():
    app = _ticket_app()
    ticket = {**TICKET, "subject": "扣款问题已解决，感谢"}
    h = app.run("customer_ticket", ticket, "lc-2")
    assert h.status.value == "succeeded", h.error
    assert "工单标记已解决" in h.falsification_flags  # 表达式校验器实时触发


def test_zero_code_domain_audit_downgrade_path():
    """低置信分类被 YAML 终审否决并降级 manual_review（全程零代码）。"""
    import json as _json

    class LowConf(DemoResponder):
        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「classify」" in system:
                data = _json.loads(super().__call__(messages, tools, response_schema, **opts))
                data["confidence"] = "low"
                return _json.dumps(data, ensure_ascii=False)
            return super().__call__(messages, tools, response_schema, **opts)

    app = _ticket_app(responder=LowConf())
    h = app.run("customer_ticket", TICKET, "lc-3")
    assert h.status.value == "succeeded", h.error
    assert h.result["conclusion"] == "manual_review"  # 终审降级生效
    assert h.result["confidence"] == "medium"


def test_zero_code_domain_input_contract_enforced():
    app = _ticket_app()
    with pytest.raises(Exception, match="subject"):
        app.run("customer_ticket", {**TICKET, "subject": None}, "lc-4")


def test_zero_code_domain_eval_runner():
    from vaf.eval.dataset import dataset_from_dict
    from vaf.eval.runner import EvalRunner, Gate

    app = _ticket_app()
    dataset = dataset_from_dict({
        "domain": "customer_ticket",
        "cases": [
            {"case_id": "c-billing", "input": TICKET, "expected_conclusion": "billing"},
            {"case_id": "c-tech", "input": {**TICKET, "raw_id": "tk-2", "subject": "实例无法启动"},
             "expected_conclusion": "technical"},
        ],
    })
    report = EvalRunner(app.engine).run(
        dataset, request_prefix="lc-eval",
        gates=[Gate(metric="conclusion_accuracy", threshold=1.0)],
    )
    assert report.gate_passed, report.failed_gates
    assert report.metrics["conclusion_accuracy"] == 1.0


def test_zero_code_domain_interaction_loop():
    """零代码领域同样支持交互澄清回路（interaction 步骤 + 哨兵）。"""

    class Responder(DemoResponder):
        def __init__(self):
            super().__init__()
            self.clarified = False

        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「ask_more」" in system:
                if not self.clarified:
                    self.clarified = True
                    return json.dumps({
                        "conclusion": "insufficient_data", "confidence": "low",
                        "clarifying_questions": ["订单号是多少？"],
                    }, ensure_ascii=False)
                return json.dumps({
                    "conclusion": "ok", "confidence": "high",
                    "findings": [
                        {"finding_id": "f1", "claim": "信息已补齐", "evidence_refs": ["ev1"]},
                    ],
                    "evidence": [{"ev_id": "ev1", "content": "订单号 8899"}],
                    "falsification": [
                        {"signal": "订单号无效", "threshold": "订单不存在", "consequence": "推翻 ok"},
                    ],
                }, ensure_ascii=False)
            if "「classify」" in system and "ask_demo" in system:
                return json.dumps({
                    "conclusion": "ok", "confidence": "high",
                    "findings": [
                        {"finding_id": "f1", "claim": "输入已处理", "evidence_refs": ["ev1"]},
                    ],
                    "evidence": [{"ev_id": "ev1", "content": "payload x"}],
                    "falsification": [
                        {"signal": "输入变更", "threshold": "payload 变化", "consequence": "推翻 ok"},
                    ],
                }, ensure_ascii=False)
            return super().__call__(messages, tools, response_schema, **opts)

    spec = {
        "domain": "ask_demo", "version": "1.0.0",
        "input": {"fields": [{"name": "payload", "type": "str"}]},
        "outputs": {"main": {"conclusion_enum": ["ok"]}},
        "skills": [
            {"name": "ask_more", "type": "analysis", "output": "main",
             "prompt": "输入 {{ input.payload }}"},
            {"name": "classify", "type": "analysis", "output": "main",
             "prompt": "输入 {{ input.payload }}，澄清 {{ clarifications }}"},
        ],
        "pipeline": {
            "name": "ask_demo", "domain": "ask_demo",
            "steps": [
                {"name": "ask", "type": "interaction", "skill": "ask_more", "max_rounds": 2},
                {"name": "cls", "type": "skill", "skill": "classify", "depends_on": ["ask"]},
            ],
        },
        "result_from": "cls",
    }
    app = VafApp()
    app.register_model_endpoint("m", ScriptedMockLLM(name="m", responder=Responder()), tier=1)
    app.load_domain_dict(spec)
    h = app.run("ask_demo", {"raw_id": "a", "source": "s", "timestamp": "t", "payload": "x"}, "lc-5")
    assert h.status.value == "waiting_input"
    assert h.pending_clarification.questions == ["订单号是多少？"]
    h = app.submit_clarification("lc-5", {"订单号是多少？": "8899"})
    assert h.status.value == "succeeded", h.error


# ==========================================================================
# 校验与错误信息
# ==========================================================================
def test_load_domain_validates_expressions_upfront():
    bad = {
        "domain": "bad", "pipeline": {"name": "bad", "domain": "bad", "steps": []},
        "skills": [
            {"name": "s", "type": "analysis", "output": "o",
             "prompt": "{{ eval('x') }}"},
        ],
        "outputs": {"o": {"conclusion_enum": ["ok"]}},
    }
    with pytest.raises(VafError, match="插值"):
        load_domain_dict(bad)


def test_load_domain_unknown_output_reference():
    bad = {
        "domain": "bad2", "pipeline": {"name": "bad2", "domain": "bad2",
                                        "steps": [{"name": "s", "type": "skill", "skill": "s"}]},
        "skills": [{"name": "s", "type": "analysis", "output": "ghost", "prompt": "x"}],
        "outputs": {},
    }
    with pytest.raises(VafError, match="未声明的输出契约"):
        load_domain_dict(bad)


def test_load_domain_result_from_default_last_terminal():
    parts = load_domain_dict({
        "domain": "rd", "pipeline": {"name": "rd", "domain": "rd",
                                     "steps": [{"name": "only", "type": "skill", "skill": "s"}]},
        "skills": [{"name": "s", "type": "generation", "prompt": "生成 {{ input.x }}"}],
    })
    # 未声明 result_from → 默认最后一个终端步骤
    from vaf.contract.base import DomainInput

    state = type("S", (), {"request_id": "r"})()
    from vaf.skill.base import TextOutput

    result = parts["synthesizer"](state, {"only": TextOutput(text="t")})
    assert result.text == "t"


# ==========================================================================
# 发现与脚手架
# ==========================================================================
def test_discovery_loads_domain_yaml_files(tmp_path):
    (tmp_path / "mini.domain.yaml").write_text(
        """domain: mini
version: "1.0"
input:
  fields: [{name: x, type: str}]
outputs:
  main: {conclusion_enum: [ok]}
skills:
  - name: s1
    type: analysis
    output: main
    prompt: "输入 {{ input.x }}"
pipeline:
  name: mini
  domain: mini
  steps: [{name: st, type: skill, skill: s1}]
""",
        encoding="utf-8",
    )
    from vaf.runtime.discovery import register_discovered

    app = VafApp()
    registered = register_discovered(app, dirs=[str(tmp_path)], include_entrypoints=False)
    assert "mini" in registered
    assert app.engine.domain_of("mini") is not None


def test_scaffold_lowcode_generates_loadable_domain(tmp_path, capsys):
    import argparse

    from vaf.cli import cmd_scaffold

    cmd_scaffold(argparse.Namespace(
        name="scaffold_demo", dir=str(tmp_path), force=True, style="lowcode"
    ))
    path = tmp_path / "scaffold_demo.domain.yaml"
    assert path.exists()
    app = VafApp()
    name = app.load_domain_file(str(path))
    assert name == "scaffold_demo"
    # 生成的模板通过结构校验（技能/契约/编排齐备）
    assert app.engine.domain_of(name).skills


def test_scaffold_code_style_still_works(tmp_path):
    import argparse

    from vaf.cli import cmd_scaffold

    cmd_scaffold(argparse.Namespace(
        name="code_demo", dir=str(tmp_path), force=True, style="code"
    ))
    assert (tmp_path / "code_demo" / "__init__.py").exists()
