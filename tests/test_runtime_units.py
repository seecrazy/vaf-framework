"""运行时基础组件测试：状态持久化 / 审核队列 / 快照 / YAML 步骤模型 / 拓扑。"""
import time

import pytest
from pydantic import ValidationError

from vaf.runtime.persistence import InMemoryStateStore, SqliteStateStore
from vaf.runtime.review import ReviewQueue, ReviewTask
from vaf.runtime.snapshot import build_plan
from vaf.runtime.state import PipelineState, RunStatus, StepState, StepStatusEnum
from vaf.runtime.steps import (
    HumanReviewStep,
    InteractionStep,
    Pipeline,
    RouterStep,
    SkillStep,
    SubPipelineStep,
    detect_cycle,
    pipeline_from_yaml_text,
)
from vaf.errors import ReviewError


# ---------------------------------------------------------------- 持久化
def _state(request_id="r1"):
    s = PipelineState(request_id=request_id, pipeline="p", domain="d", plan_hash="h1")
    s.step_states["a"] = StepState(step="a", status=StepStatusEnum.SUCCEEDED, output={"x": 1})
    s.variables["k"] = [1, 2]
    return s


def test_in_memory_roundtrip():
    store = InMemoryStateStore()
    store.save(_state())
    loaded = store.get("r1")
    assert loaded.step_states["a"].output == {"x": 1}
    assert loaded.variables["k"] == [1, 2]


def test_sqlite_roundtrip_and_list(tmp_path):
    store = SqliteStateStore(str(tmp_path / "state.db"))
    s1 = _state("s1")
    s1.status = RunStatus.RUNNING
    store.save(s1)
    store.save(_state("s2"))
    loaded = store.get("s1")
    assert loaded.plan_hash == "h1"
    assert [s.request_id for s in store.list()] == ["s1", "s2"]
    assert [s.request_id for s in store.list(status=RunStatus.RUNNING)] == ["s1"]
    store.close()


# ---------------------------------------------------------------- 审核队列
def test_review_lifecycle():
    clock = lambda: 1000.0
    q = ReviewQueue(clock)
    task = q.submit("run-1", "audit", "p", on_timeout="reject", timeout_seconds=10)
    assert len(q.pending()) == 1
    decided = q.decide(task.task_id, "approved", reviewer="alice", comment="ok")
    assert decided.status == "approved"
    with pytest.raises(ReviewError, match="already"):
        q.decide(task.task_id, "rejected")
    with pytest.raises(ReviewError, match="unknown review task"):
        q.decide("ghost", "approved")
    with pytest.raises(ReviewError, match="invalid"):
        q.decide(task.task_id, "maybe")
    assert q.get("ghost") is None  # get 不抛，返回 None


def test_review_timeout_sweep():
    t0 = 1000.0
    q = ReviewQueue(lambda: t0)
    task = q.submit("run-1", "audit", "p", on_timeout="pass", timeout_seconds=60)
    assert q.sweep_timeouts(now=1010.0) == []  # 未到期限
    results = q.sweep_timeouts(now=1100.0)
    assert len(results) == 1 and results[0][0].status == "timeout" and results[0][1] == "pass"
    assert q.sweep_timeouts(now=1200.0) == []  # 幂等


# ---------------------------------------------------------------- 快照
def test_plan_hash_changes_with_versions():
    base = dict(
        pipeline_name="p",
        pipeline_version="1.0",
        skill_versions={"s": "1.0"},
        prompt_versions={"vaf.system.core": 1},
        config_versions={"k": 1},
        model_route_digest="ep1",
    )
    h1 = build_plan(**base).plan_hash
    assert h1
    base["skill_versions"] = {"s": "1.1"}
    assert build_plan(**base).plan_hash != h1
    base["skill_versions"] = {"s": "1.0"}
    assert build_plan(**base).plan_hash == h1  # 确定性


# ---------------------------------------------------------------- 步骤模型
def test_human_review_on_timeout_is_required():
    with pytest.raises(ValidationError):
        HumanReviewStep(name="r", trigger_condition="true")  # 缺 on_timeout → 校验失败
    step = HumanReviewStep(name="r", trigger_condition="true", on_timeout="reject")
    assert step.on_timeout == "reject"


def test_pipeline_yaml_roundtrip():
    yaml_text = """
name: demo
domain: demo
version: "1.0"
steps:
  - name: a
    type: skill
    skill: s1
  - name: route
    type: router
    depends_on: [a]
    routes:
      - condition: "steps.a.output.x == 1"
        go_to: b
    default_next: c
  - name: b
    type: skill
    skill: s2
    depends_on: [route]
    condition: "routers.route.selected == 'b'"
  - name: c
    type: skill
    skill: s2
    depends_on: [route]
  - name: ask
    type: interaction
    skill: s3
    depends_on: [b]
    max_rounds: 3
  - name: sub
    type: sub_pipeline
    pipeline: child
    depends_on: [c]
"""
    p = pipeline_from_yaml_text(yaml_text)
    assert p.name == "demo"
    assert isinstance(p.steps[1], RouterStep)
    assert p.steps[1].routes[0].go_to == "b"
    assert isinstance(p.steps[4], InteractionStep)
    assert isinstance(p.steps[5], SubPipelineStep)


def test_unknown_step_type_rejected():
    with pytest.raises(Exception):
        pipeline_from_yaml_text("name: d\ndomain: d\nsteps:\n  - name: x\n    type: quantum")


def test_detect_cycle():
    steps = [
        SkillStep(name="a", skill="s", depends_on=["b"]),
        SkillStep(name="b", skill="s", depends_on=["a"]),
        SkillStep(name="c", skill="s"),
    ]
    assert detect_cycle(steps) is not None
    assert detect_cycle(steps[:1] + steps[2:]) is None
