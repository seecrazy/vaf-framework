"""medical_triage 端到端：澄清回路 → 人工审核 → 幂等 → 审核超时 → 审计降级。"""
import pytest

from e2e_helpers import EMERGENCY_PATIENT, MILD_PATIENT
from vaf.security.permissions import User


REVIEWER = User("reviewer-1", {"reviewer"})


def test_full_flow_clarify_then_review(build_medical_app):
    app = build_medical_app
    h = app.run("medical_triage", EMERGENCY_PATIENT, "e2e-med-1")
    # 1) 信息不足 → 澄清暂停
    assert h.status.value == "waiting_input"
    assert h.pending_clarification and len(h.pending_clarification.questions) >= 1

    h = app.submit_clarification("e2e-med-1", {"疼痛是否放射？": "是"})
    # 2) 急诊结论 → 人工审核暂停
    assert h.status.value == "waiting_review"
    assert h.pending_review_task

    h = app.submit_review(h.pending_review_task, "approved", reviewer=REVIEWER, comment="同意")
    # 3) 审核通过 → 分层 → 合成
    assert h.status.value == "succeeded"
    assert h.result["conclusion"] == "emergency"
    assert h.result["clarification_rounds"] == 1
    assert h.result["department"] == "急诊科"


def test_idempotent_replay_returns_cached(build_medical_app):
    app = build_medical_app
    h = app.run("medical_triage", MILD_PATIENT, "e2e-med-2")
    # 轻症不触发澄清/审核，直接完成
    assert h.status.value == "succeeded"
    again = app.run("medical_triage", MILD_PATIENT, "e2e-med-2")
    assert again.status.value == "succeeded"
    assert again.result == h.result


def test_review_rejection_fails_run(build_medical_app):
    app = build_medical_app
    h = app.run("medical_triage", EMERGENCY_PATIENT, "e2e-med-3")
    h = app.submit_clarification("e2e-med-3", {"疼痛是否放射？": "是"})
    h = app.submit_review(h.pending_review_task, "rejected", reviewer=REVIEWER, comment="信息存疑")
    assert h.status.value == "failed"
    assert "rejected" in h.error


def test_review_requires_role(build_medical_app):
    app = build_medical_app
    h = app.run("medical_triage", EMERGENCY_PATIENT, "e2e-med-4")
    h = app.submit_clarification("e2e-med-4", {"疼痛是否放射？": "是"})
    from vaf.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError, match="reviewer"):
        app.submit_review(h.pending_review_task, "approved", reviewer=User("intern", {"operator"}))


def test_review_timeout_sweep_applies_policy(build_medical_app):
    app = build_medical_app
    h = app.run("medical_triage", EMERGENCY_PATIENT, "e2e-med-5")
    h = app.submit_clarification("e2e-med-5", {"疼痛是否放射？": "是"})
    task_id = h.pending_review_task
    # 时间快进：直接把任务 deadline 置于过去
    task = app.review_queue.get(task_id)
    app.review_queue._tasks[task_id].deadline = app.clock() - 1
    handles = app.sweep_review_timeouts()
    assert handles, "sweep 应产生至少一个 resume 句柄"
    # on_timeout=reject → 失败
    final = app.get_run("e2e-med-5")
    assert final.status.value == "failed"
    assert "review timeout" in final.error


def test_audit_revision_downgrades_low_confidence_emergency():
    """低置信 emergency：终审否决并给出修订（C7 修订路径）。"""
    import json

    from vaf.app import VafApp
    from vaf.demo_llm import DemoResponder
    from vaf.llm.mock import ScriptedMockLLM
    import medical_triage

    class LowConfResponder:
        def __init__(self, inner):
            self.inner = inner

        def __call__(self, messages, tools, response_schema, **opts):
            system = messages[0]["content"]
            if "「differential_diagnosis」" in system and "咳嗽" not in system:
                data = json.loads(self.inner(messages, tools, response_schema, **opts))
                data["confidence"] = "low"
                return json.dumps(data, ensure_ascii=False)
            return self.inner(messages, tools, response_schema, **opts)

    app = VafApp()
    app.register_model_endpoint(
        "mock-low", ScriptedMockLLM(name="mock-low", responder=LowConfResponder(DemoResponder())), tier=1
    )
    medical_triage.register(app)

    h = app.run("medical_triage", EMERGENCY_PATIENT, "e2e-med-6")
    if h.status.value == "waiting_input":
        h = app.submit_clarification("e2e-med-6", {"疼痛是否放射？": "是"})
    if h.status.value == "waiting_review":
        h = app.submit_review(h.pending_review_task, "approved", reviewer=REVIEWER)
    assert h.status.value == "succeeded"
    # 终审把低置信急诊降级为 urgent
    assert h.result["conclusion"] == "urgent"


def test_falsification_realtime_flag_recorded(build_medical_app):
    """DemoResponder 的证伪条件未绑定 checker_id —— 引擎不应崩溃；
    绑定 checker 的条件由 medical 领域 FeverRecheckChecker 在 run-med 场景验证。"""
    app = build_medical_app
    h = app.run("medical_triage", MILD_PATIENT, "e2e-med-7")
    assert h.status.value == "succeeded"
    # 状态可追溯
    state = app.state_store.get("e2e-med-7")
    assert state.plan_hash
    assert all(ss.status.value in ("succeeded", "skipped") for ss in state.step_states.values())


def test_sqlite_persistence_across_app_instances(tmp_path):
    from vaf.app import VafApp
    from vaf.demo_llm import DemoResponder
    from vaf.llm.mock import ScriptedMockLLM
    import medical_triage

    db = str(tmp_path / "state.db")
    app1 = VafApp(sqlite_path=db)
    app1.register_model_endpoint("m", ScriptedMockLLM(responder=DemoResponder()), tier=1)
    medical_triage.register(app1)
    h1 = app1.run("medical_triage", EMERGENCY_PATIENT, "persist-1")
    assert h1.status.value == "waiting_input"

    # 新实例从 SQLite 恢复并继续
    app2 = VafApp(sqlite_path=db)
    app2.register_model_endpoint("m", ScriptedMockLLM(responder=DemoResponder()), tier=1)
    medical_triage.register(app2)
    h2 = app2.submit_clarification("persist-1", {"疼痛是否放射？": "是"})
    assert h2.status.value == "waiting_review"
    h3 = app2.submit_review(h2.pending_review_task, "approved", reviewer=REVIEWER)
    assert h3.status.value == "succeeded"
