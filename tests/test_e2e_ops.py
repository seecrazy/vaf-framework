"""ops_incident 端到端：路由分支 / 探测循环 / 工具 / 子流程 / 失败重试续跑。"""
import json

import pytest

from e2e_helpers import INCIDENT_INPUT
from vaf.app import VafApp
from vaf.demo_llm import DemoResponder
from vaf.llm.mock import ScriptedMockLLM
from vaf.runtime.state import RunStatus


def test_high_confidence_routes_to_deep_probe(build_ops_app):
    app = build_ops_app
    h = app.run("ops_incident", INCIDENT_INPUT, "e2e-ops-1")
    assert h.status.value == "succeeded", h.error
    state = app.state_store.get("e2e-ops-1")
    # 高置信假设 → deep_probe；quick_probe 被剪枝
    assert state.routers == {"route_next": "deep_probe"}
    assert state.step_states["deep_probe"].status.value == "succeeded"
    assert state.step_states["quick_probe"].status.value == "skipped"
    # 子流程完成并把 child_request_id 传回
    assert h.result["child_report_request_id"] == "e2e-ops-1:report_sub"
    child = app.state_store.get("e2e-ops-1:report_sub")
    assert child.status == RunStatus.SUCCEEDED
    assert child.result["report_markdown"].startswith("# 事故报告")
    # 工具被调用（探测 ≥1 轮）
    tool_calls = app.metrics_snapshot()["counters"]["tool_calls_total"]
    assert any(t["labels"]["tool"] == "log_probe" for t in tool_calls)


def test_low_confidence_routes_directly_to_synthesis():
    """低置信假设 → 直接合成，探测全部剪枝。"""
    app = VafApp()

    class LowConf(DemoResponder):
        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「hypothesis_gen」" in system:
                data = json.loads(super().__call__(messages, tools, response_schema, **opts))
                data["confidence"] = "low"
                data["findings"] = [
                    {"finding_id": "f0", "claim": "线索不足", "evidence_refs": ["ev0"]},
                ]
                data["evidence"] = [{"ev_id": "ev0", "content": "仅一条告警"}]
                return json.dumps(data, ensure_ascii=False)
            return super().__call__(messages, tools, response_schema, **opts)

    app.register_model_endpoint("m", ScriptedMockLLM(responder=LowConf()), tier=1)
    import ops_incident

    ops_incident.register(app)
    h = app.run("ops_incident", INCIDENT_INPUT, "e2e-ops-2")
    assert h.status.value == "succeeded", h.error
    state = app.state_store.get("e2e-ops-2")
    assert state.routers == {"route_next": "synthesize_rca"}
    assert state.step_states["deep_probe"].status.value == "skipped"
    assert state.step_states["quick_probe"].status.value == "skipped"


def test_failed_run_resumes_from_failed_step():
    """schema 持续失败 → run FAILED → 修复 LLM 后 resume 从断点续跑（幂等跳过已完成步骤）。"""
    app = VafApp()

    class BrokenThenFixed:
        def __init__(self):
            self.fixed = False
            self.inner = DemoResponder()

        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「alert_triage」" in system and not self.fixed:
                return json.dumps({"conclusion": "critical"})  # 缺必填证据链
            return self.inner(messages, tools, response_schema, **opts)

    stateful = BrokenThenFixed()
    app.register_model_endpoint("m", ScriptedMockLLM(responder=stateful), tier=1)
    import ops_incident

    ops_incident.register(app)

    h1 = app.run("ops_incident", INCIDENT_INPUT, "e2e-ops-3")
    assert h1.status.value == "failed"
    assert "schema" in h1.error.lower() or "repair" in h1.error.lower()

    # 修复"模型"后断点续跑
    stateful.fixed = True
    h2 = app.resume("e2e-ops-3")
    assert h2.status.value == "succeeded", h2.error
    state = app.state_store.get("e2e-ops-3")
    # alert_triage 首轮失败后 resume 重新执行成功
    assert state.step_states["alert_triage"].status.value == "succeeded"


