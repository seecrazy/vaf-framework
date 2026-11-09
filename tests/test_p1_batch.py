"""P1 批次测试：DI 注入 / MCP 接入 / Prometheus 导出。"""
import sys

import pytest

fastapi_testclient = pytest.importorskip("fastapi.testclient")

from e2e_helpers import make_app  # noqa: E402
from vaf.api.auth import ApiKeyStore  # noqa: E402
from vaf.api.gateway import create_gateway  # noqa: E402
from vaf.observe.metrics import to_prometheus  # noqa: E402
from vaf.runtime.persistence import InMemoryStateStore  # noqa: E402
from vaf.runtime.review import ReviewQueue  # noqa: E402
from vaf.tools.mcp import McpStdioClient, register_mcp_server  # noqa: E402


def test_vaf_app_component_injection():
    """七大组件可注入替换（不摸内部属性）。"""
    from vaf.app import VafApp
    from vaf.config.center import ConfigCenter
    from vaf.skill.memory import ErrorMemory

    store = InMemoryStateStore()
    queue = ReviewQueue(lambda: 1.0)
    center = ConfigCenter(env="staging")
    memory = ErrorMemory()
    app = VafApp(state_store=store, review_queue=queue, config_center=center, error_memory=memory)
    assert app.state_store is store
    assert app.review_queue is queue
    assert app.config is center
    assert app.error_memory is memory
    # 默认实现路径不受影响
    app2 = VafApp()
    assert app2.state_store is not store


FAKE_MCP = str(__import__("pathlib").Path(__file__).parent / "fixtures_fake_mcp_server.py")


@pytest.fixture()
def mcp_app():
    app = make_app()
    names = register_mcp_server(app, name="fake", command=[sys.executable, FAKE_MCP])
    assert set(names) == {"fake_echo", "fake_add"}
    yield app, names
    app.close()


def test_mcp_tools_list_and_call(mcp_app):
    app, names = mcp_app
    echo = app.tool_registry.fn("fake_echo")({"text": "VAF"})
    assert echo["content"] == "echo:VAF"
    add = app.tool_registry.fn("fake_add")({"a": 20, "b": 22})
    assert add["content"] == "42"
    # spec 声明完整（描述/参数 schema 来自 MCP 服务器）
    spec = app.tool_registry.spec("fake_echo")
    assert spec.description == "回显文本" and "text" in spec.params_schema.get("properties", {})


def test_mcp_error_propagates_as_tool_error(mcp_app):
    from vaf.errors import ToolError

    client = McpStdioClient([sys.executable, FAKE_MCP], "solo")
    try:
        with pytest.raises(ToolError, match="unknown tool"):
            client.call_tool("ghost", {})
    finally:
        client.close()


def test_mcp_tool_usable_by_skill_pipeline(mcp_app):
    """端到端：技能声明 MCP 工具白名单并在推理中调用。"""
    import json

    from vaf.contract.base import DomainInput
    from vaf.llm.mock import ScriptedMockLLM, tool_call
    from vaf.runtime.steps import Pipeline, SkillStep
    from vaf.skill.base import AnalysisSkill
    from vaf.skill.context import SkillContext
    from vaf.contract.base import SkillOutput

    app, _ = mcp_app

    class QInput(DomainInput):
        question: str = ""

    class QOut(SkillOutput):
        conclusion: str

    class MathSkill(AnalysisSkill):
        name = "math_skill"
        version = "1.0.0"
        output_contract = QOut

        def tools(self):
            return ["fake_add"]

        def model_tier(self) -> int:
            return 0  # 命中 math-mock 端点

        def build_prompt(self, ctx: SkillContext) -> str:
            return "计算"

    # 覆盖端点：先请求工具调用，拿到结果后输出结论
    scripted = ScriptedMockLLM(script=[
        [tool_call("fake_add", {"a": 40, "b": 2})],
        json.dumps({
            "conclusion": "42", "confidence": "high",
            "findings": [{"finding_id": "f1", "claim": "计算完成", "evidence_refs": ["ev1"]}],
            "evidence": [{"ev_id": "ev1", "content": "add 返回 42"}],
            "falsification": [{"signal": "输入变更", "threshold": "参数变化", "consequence": "重算"}],
        }, ensure_ascii=False),
    ])
    app.model_router.register("math-mock", scripted, tier=0)  # tier 0 最优先
    app.register_domain(
        name="math_demo", input_contract=QInput,
        pipeline=Pipeline(name="math_demo", domain="math_demo",
                          steps=[SkillStep(name="m", skill="math_skill")]),
        skills=[MathSkill()],
        synthesizer=lambda state, outputs: outputs["m"],
    )
    h = app.run("math_demo", {"raw_id": "q1", "source": "t", "timestamp": "0", "question": "40+2"},
                "mcp-e2e-1")
    assert h.status.value == "succeeded", h.error
    assert h.result["conclusion"] == "42"


