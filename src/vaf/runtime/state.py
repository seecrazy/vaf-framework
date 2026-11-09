"""Pipeline 状态模型（DESIGN §7.3）：可 JSON 持久化，断点续跑的锚点。"""
from __future__ import annotations

import time
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    WAITING_INPUT = "waiting_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INVALIDATED = "invalidated"  # 证伪处置标记（A5）

    @property
    def terminal(self) -> bool:
        return self in (
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.INVALIDATED,
        )


class StepStatusEnum(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    DEGRADED = "degraded"

    @property
    def done(self) -> bool:
        """依赖视角的完成：后续步骤可继续。"""
        return self in (
            StepStatusEnum.SUCCEEDED,
            StepStatusEnum.SKIPPED,
            StepStatusEnum.DEGRADED,
        )


class StepState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step: str
    status: StepStatusEnum = StepStatusEnum.PENDING
    output: Optional[Any] = None  # model_dump(mode="json") 的输出快照
    output_contract: str = ""  # 输出契约类名（恢复类型化用）
    attempts: int = 0
    loop_count: int = 0
    degraded_reason: str = ""
    error: str = ""
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    reviewer: str = ""
    review_comment: str = ""


class SideEffectRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    undo_key: str
    request_id: str
    tool: str
    description: str
    done: bool = False


class ReviewInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    step: str
    status: str = "pending"  # pending | approved | rejected | timeout
    decision_by: str = ""
    comment: str = ""


class ClarificationInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    round_no: int
    questions: List[str] = Field(default_factory=list)
    answers: Dict[str, Any] = Field(default_factory=dict)


class PipelineState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    pipeline: str
    domain: str = ""
    plan_hash: str = ""
    status: RunStatus = RunStatus.PENDING
    input: Dict[str, Any] = Field(default_factory=dict)
    step_states: Dict[str, StepState] = Field(default_factory=dict)
    variables: Dict[str, Any] = Field(default_factory=dict)
    routers: Dict[str, str] = Field(default_factory=dict)  # router 步骤名 → 选中目标
    tool_calls: List[Dict[str, Any]] = Field(default_factory=list)
    side_effects: List[SideEffectRecord] = Field(default_factory=list)
    reviews: List[ReviewInfo] = Field(default_factory=list)
    clarifications: List[ClarificationInfo] = Field(default_factory=list)
    falsification_flags: List[str] = Field(default_factory=list)
    result: Optional[Any] = None
    result_contract: str = ""
    error: str = ""
    pending_review_task: Optional[str] = None
    pending_clarification: Optional[ClarificationInfo] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    def touch(self) -> None:
        self.updated_at = time.time()

    def step_output(self, step: str) -> Optional[Dict[str, Any]]:
        ss = self.step_states.get(step)
        return ss.output if ss else None