def test_sub_pipeline_failure_retries_on_parent_resume():
    """子流程失败 → 父 run 失败 → 父 resume 时子流程从失败点重试（而非幂等冲突）。"""
    app = VafApp()

    class ChildBrokenThenFixed:
        def __init__(self):
            self.fixed = False
            self.inner = DemoResponder()

        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「generate_report」" in system and not self.fixed:
                return json.dumps({"conclusion": "critical"})  # 缺证据链
            return self.inner(messages, tools, response_schema, **opts)

    stateful = ChildBrokenThenFixed()
    app.register_model_endpoint("m", ScriptedMockLLM(responder=stateful), tier=1)
    import ops_incident

    ops_incident.register(app)
    incident = {**INCIDENT_INPUT, "raw_id": "inc-sub"}

    h1 = app.run("ops_incident", incident, "e2e-ops-4")
    assert h1.status.value == "failed"
    assert "report_sub" in h1.error

    stateful.fixed = True
    h2 = app.resume("e2e-ops-4")
    assert h2.status.value == "succeeded", h2.error
    child = app.state_store.get("e2e-ops-4:report_sub")
    assert child is not None
    assert child.step_states["generate_report"].status.value == "succeeded"


def test_compensation_runs_on_failure():
    """非幂等副作用工具执行后流程失败 → 引擎逆序补偿（undo_fn 被调用）。"""
    from vaf.tools.registry import ToolSpec

    tickets = {"created": [], "undone": []}

    def create_ticket(args):
        tickets["created"].append(args["title"])
        return {"ticket_id": len(tickets["created"])}

    def undo_ticket(args):
        tickets["undone"].append(args["title"])
        return {"undone": True}

    class ToolThenFail:
        """先调用工具（副作用成功），再持续输出违约 JSON → 技能重试耗尽 → run 失败。"""

        def __init__(self):
            self.inner = DemoResponder()
            self.tool_done = False

        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「ops_dispatch」" in system:
                if not self.tool_done:
                    self.tool_done = True
                    from vaf.llm.mock import tool_call

                    return [tool_call("create_ticket", {"title": "INC-42"})]
                return json.dumps({"conclusion": "critical"})  # 持续违约
            return self.inner(messages, tools, response_schema, **opts)

    app = VafApp()
    app.register_model_endpoint("m", ScriptedMockLLM(responder=ToolThenFail()), tier=1)
    import ops_incident as ops

    ops.register(app)
    app.register_tool(
        create_ticket,
        ToolSpec(
            name="create_ticket",
            description="创建工单（演示补偿）",
            has_side_effect=True,
            idempotent=False,
        ),
        undo_fn=undo_ticket,
    )

    # 注册一个调用该工具的最小领域
    from vaf.contract.base import DomainInput, INSUFFICIENT_DATA
    from vaf.runtime.steps import Pipeline, SkillStep
    from vaf.skill.base import AnalysisSkill

    class DispatchInput(DomainInput):
        title: str = ""

    from enum import Enum

    class DispatchConclusion(str, Enum):
        DONE = "done"
        INSUFFICIENT = INSUFFICIENT_DATA

    from vaf.contract.base import SkillOutput

    class DispatchOutput(SkillOutput):
        conclusion: DispatchConclusion

    class DispatchSkill(AnalysisSkill):
        name = "ops_dispatch"
        version = "1.0.0"
        output_contract = DispatchOutput

        def tools(self):
            return ["create_ticket"]

        def build_prompt(self, ctx):
            return "调用 create_ticket 后给出结论"

    app.register_domain(
        name="ops_dispatch",
        input_contract=DispatchInput,
        pipeline=Pipeline(
            name="ops_dispatch",
            domain="ops_dispatch",
            steps=[SkillStep(name="dispatch", skill="ops_dispatch", max_retries=1)],
        ),
        skills=[DispatchSkill()],
    )

    h = app.run(
        "ops_dispatch",
        {"raw_id": "d-1", "source": "t", "timestamp": "0", "title": "INC-42"},
        "comp-1",
    )
    assert h.status.value == "failed"
    assert tickets["created"] == ["INC-42"]
    # 失败路径触发逆序补偿
    assert tickets["undone"] == ["INC-42"]
    state = app.state_store.get("comp-1")
    assert state.side_effects and state.side_effects[0].done is True
    # 补偿动作有审计留痕
    audit_records = [
        r for r in app.audit._sinks[0].records("tool_undo")
    ]
    assert audit_records
