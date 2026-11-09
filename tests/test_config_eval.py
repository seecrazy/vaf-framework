"""配置中心与评估闭环测试。"""
import math

import pytest

from vaf.config.center import ConfigCenter
from vaf.eval.dataset import dataset_from_yaml_text
from vaf.eval.drift import DriftDetector, js_divergence
from vaf.eval.judge import JudgeGovernance
from vaf.eval.runner import CaseResult, EvalRunner, Gate
from vaf.errors import VafError
from vaf.security.audit import AuditLog, MemoryAuditSink


# ---------------------------------------------------------------- 配置中心
def test_versioning_and_env_isolation():
    center = ConfigCenter(env="test")
    center.set("threshold", 0.5, env="test")
    center.set("threshold", 0.9, env="prod")
    assert center.get("threshold", env="test") == 0.5
    assert center.get("threshold", env="prod") == 0.9
    assert center.get("threshold", env="dev", default=1.0) == 1.0
    assert center.versions_snapshot(env="test") == {"threshold": 1}


def test_hot_update_listener_fires():
    center = ConfigCenter()
    seen = []
    center.on_change("k", lambda key, value: seen.append((key, value)))
    center.set("k", "v1")
    center.set("k", "v2")
    assert seen == [("k", "v1"), ("k", "v2")]


def test_gray_release_by_tenant_and_percentage():
    center = ConfigCenter(env="prod")
    center.set("flag", "old", env="prod")
    center.set("flag", "new", env="prod")
    v2 = center.history("flag")[-1].version
    center.set_gray("flag", v2, tenant_in=["vip-tenant"], percentage=0.0, env="prod")
    assert center.get("flag", env="prod", tenant="vip-tenant") == "new"
    assert center.get("flag", env="prod", tenant="normal") == "old"  # percentage=0 不放量


def test_gray_percentage_stable_bucket():
    center = ConfigCenter(env="prod")
    center.set("k", "a", env="prod")
    center.set("k", "b", env="prod")
    center.set_gray("k", 2, percentage=50.0, env="prod")
    hits = sum(
        1
        for i in range(100)
        if center.get("k", env="prod", request_id=f"req-{i}") == "b"
    )
    assert 20 < hits < 80  # 稳定哈希分桶，期望约 50
    # 同一 request_id 两次读取结果一致（稳定性）
    first = center.get("k", env="prod", request_id="req-42")
    assert first == center.get("k", env="prod", request_id="req-42")


def test_rollback_appends_new_version():
    center = ConfigCenter()
    center.set("k", "v1")
    center.set("k", "bad")
    center.rollback("k", to_version=1)
    assert center.get("k") == "v1"
    assert center.history("k")[-1].version == 3  # 版本链只增不减
    with pytest.raises(VafError):
        center.rollback("k", to_version=99)


def test_gray_unknown_version_rejected():
    center = ConfigCenter()
    center.set("k", 1)
    with pytest.raises(VafError, match="unknown version"):
        center.set_gray("k", 99)


# ---------------------------------------------------------------- 裁判治理（B4）
def test_judge_must_register_before_semantic_eval():
    gov = JudgeGovernance()
    with pytest.raises(VafError, match="no judge model registered"):
        gov.require_registered()


def test_judge_calibration_gate():
    gov = JudgeGovernance(min_agreement=0.8, min_calibration_n=30)
    with pytest.raises(VafError, match="agreement"):
        gov.register("m", "v1", calibration_n=100, agreement=0.5)
    with pytest.raises(VafError, match="calibration sample"):
        gov.register("m", "v1", calibration_n=10, agreement=0.95)
    gov.register("m", "v1", calibration_n=100, agreement=0.95)
    assert gov.active().model == "m"


def test_judge_change_warns():
    gov = JudgeGovernance()
    gov.register("m1", "v1", 50, 0.9)
    gov.register("m2", "v1", 50, 0.9)
    assert any("not comparable" in w for w in gov.warnings)


# ---------------------------------------------------------------- 漂移检测
def test_js_divergence_identity_and_shift():
    assert js_divergence([1, 1], [1, 1]) < 1e-6
    assert js_divergence([10, 0], [0, 10]) > 0.5


def test_input_missing_rate_drift():
    det = DriftDetector(js_threshold=0.1)
    base = [{"a": 1, "b": 2}] * 10
    curr = [{"a": 1}] * 10  # b 全缺失
    alert = det.input_missing_rate("b", base, curr)
    assert alert.score > 0.1


def test_output_distribution_drift():
    det = DriftDetector(js_threshold=0.05)
    base = {"emergency": 5, "routine": 95}
    curr = {"emergency": 60, "routine": 40}
    report = det.detect(output_distributions=[("conclusion", base, curr)])
    assert report.has_alert and report.alerts[0].subject == "conclusion"


def test_metric_control_limit_3sigma():
    det = DriftDetector(sigma=3.0)
    series = [0.5] * 20 + [0.9]
    alert = det.metric_control_limit("accuracy", series)
    assert alert.score > 3.0
    assert det.metric_control_limit("ok", [0.5] * 20).score <= 3.0


# ---------------------------------------------------------------- 评估指标
def test_metrics_computation():
    cases = [
        CaseResult(case_id="1", status="succeeded", conclusion="a", expected_conclusion="a", correct=True, falsification_provided=True),
        CaseResult(case_id="2", status="succeeded", conclusion="b", expected_conclusion="a", correct=False, falsification_provided=True),
        CaseResult(case_id="3", status="failed"),
    ]
    m = EvalRunner._compute_metrics(cases)
    assert m["conclusion_accuracy"] == 0.5
    assert m["contract_pass_rate"] == pytest.approx(2 / 3, abs=0.01)
    assert m["falsification_rate"] == 1.0


def test_gate_passed():
    g = Gate(metric="conclusion_accuracy", threshold=0.8)
    assert g.passed(0.9)
    assert not g.passed(0.7)


def test_dataset_yaml_load():
    ds = dataset_from_yaml_text(
        """
domain: demo
cases:
  - case_id: c1
    input: {raw_id: "1"}
    expected_conclusion: ok
    tags: [smoke]
"""
    )
    assert ds.domain == "demo"
    assert ds.filtered("smoke")[0].case_id == "c1"
    assert ds.filtered("none") == []
