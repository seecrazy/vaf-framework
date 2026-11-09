"""ops_incident 端到端手动验证：路由 → 探测(工具调用) → 子流程 → 合成。"""
import json
import sys

import os
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "domains"))

from vaf.app import VafApp
from vaf.llm.mock import ScriptedMockLLM, tool_call
import ops_incident as ops


def _out(**kw):
    base = {
        "conclusion": "degraded",
        "confidence": "medium",
        "findings": [
            {"finding_id": "f0", "claim": "依据0", "evidence_refs": ["ev0"]},
            {"finding_id": "f1", "claim": "依据1", "evidence_refs": ["ev1"]},
        ],
        "evidence": [
            {"ev_id": "ev0", "content": "告警: db-proxy connection pool exhausted"},
            {"ev_id": "ev1", "content": "告警: payment-api upstream timeout"},
        ],
        "falsification": [
            {
                "signal": "db-proxy 连接池指标回落",
                "threshold": "active < max 的 80%",
                "consequence": "推翻连接池耗尽根因",
                "check_frequency": "realtime",
            }
        ],
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


_probe_round = {"n": 0}


def _responder(messages, tools, response_schema, **opts):
    system = messages[0]["content"]
    if "「alert_triage」" in system:
        return _out(conclusion="critical", suspected_components=["db-proxy"])
    if "「hypothesis_gen」" in system:
        return _out(
            conclusion="degraded",
            confidence="high",
            hypotheses=[
                {"component": "db-proxy", "guess": "连接池耗尽", "confidence": "high"}
            ],
            next_probe_target="db-proxy",
        )
    if "「probe_executor」" in system:
        # 第 1 轮：先请求工具；拿到工具结果后给结论
        has_tool_result = any(m.get("role") == "tool" for m in messages)
        if not has_tool_result:
            return [tool_call("log_probe", {"target": "db-proxy", "pattern": "error"})]
        gaps = [] if _probe_round["n"] >= 1 else [
            {"item": "payment-api 视角日志", "reason": "unavailable", "impact": "minor"}
        ]
        _probe_round["n"] += 1
        return _out(
            conclusion="degraded",
            probe_target="db-proxy",
            evidence_found=True,
            probe_summary="db-proxy ERROR: connection pool exhausted",
            data_gaps=gaps,
        )
    if "「prepare_report_input」" in system:
        return json.dumps(
            {
                "raw_id": "inc-001",
                "source": "prometheus",
                "timestamp": "2026-11-08T10:01:00",
                "root_cause": "db-proxy 连接池耗尽导致支付链路超时",
                "severity": "critical",
            },
            ensure_ascii=False,
        )
    if "「rca_synthesizer」" in system:
        return _out(
            conclusion="critical",
            suspected_components=["db-proxy"],
            suggested_actions=["重启 db-proxy 并扩容连接池", "对 payment-api 熔断降级"],
        )
    if "「generate_report」" in system:
        return _out(
            conclusion="critical",
            report_markdown="# 事故报告\n根因：db-proxy 连接池耗尽",
            notify_channels=["oncall-im", "status-page"],
        )
    raise AssertionError(f"unexpected prompt: {system[:120]}")


app = VafApp()
mock = ScriptedMockLLM(name="mock-ops", responder=_responder)
app.register_model_endpoint("mock-endpoint", mock, tier=1)
ops.register(app)

incident = {
    "raw_id": "inc-001",
    "source": "prometheus",
    "timestamp": "2026-11-08T10:01:00",
    "service": "payment",
    "oncall": "alice",
    "alerts": [
        {"id": "a1", "alert_key": "K1", "severity": "critical", "title": "db-proxy conn pool"},
        {"id": "a2", "alert_key": "K1", "severity": "critical", "title": "db-proxy conn pool (dup)"},
        {"id": "a3", "alert_key": "K3", "severity": "", "title": "no severity"},
    ],
}

h = app.run("ops_incident", incident, "run-ops-1")
print("status:", h.status.value)
print("error:", h.error[:300] if h.error else "")
if h.result:
    r = h.result
    print("conclusion:", r.get("conclusion"), "| root_cause:", r.get("root_cause"))
    print("probe_rounds:", r.get("probe_rounds"), "| child:", r.get("child_report_request_id"))
    print("actions:", r.get("actions"))
state = app.state_store.get("run-ops-1")
print("routers:", state.routers if state else None)
print("steps:", {k: v.status.value for k, v in state.step_states.items()} if state else None)
tool_calls = app.metrics_snapshot()["counters"].get("tool_calls_total", [])
print("tool_calls:", tool_calls)
print("OPS E2E OK" if h.status.value == "succeeded" else "OPS E2E FAILED")