def test_prometheus_export_and_metrics_endpoint():
    app = make_app()
    app.metrics.counter("runs_total", "run 总数").inc(pipeline="p1")
    app.metrics.histogram("step_duration_s").observe(0.5, type="skill")
    text = to_prometheus(app.metrics)
    assert "vaf_runs_total" in text and 'pipeline="p1"' in text
    assert "# TYPE vaf_step_duration_s gauge" in text and "_stat=\"p95\"" in text

    gateway = create_gateway(app, keys=ApiKeyStore.from_specs(["k:ops:operator"]),
                             review_base="http://x")
    client = fastapi_testclient.TestClient(gateway)
    body = client.get("/metrics", headers={"X-VAF-API-Key": "k"}).text
    assert "vaf_runs_total" in body
    assert client.get("/metrics").status_code == 401  # 需要鉴权


# ---------------------------------------------------------------- P2/P3 批次
def test_ambient_preset_physician_signoff():
    from vaf.presets import try_preset

    results = try_preset("ambient_clinical_notes")
    for _, handle in results:
        assert handle.status.value == "waiting_review"  # 草稿必须医生签核
        assert handle.pending_review_task


def test_falsify_sweep_cli(tmp_path):
    """证伪回检：状态库含带 checker 的历史 run → sweep 触发处置并审计。"""
    import argparse
    import json
    import shutil

    from vaf.app import VafApp
    from vaf.llm.mock import ScriptedMockLLM
    from vaf.presets import install_preset
    from vaf.runtime.state import (
        PipelineState,
        RunStatus,
        StepState,
        StepStatusEnum,
    )

    db = str(tmp_path / "f.db")
    domains_dir = tmp_path / "domains"
    install_preset("credit_review", target_dir=str(domains_dir))

    # 写入一条含证伪条件的历史 run（评分 420 → 命中 recheck checker）
    app1 = VafApp(sqlite_path=db)
    app1.register_model_endpoint("m", ScriptedMockLLM(name="m", responder=lambda *a, **k: "{}"), tier=1)
    app1.load_domain_file(str(domains_dir / "credit_review.domain.yaml"))
    state = PipelineState(
        request_id="fs-1", pipeline="credit_review", domain="credit_review",
        status=RunStatus.SUCCEEDED,
        input={"raw_id": "x", "source": "t", "timestamp": "0", "loan_amount": 1000,
               "term_months": 3, "monthly_income": 100, "monthly_debt_payment": 0,
               "credit_score": 420, "loan_purpose": "教育"},
    )
    state.step_states["clarify_intake"] = StepState(
        step="clarify_intake", status=StepStatusEnum.SUCCEEDED,
        output={"conclusion": "approve", "confidence": "high",
                "falsification": [{"signal": "评分下降", "threshold": "score<600",
                                    "consequence": "撤销", "check_frequency": "daily",
                                    "checker_id": "credit_score_recheck"}]})
    app1.state_store.save(state)
    memory_sink = type("S", (), {"write": lambda self, r: None, "records": lambda self, a=None: []})()
    app1.audit.add_sink(memory_sink)
    app1.close = lambda: None

    # sweep 用独立进程语义：build_app 发现 domains/ + 读同一个状态库
    import vaf.cli as cli
    orig_build = cli.build_app
    def build_with_store(sqlite_path=None, **kw):
        return app1  # 复用同一 app（checker 已注册）
    cli.build_app = build_with_store
    try:
        rc = cli.cmd_falsify_sweep(argparse.Namespace(state=db))
    finally:
        cli.build_app = orig_build
    assert rc == 0
    invalidated = [
        r for s2 in app1.audit._sinks
        for r in (s2.records("conclusion_invalidated") if hasattr(s2, "records") else [])
    ]
    assert invalidated, "评分 420 < 450 应触发证伪处置"


def test_audit_log_chained_hash():
    from vaf.security.audit import AuditLog, MemoryAuditSink

    sink = MemoryAuditSink()
    log = AuditLog([sink])
    log.record("a", "act1", "t1")
    log.record("b", "act2", "t2")
    records = sink.records()
    assert records[1].prev_hash == records[0].hash  # 链式
    # 篡改可检测：改内容后重算 hash ≠ 链上留存的 hash → 验证器据此发现篡改
    records[0].actor = "tampered"
    assert records[0]._compute_hash() != records[0].hash


def test_sse_events_endpoint():
    import threading
    import time

    app = make_app()
    gateway = create_gateway(app, keys=None)
    client = fastapi_testclient.TestClient(gateway)
    with client.stream("GET", "/api/v1/events/stream") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["content-type"]
        # 发一个事件，应能在流中读到
        app.events.publish("review_requested", {"task_id": "sse-1"})
        received = b""
        deadline = time.time() + 5
        for chunk in response.iter_bytes():
            received += chunk
            if b"sse-1" in received or time.time() > deadline:
                break
        assert b"sse-1" in received
