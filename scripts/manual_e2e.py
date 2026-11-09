"""端到端手动验证脚本：medical_triage 全流程（开发期自检，等价逻辑进正式测试）。"""
import json
import sys

sys.path.insert(0, "src")
sys.path.insert(0, "domains")

from vaf.app import VafApp
from vaf.llm.mock import ScriptedMockLLM
from vaf.security.permissions import User
import medical_triage as medical


def _triage_json(conclusion, confidence, findings=1, falsify=False, red_flags=None):
    data = {
        "conclusion": conclusion,
        "confidence": confidence,
        "findings": [
            {"finding_id": f"f{i}", "claim": f"依据{i}", "evidence_refs": [f"ev{i}"]}
            for i in range(findings)
        ],
        "evidence": [{"ev_id": f"ev{i}", "content": f"输入数据{i}"} for i in range(findings)],
        "falsification": (
            [
                {
                    "signal": "复测体温",
                    "threshold": "<38.5",
                    "consequence": "推翻发热判断",
                    "check_frequency": "realtime",
                    "checker_id": "triage_fever_recheck",
                }
            ]
            if falsify
            else []
        ),
    }
    if red_flags is not None:
        data["red_flags"] = red_flags
    return data


def _responder(messages, tools, response_schema, **opts):
    system = messages[0]["content"]
    if "「symptom_intake」" in system:
        return json.dumps(
            {"symptoms": ["chest_pain"], "duration_hours": 3, "severity_1to10": 7},
            ensure_ascii=False,
        )
    if "「vitals_risk_check」" in system:
        return json.dumps(_triage_json("urgent", "medium", findings=1, falsify=True), ensure_ascii=False)
    if "「triage_clarify」" in system:
        if "已收集澄清回答：{}" in system:
            return json.dumps(
                {
                    "conclusion": "insufficient_data",
                    "confidence": "low",
                    "clarifying_questions": ["疼痛是否放射至左臂/下颌？", "是否伴出汗恶心？"],
                },
                ensure_ascii=False,
            )
        return json.dumps(_triage_json("emergency", "high", findings=2, falsify=True), ensure_ascii=False)
    if "「differential_diagnosis」" in system:
        return json.dumps(
            _triage_json("emergency", "high", findings=2, falsify=True, red_flags=["radiating_pain"]),
            ensure_ascii=False,
        )
    if "「risk_stratification」" in system:
        return json.dumps(_triage_json("emergency", "high", findings=1, falsify=True), ensure_ascii=False)
    raise AssertionError(f"unexpected skill prompt: {system[:120]}")


app = VafApp()
mock = ScriptedMockLLM(name="mock-main", responder=_responder)
app.register_model_endpoint("mock-endpoint", mock, tier=1)
medical.register(app)

patient = {
    "raw_id": "p-001",
    "source": "hotline",
    "timestamp": "2026-11-08T10:00:00",
    "chief_complaint": "胸痛 3 小时，联系电话 13800138000",
    "vitals": {"heart_rate": 128, "temperature_c": 36.8, "systolic_bp": 150},
    "age": 58,
    "gender": "male",
}

h1 = app.run("medical_triage", patient, "run-med-1")
print("1) 澄清暂停:", h1.status.value, h1.pending_clarification.questions if h1.pending_clarification else None)

h2 = app.submit_clarification("run-med-1", {"疼痛是否放射至左臂/下颌？": "是", "是否伴出汗恶心？": "是"})
print("2) 审核暂停:", h2.status.value, "task:", h2.pending_review_task)

reviewer = User("dr-house", {"reviewer"})
h3 = app.submit_review(h2.pending_review_task, "approved", reviewer=reviewer, comment="同意急诊")
print("3) 最终:", h3.status.value, "结论:", (h3.result or {}).get("conclusion"), "澄清轮数:", (h3.result or {}).get("clarification_rounds"))

h4 = app.run("medical_triage", patient, "run-med-1")
print("4) 幂等重放:", h4.status.value)

print("runs_total:", app.metrics_snapshot()["counters"]["runs_total"])
print("MEDICAL E2E OK")
