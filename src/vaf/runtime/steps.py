"""Pipeline 步骤模型（DESIGN §7.1）：discriminated union + YAML 加载。

步骤类型：skill / human_review / interaction / router / sub_pipeline。
human_review.on_timeout 无默认值——高危决策必须显式配置（B6）。
"""
from __future__ import annotations

from typing import Annotated, List, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ..errors import PipelineValidationError


class SkillStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["skill"] = "skill"
    name: str
    skill: str
    depends_on: List[str] = Field(default_factory=list)
    parallel_group: Optional[str] = None
    condition: Optional[str] = None
    loop_condition: Optional[str] = None
    max_loops: int = 3
    on_schema_fail: Literal["retry", "skip", "degrade", "abort"] = "retry"
    max_retries: int = 2
    state_acl: List[str] = Field(default_factory=list)


class HumanReviewStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["human_review"] = "human_review"
    name: str
    depends_on: List[str] = Field(default_factory=list)
    trigger_condition: str
    timeout_seconds: int = 3600
    on_timeout: Literal["pass", "reject", "degrade"]  # 无默认：必须显式配置（B6）
    review_fields: List[str] = Field(default_factory=list)
    required_role: Optional[str] = None


class InteractionStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["interaction"] = "interaction"
    name: str
    skill: str
    depends_on: List[str] = Field(default_factory=list)
    trigger_condition: Optional[str] = None
    max_rounds: int = 3
    on_exhausted: Literal["abort", "skip", "degrade"] = "abort"


class RouteRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    condition: str
    go_to: str


class RouterStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["router"] = "router"
    name: str
    depends_on: List[str] = Field(default_factory=list)
    routes: List[RouteRule] = Field(default_factory=list)
    default_next: Optional[str] = None


class SubPipelineStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sub_pipeline"] = "sub_pipeline"
    name: str
    pipeline: str
    depends_on: List[str] = Field(default_factory=list)
    input_from: Optional[str] = None


class GenerationStep(BaseModel):
    """生成（流式）步骤（V2.1）：走 executor.stream_execute 的滚动校验 + holdback。"""

    model_config = ConfigDict(extra="forbid")

    type: Literal["generation"] = "generation"
    name: str
    skill: str
    depends_on: List[str] = Field(default_factory=list)
    condition: Optional[str] = None
    checkpoint_size: int = 80
    holdback_size: int = 24


AnyStep = Annotated[
    Union[
        SkillStep,
        HumanReviewStep,
        InteractionStep,
        RouterStep,
        SubPipelineStep,
        GenerationStep,
    ],
    Field(discriminator="type"),
]

# 开放步骤联合（V2.1）：内置类型走判别联合；自定义步骤类型以 BaseModel 子类实例
# 直接构造 Pipeline（类保留），由 engine.register_step_handler 注册的处理器执行。
# YAML 中出现未注册的 type 标签会被解析为裸 BaseModel 并在注册期报错提示。


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    domain: str
    version: str = "1.0.0"
    preprocessor: Optional[str] = None
    steps: List[Union[AnyStep, BaseModel]]
    output_synthesizer: Optional[str] = None
    audit_skill: Optional[str] = None
    on_audit_fail: Literal["abort", "accept_revision"] = "accept_revision"

    def step_map(self) -> dict:
        return {s.name: s for s in self.steps}


_pipeline_adapter: TypeAdapter = TypeAdapter(Pipeline)


def pipeline_from_dict(data: dict) -> Pipeline:
    try:
        pipeline = _pipeline_adapter.validate_python(data)
    except Exception as exc:  # pydantic 错误转领域错误
        raise PipelineValidationError(f"invalid pipeline definition: {exc}") from exc
    # YAML/dict 路径严格化：未知 type 标签会退化成裸 BaseModel（字段丢失），
    # 在此直接拒绝；自定义步骤类型请以 Python 实例构造 Pipeline。
    for step in pipeline.steps:
        if type(step).__name__ == "BaseModel":
            raise PipelineValidationError(
                f"step '{getattr(step, 'name', '?')}' has unknown type tag "
                f"'{getattr(step, 'type', '?')}'; custom step types must be constructed in Python"
            )
    return pipeline


def pipeline_from_yaml(path: str) -> Pipeline:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise PipelineValidationError(f"pipeline yaml must be a mapping: {path}")
    return pipeline_from_dict(data)


def pipeline_from_yaml_text(text: str) -> Pipeline:
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise PipelineValidationError("pipeline yaml must be a mapping")
    return pipeline_from_dict(data)


# ---------------------------------------------------------------------------
# 拓扑工具：Kahn 环检测
# ---------------------------------------------------------------------------
def detect_cycle(steps: List[AnyStep]) -> Optional[List[str]]:
    names = {s.name for s in steps}
    deps = {s.name: [d for d in s.depends_on if d in names] for s in steps}
    indegree = {n: 0 for n in names}
    for n, ds in deps.items():
        indegree[n] = len(ds)
    queue = [n for n, d in indegree.items() if d == 0]
    order: List[str] = []
    while queue:
        current = queue.pop()
        order.append(current)
        for n, ds in deps.items():
            if current in ds:
                indegree[n] -= 1
                if indegree[n] == 0:
                    queue.append(n)
    if len(order) != len(names):
        remaining = sorted(n for n in names if n not in order)
        return remaining
    return None
